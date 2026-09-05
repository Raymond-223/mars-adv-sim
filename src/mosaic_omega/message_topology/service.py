"""One facade for topology rebuild and receiver-aware context delivery."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
import time

from ..agent_runtime.task_messages import EvidenceRef, TaskMessage
from ..agent_runtime.trace_context import TRACE_CONTEXTS
from ..storage import ContentAddressedObjectStore
from .config import TopologyConfig
from .decision_impact import DecisionImpactEstimator, ImpactAssessment
from .delta_builder import DeltaBuildResult, DeltaBuilder
from .knowledge_digest import KnowledgeDigestStore
from .message_policy import (
    DeferredMessageQueue,
    MessagePolicy,
    MessagePolicyConfig,
    PolicyAction,
    PolicyDecision,
)
from .models import EdgeCandidate, RebuildResult
from .feedback import DecisionFeedbackEvent, DecisionFeedbackStore
from .telemetry import MessageTopologyTelemetry
from .semantic_dedup import DeduplicationResult, SemanticDeduplicator
from .topology_manager import TopologyManager


@dataclass(frozen=True)
class ContextDeliveryPlan:
    action: PolicyAction
    reason: str
    message: TaskMessage | None
    delta: DeltaBuildResult | None = None
    impact: ImpactAssessment | None = None
    dedup: DeduplicationResult | None = None
    policy: PolicyDecision | None = None
    queue_wait_ms: float | None = None


class MessageTopologyService:
    """Policy-free transport callers can use this as a deterministic gateway."""

    def __init__(
        self,
        *,
        topology_config: TopologyConfig | None = None,
        policy_config: MessagePolicyConfig | None = None,
        object_store: ContentAddressedObjectStore | None = None,
    ) -> None:
        self.topology = TopologyManager(topology_config)
        self.digests = KnowledgeDigestStore()
        self.delta_builder = DeltaBuilder()
        self.impact_estimator = DecisionImpactEstimator()
        self.policy = MessagePolicy(policy_config)
        config = topology_config or TopologyConfig()
        self.deduplicator = SemanticDeduplicator(
            ttl_s=config.semantic_dedup_ttl_s,
            max_entries=config.semantic_dedup_capacity,
        )
        self.deferred = DeferredMessageQueue()
        self.feedback = DecisionFeedbackStore()
        self.telemetry = MessageTopologyTelemetry()
        self.object_store = object_store
        self._intents: dict[tuple[str, str, str], EdgeCandidate] = {}

    def register_communication_intent(self, message: TaskMessage) -> None:
        """A real context request becomes a task-relevant candidate edge."""
        if message.receiver == "coordinator" or message.sender == message.receiver:
            return
        self._intents[(message.sender, message.receiver, message.task_id)] = EdgeCandidate(
            source=message.sender,
            target=message.receiver,
            task_ids=frozenset({message.task_id}),
            score=message.priority / 10,
            required=message.priority >= self.topology.config.high_risk_priority,
            high_risk=message.priority >= self.topology.config.high_risk_priority,
            reason="context_demand",
        )

    def rebuild_topology(
        self,
        *,
        online_agents: Iterable[str],
        task_dependencies: Mapping[str, Sequence[str]],
        assignments: Mapping[str, str | None],
        task_priorities: Mapping[str, int],
        agent_reliability: Mapping[str, float] | None = None,
        agent_latency_scores: Mapping[str, float] | None = None,
        task_information_values: Mapping[str, float] | None = None,
        standby_assignments: Mapping[str, Sequence[str]] | None = None,
        changed_agents: Iterable[str] = (),
        changed_task_ids: Iterable[str] = (),
        now: float | None = None,
    ) -> RebuildResult:
        return self.topology.rebuild(
            online_agents=online_agents,
            task_dependencies=task_dependencies,
            assignments=assignments,
            task_priorities=task_priorities,
            agent_reliability=agent_reliability,
            agent_latency_scores=agent_latency_scores,
            task_information_values=task_information_values,
            standby_assignments=standby_assignments,
            extra_candidates=tuple(self._intents.values()),
            changed_agents=changed_agents,
            changed_task_ids=changed_task_ids,
            now=now,
        )

    def prepare_context(self, candidate: TaskMessage, *, age_s: float = 0.0, now: float | None = None) -> ContextDeliveryPlan:
        """Return a publish plan. Call ``mark_delivered`` only after publish succeeds."""
        if candidate.receiver != "coordinator" and not self._edge_active(candidate):
            return ContextDeliveryPlan(PolicyAction.DROP, "topology_pruned", None)
        delta = self.delta_builder.build(candidate, self.digests.get(candidate.receiver, candidate.task_id))
        if delta.message is None:
            return ContextDeliveryPlan(PolicyAction.DROP, "receiver_already_knows", None, delta=delta)
        dedup = self.deduplicator.check(delta.message, now=now)
        if dedup.duplicate:
            return ContextDeliveryPlan(PolicyAction.DROP, f"duplicate_{dedup.reason}", None, delta=delta, dedup=dedup)
        impact = self.impact_estimator.assess(delta.message)
        decision = self.policy.decide(delta.message, impact, age_s=age_s, queue_depth=len(self.deferred))
        current_trace = TRACE_CONTEXTS.get(delta.message.message_id)
        TRACE_CONTEXTS.register(
            delta.message.message_id,
            task_id=delta.message.task_id,
            parent_message_id=(
                current_trace.parent_message_id if current_trace is not None else candidate.message_id
            ),
            token_budget=self.policy.config.token_budget_per_message,
        )
        timestamp = time.monotonic() if now is None else now
        if decision.action is PolicyAction.MERGE:
            trace = TRACE_CONTEXTS.get(delta.message.message_id)
            TRACE_CONTEXTS.register(
                delta.message.message_id,
                task_id=delta.message.task_id,
                parent_message_id=trace.parent_message_id if trace is not None else candidate.message_id,
                queued_at=time.time(),
            )
            self.deferred.merge(delta.message, now=timestamp)
            return ContextDeliveryPlan(decision.action, decision.reason, None, delta, impact, dedup, decision)
        if decision.action is PolicyAction.DEFER:
            trace = TRACE_CONTEXTS.get(delta.message.message_id)
            TRACE_CONTEXTS.register(
                delta.message.message_id,
                task_id=delta.message.task_id,
                parent_message_id=trace.parent_message_id if trace is not None else candidate.message_id,
                queued_at=time.time(),
            )
            self.deferred.defer(delta.message, now=timestamp)
            return ContextDeliveryPlan(decision.action, decision.reason, None, delta, impact, dedup, decision)
        if decision.action is PolicyAction.DROP:
            return ContextDeliveryPlan(decision.action, decision.reason, None, delta, impact, dedup, decision)
        return ContextDeliveryPlan(decision.action, decision.reason, delta.message, delta, impact, dedup, decision)

    def mark_delivered(self, message: TaskMessage, *, now: float | None = None) -> None:
        self.deduplicator.record(message, now=now)
        self.digests.record_delivered(message, now=now)

    def report_decision_feedback(self, event: DecisionFeedbackEvent) -> bool:
        return self.feedback.report(event)

    def store_evidence(
        self,
        evidence_id: str,
        data: bytes,
        *,
        note: str | None = None,
        media_type: str = "application/octet-stream",
    ) -> EvidenceRef:
        if self.object_store is None:
            raise RuntimeError("no object store adapter is configured")
        stored = self.object_store.put(data, media_type=media_type)
        return EvidenceRef(evidence_id, stored.uri, note)

    def drain_deferred(self, *, now: float | None = None) -> tuple[ContextDeliveryPlan, ...]:
        """End one merge window and return valid messages for publication."""
        timestamp = time.monotonic() if now is None else now
        plans: list[ContextDeliveryPlan] = []
        for message, queued_at in self.deferred.pop_all():
            age_s = timestamp - queued_at
            if age_s >= message.ttl:
                plans.append(ContextDeliveryPlan(
                    PolicyAction.DROP, "ttl_expired", None, queue_wait_ms=age_s * 1000
                ))
            else:
                wait_ms = age_s * 1000
                self.telemetry.queue_wait_ms.observe(wait_ms)
                plans.append(ContextDeliveryPlan(
                    PolicyAction.SEND, "merge_window_elapsed", message, queue_wait_ms=wait_ms
                ))
        return tuple(plans)

    def get_snapshot(self):
        return self.topology.get_snapshot()

    def _edge_active(self, message: TaskMessage) -> bool:
        return any(
            edge.source == message.sender
            and edge.target == message.receiver
            and message.task_id in edge.task_ids
            for edge in self.topology.get_snapshot().edges
        )
