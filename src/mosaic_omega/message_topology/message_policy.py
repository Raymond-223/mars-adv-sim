"""TTL, priority, budget, and merge decisions for low-entropy messages."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from enum import Enum
import uuid

from ..agent_runtime.task_messages import ConstraintDelta, EvidenceRef, FactDelta, TaskMessage
from .decision_impact import ImpactAssessment


class PolicyAction(str, Enum):
    SEND = "SEND"
    DEFER = "DEFER"
    MERGE = "MERGE"
    DROP = "DROP"


@dataclass(frozen=True)
class MessagePolicyConfig:
    max_queue_size: int = 32
    token_budget_per_message: int = 256
    force_send_priority: int = 8

    def __post_init__(self) -> None:
        if self.max_queue_size < 1 or self.token_budget_per_message < 1:
            raise ValueError("queue size and token budget must be positive")
        if not 1 <= self.force_send_priority <= 10:
            raise ValueError("force_send_priority must be within [1, 10]")


@dataclass(frozen=True)
class PolicyDecision:
    action: PolicyAction
    reason: str
    estimated_tokens: int


def estimate_tokens(message: TaskMessage) -> int:
    """Stable pre-LLM estimate; replaced by tokenizer measurements later."""
    text = message.summary or ""
    text += "".join(item.text or "" for item in message.facts)
    text += "".join(item.text or "" for item in message.constraints)
    text += "".join(item.artifact_id + (item.note or "") for item in message.evidence_refs)
    # JSON punctuation and protocol fields also consume tokens in later prompts.
    return max(1, (len(text) + 3) // 4 + 12)


class MessagePolicy:
    def __init__(self, config: MessagePolicyConfig | None = None) -> None:
        self.config = config or MessagePolicyConfig()

    def decide(
        self,
        message: TaskMessage,
        impact: ImpactAssessment,
        *,
        age_s: float = 0.0,
        queue_depth: int = 0,
    ) -> PolicyDecision:
        tokens = estimate_tokens(message)
        if age_s >= message.ttl:
            return PolicyDecision(PolicyAction.DROP, "ttl_expired", tokens)
        if impact.critical or message.priority >= self.config.force_send_priority:
            return PolicyDecision(PolicyAction.SEND, "critical_information", tokens)
        if queue_depth >= self.config.max_queue_size:
            return PolicyDecision(PolicyAction.DROP, "queue_full_low_impact", tokens)
        if queue_depth > 0 and impact.score < 0.6:
            return PolicyDecision(PolicyAction.MERGE, "coalesce_low_impact", tokens)
        if tokens > self.config.token_budget_per_message:
            return PolicyDecision(PolicyAction.DEFER, "over_message_budget", tokens)
        return PolicyDecision(PolicyAction.SEND, "within_budget", tokens)


def merge_task_messages(previous: TaskMessage, incoming: TaskMessage) -> TaskMessage:
    """Coalesce one sender/receiver/task stream without changing wire fields."""
    if (previous.sender, previous.receiver, previous.task_id) != (incoming.sender, incoming.receiver, incoming.task_id):
        raise ValueError("only one sender/receiver/task stream may be merged")
    facts: OrderedDict[str, FactDelta] = OrderedDict((item.id, item) for item in previous.facts)
    constraints: OrderedDict[str, ConstraintDelta] = OrderedDict((item.id, item) for item in previous.constraints)
    evidence: OrderedDict[str, EvidenceRef] = OrderedDict((item.id, item) for item in previous.evidence_refs)
    for item in incoming.facts:
        facts[item.id] = item
    for item in incoming.constraints:
        constraints[item.id] = item
    for item in incoming.evidence_refs:
        evidence[item.id] = item
    return TaskMessage.create(
        message_id=uuid.uuid4().hex,
        sender=incoming.sender,
        receiver=incoming.receiver,
        task_id=incoming.task_id,
        summary=incoming.summary if incoming.summary is not None else previous.summary,
        facts=tuple(facts.values()),
        constraints=tuple(constraints.values()),
        evidence_refs=tuple(evidence.values()),
        priority=max(previous.priority, incoming.priority),
        ttl=min(previous.ttl, incoming.ttl),
    )


class DeferredMessageQueue:
    """Bounded, merge-by-stream queue; publication remains the caller's job."""

    def __init__(self) -> None:
        self._pending: OrderedDict[tuple[str, str, str], tuple[TaskMessage, float]] = OrderedDict()

    def __len__(self) -> int:
        return len(self._pending)

    def defer(self, message: TaskMessage, *, now: float) -> None:
        key = (message.sender, message.receiver, message.task_id)
        pending = self._pending.get(key)
        if pending is None:
            self._pending[key] = (message, now)
            return
        existing, queued_at = pending
        self._pending[key] = (merge_task_messages(existing, message), queued_at)

    def merge(self, message: TaskMessage, *, now: float) -> None:
        self.defer(message, now=now)

    def pop_all(self) -> tuple[tuple[TaskMessage, float], ...]:
        messages = tuple(self._pending.values())
        self._pending.clear()
        return messages
