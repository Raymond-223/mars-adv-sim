"""Single authoritative MOSAIC-Ω V3 execution chain.

Ownership follows the project handbook and the compliant submodule handoffs:

UserGoal -> GoalSpec -> ToDAG -> ExecutionScheduler/EventStore ->
Memory Context + MessageTopology -> Agent/ToolRuntime -> Evidence/Verification ->
Recovery when needed -> Observability/Evidence Manifest.

There is deliberately no second task-state authority.  All new runs are owned by
``ExecutionSchedulerService`` and projected to other modules through explicit contracts.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import uuid
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from ..agent_runtime.edge_cloud import ExecutionTier
from ..agent_runtime.models import AgentProfile
from ..agent_runtime.task_messages import ConstraintDelta, DeltaOperation, EvidenceRef, FactDelta, TaskMessage
from ..agent_runtime.trace_context import TRACE_CONTEXTS, business_content_hash
from ..execution_scheduler.adapters.deepseek_agent import DeepSeekAgent
from ..execution_scheduler.adapters.local_agent import LocalDeterministicAgent, RequirementBaselineAgent
from ..execution_scheduler.adapters.mqtt_agent import AgentRpcClient, MqttAgentAdapter
from ..execution_scheduler.adapters.tool_planning_agent import ToolPlanningAgent
from ..execution_scheduler.config import Settings
from ..execution_scheduler.models import (
    ActorKind,
    Assignment,
    CapabilityProfile,
    ErrorClass,
    TaskNodeView,
    TaskState,
)
from ..execution_scheduler.service import ExecutionSchedulerService
from ..goalspec import compile_goal
from ..memory_recovery import MemoryService
from ..message_topology import MessageTopologyService
from ..observability import ObservabilityRuntime
from ..todag import ToDAGEngine
from ..todag.agents import DecompositionAgent
from .contracts import (
    canonical_goalspec,
    event_contract,
    evidence_manifest,
    execution_event_to_memory,
    message_envelope_contract,
    tasknode_contract,
    topology_snapshot_contract,
    verification_results,
)
from .registry_bridge import DynamicRegistrySchedulerBridge

try:
    from dotenv import load_dotenv
except ImportError:  # optional convenience dependency
    load_dotenv = None  # type: ignore[assignment]


@dataclass
class MainChainRunResult:
    run_id: str
    runtime_metadata: dict[str, Any]
    goalspec: dict[str, Any]
    canonical_goalspec: dict[str, Any]
    taskgraph: dict[str, Any]
    capability_profiles: list[dict[str, Any]]
    tasks: list[dict[str, Any]]
    events: list[dict[str, Any]]
    verification_results: list[dict[str, Any]]
    evidence_manifest: list[dict[str, Any]]
    topology_snapshot: dict[str, Any]
    topology_history: list[dict[str, Any]]
    communication: list[dict[str, Any]]
    context_packs: dict[str, dict[str, Any]]
    memory_metrics: dict[str, float]
    completed_task_ids: list[str]
    all_succeeded: bool
    scheduler_policies: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "runtime_metadata": self.runtime_metadata,
            "goalspec": self.goalspec,
            "canonical_goalspec": self.canonical_goalspec,
            "taskgraph": self.taskgraph,
            "capability_profiles": self.capability_profiles,
            "tasks": self.tasks,
            "events": self.events,
            "verification_results": self.verification_results,
            "evidence_manifest": self.evidence_manifest,
            "topology_snapshot": self.topology_snapshot,
            "topology_history": self.topology_history,
            "communication": self.communication,
            "context_packs": self.context_packs,
            "memory_metrics": self.memory_metrics,
            "completed_task_ids": self.completed_task_ids,
            "all_succeeded": self.all_succeeded,
            "scheduler_policies": self.scheduler_policies,
        }



class MosaicMainChain:
    """Facade for one end-to-end, replayable MOSAIC-Ω run."""

    def __init__(
        self,
        *,
        workspace: str | Path = ".mosaic_workspace",
        scheduler_policy: str = "ortools",
        execution: ExecutionSchedulerService | None = None,
        memory: MemoryService | None = None,
        topology: MessageTopologyService | None = None,
        observability: ObservabilityRuntime | None = None,
    ) -> None:
        workspace = Path(workspace).resolve()
        if load_dotenv is not None:
            load_dotenv()
        # Start from the process environment so every documented knob
        # (SCHEDULER_WEIGHT_*, MAX_TASK_RETRIES, SCHEDULER_ALLOW_FALLBACK,
        # AGENT_POOL_*, PROVIDER_MAX_CONCURRENCY, ...) actually reaches the
        # runtime.  Passing only a literal dict made ``.env`` configuration
        # silently inert for every main-chain run.  Chain-owned values are
        # applied last and still win.
        settings = Settings.from_env({
            **os.environ,
            "EXECUTION_WORKSPACE": str(workspace),
            "SCHEDULER_POLICY": scheduler_policy,
            "ALLOWED_COMMANDS": f"python,python3,python.exe,{Path(sys.executable).name}",
            "TOOL_TIMEOUT_S": os.getenv("TOOL_TIMEOUT_S", "30"),
            "EVENT_SNAPSHOT_INTERVAL": os.getenv("EVENT_SNAPSHOT_INTERVAL", "100"),
            "EXECUTION_SCHEMA_VERSION": os.getenv("EXECUTION_SCHEMA_VERSION", "0.1"),
        })
        # Product runs default to stdlib SQLite so EventStore/task/idempotency
        # authority survives process restarts without requiring Docker/PostgreSQL.
        # Tests may still inject ExecutionSchedulerService.memory(settings), and
        # server deployment may inject the PostgreSQL-backed service.
        self.execution = execution or ExecutionSchedulerService.sqlite(settings)
        self.registry_bridge = DynamicRegistrySchedulerBridge(self.execution)
        self.memory = memory or MemoryService()
        self.topology = topology or MessageTopologyService()
        self.observability = observability or ObservabilityRuntime(workspace / "observability")
        self._synced_event_ids: set[str] = set()
        self._communication: list[dict[str, Any]] = []
        self._communication_decisions: list[dict[str, Any]] = []
        self._context_packs: dict[str, dict[str, Any]] = {}
        self._topology_history: list[dict[str, Any]] = []
        self._active_run_id: str | None = None
        # READY DAG nodes can prepare context concurrently.  The capture gate must
        # be atomic; otherwise multiple worker threads can all observe the same
        # stale throttle timestamp and each rebuild/write a multi-megabyte dashboard
        # snapshot.  That race made long runs progressively slower.
        self._observe_lock = threading.RLock()

    @property
    def communication_log(self) -> tuple[dict[str, Any], ...]:
        return tuple(self._communication)

    @property
    def communication_decision_log(self) -> tuple[dict[str, Any], ...]:
        return tuple(self._communication_decisions)

    def _observe(self, run_id: str, phase: str, *, force: bool = False) -> dict[str, Any]:
        """Refresh the read-only dashboard projection from authoritative services.

        The throttle check and capture are one critical section.  This matters
        because independent READY tasks call ``prepare_agent_context`` on worker
        threads; without an atomic gate they could all rebuild the same large
        snapshot simultaneously.
        """
        with self._observe_lock:
            if not self.observability.should_capture(force=force):
                return {}
            topology = topology_snapshot_contract(self.topology.get_snapshot())
            return self.observability.capture(
                run_id=run_id,
                phase=phase,
                tasks=self.execution.events.tasks(run_id),
                events=self.execution.events.events(run_id=run_id),
                capabilities=self.execution.capabilities.list(),
                communication=self._communication,
                communication_decisions=self._communication_decisions,
                context_packs=self._context_packs,
                memory_records=self.memory.observability_records(run_id),
                memory_metrics=self.memory.export_metrics(),
                topology_snapshot=topology,
                topology_telemetry=self.topology.telemetry.snapshot(),
                # The full version history is what makes topology *dynamic* in the
                # console: without it only the final graph could ever be shown.
                topology_history=list(self._topology_history),
            )

    # Resource pools are declared once so DEVICE/EDGE/CLOUD are genuinely
    # separate capacity, latency, energy and privacy domains that the solver
    # chooses between, not a label stamped on a single machine.
    _RESOURCE_POOLS: tuple[dict[str, Any], ...] = (
        {
            "actor_id": "pool-device", "tier": "device", "base_capacity": 2,
            "latency_ms": 5.0, "energy_cost": 0.2, "reliability": 0.99,
            "allowed_privacy_levels": ["normal", "public", "internal", "confidential", "restricted", "secret"],
            "description": "on-device execution; no network egress; smallest capacity",
        },
        {
            "actor_id": "pool-edge", "tier": "edge", "base_capacity": 4,
            "latency_ms": 25.0, "energy_cost": 0.5, "reliability": 0.97,
            "allowed_privacy_levels": ["normal", "public", "internal", "confidential"],
            "description": "edge node execution; low latency; rejects restricted/secret data",
        },
        {
            "actor_id": "pool-cloud", "tier": "cloud", "base_capacity": 8,
            "latency_ms": 80.0, "energy_cost": 1.0, "reliability": 0.95,
            "allowed_privacy_levels": ["normal", "public", "internal"],
            "description": "cloud provider execution; highest capacity; rejects confidential and above",
        },
    )

    def _register_resource_pools(self, *, agent_count: int) -> None:
        for pool in self._RESOURCE_POOLS:
            capacity = pool["base_capacity"]
            if pool["tier"] == "cloud":
                capacity = max(capacity, agent_count * 2)
            self.execution.register_actor(CapabilityProfile(
                actor_id=pool["actor_id"], kind=ActorKind.DEVICE, task_types=frozenset({"*"}),
                capabilities=frozenset({"*"}), permissions=frozenset({"*"}),
                reliability=pool["reliability"], latency_ms=pool["latency_ms"],
                energy_cost=pool["energy_cost"], capacity=capacity,
                device_location=pool["tier"],
                metadata={
                    "tier": pool["tier"],
                    "allowed_privacy_levels": list(pool["allowed_privacy_levels"]),
                    "pool_description": pool["description"],
                    "capacity_source": "declared_resource_pool",
                },
            ))

    #: Baseline delivery kinds per role, used when the caller does not supply the
    #: kinds actually present in the compiled DAG.
    _DEFAULT_DELIVERY_KINDS: dict[str, tuple[str, ...]] = {
        "code": ("software", "document", "reasoning"),
        "robotics": ("robotics", "document", "reasoning"),
        "data": ("data", "document", "research", "reasoning"),
        "calculation": ("data", "document", "reasoning"),
        "search": ("research", "document", "reasoning"),
        "analysis": ("research", "document", "data", "reasoning"),
        "report": ("document", "research", "reasoning"),
        "review": ("verification", "document", "reasoning"),
        "plan": ("document", "reasoning"),
    }

    def _delivery_kinds_for(
        self, skill: str, observed: Mapping[str, set[str]] | None = None
    ) -> tuple[str, ...]:
        """Which delivery kinds a role Agent is allowed to act on.

        A role can legitimately serve more than one kind (a ``code`` Agent also
        writes design docs), but never all of them: that is what keeps the
        permission scope a meaningful routing constraint.

        The kinds a role must cover are taken from the compiled DAG when it is
        available.  A purely static table would under-grant, because ToDAG picks a
        node's delivery kind from its text ("...并通过测试" is software work) while
        the skill guess can land on a different role, leaving the task with no
        permitted executor at all.
        """
        kinds = set(self._DEFAULT_DELIVERY_KINDS.get(skill, ("document", "research", "reasoning")))
        if observed:
            kinds.update(observed.get(skill, ()))
        return tuple(sorted(kinds))

    @staticmethod
    def _permissions_for(kinds: Sequence[str]) -> tuple[str, ...]:
        table = DecompositionAgent.DELIVERY_PERMISSIONS
        granted = {perm for kind in kinds for perm in table.get(kind, ())}
        return tuple(sorted(granted)) or ("file.write",)

    def _pool_size(self, skill: str, demand: int) -> int:
        """How many instances of one role the pool should hold.

        The rule is demand-driven with a floor: a role that is actually needed
        always gets at least ``AGENT_POOL_MIN_INSTANCES`` instances so same-role
        Agents can compete, run in parallel and cover for each other.  A single
        instance per role is what made the previous build a static pipeline.
        """
        settings = self.execution.settings
        # One instance per concurrently-needed task of that role, floored at the
        # configured minimum and capped so a huge DAG cannot open an unbounded
        # number of provider connections.  Keeping the floor above 1 is the point:
        # it guarantees there is always a second same-role Agent to compete with,
        # take over from, or run alongside the first.
        return max(settings.agent_pool_min_instances, min(settings.agent_pool_max_instances, max(1, demand)))

    def register_default_deepseek_resources(
        self,
        required_skills: Iterable[str],
        delivery_kinds_by_skill: Mapping[str, set[str]] | None = None,
    ) -> None:
        """Build the heterogeneous Agent pool and the end-edge-cloud resource pools.

        New custom runs consume ``MOSAIC_AGENT_CONFIG_PATH``.  Enabled templates
        affect real scheduling (skills, permissions, tier, max load and requested
        model).  Every required role is then instantiated as *multiple* competing
        Agent instances, plus a standby, so the group is a real pool rather than
        one Agent per role.
        """
        if load_dotenv is None:
            raise RuntimeError('缺少 python-dotenv，请运行：py -m pip install -e "[deepseek]"')
        load_dotenv()
        raw_skills = [str(skill).strip() for skill in required_skills if str(skill).strip()]
        demand = Counter(raw_skills)
        skills = sorted(demand) or ["general"]
        if not demand:
            demand = Counter({"general": 1})
        existing = {profile.actor_id for profile in self.execution.capabilities.list()}
        provider_id = os.getenv("MOSAIC_PROVIDER", "deepseek").strip() or "deepseek"
        provider_slug = "".join(ch for ch in provider_id.casefold() if ch.isalnum() or ch in "-_") or "provider"
        base_url = (os.getenv("OPENAI_BASE_URL") or os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")).rstrip("/")
        model_name = (os.getenv("LLM_MODEL_NAME") or os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")).strip() or "deepseek-v4-flash"

        configured: list[dict[str, Any]] = []
        config_path = os.getenv("MOSAIC_AGENT_CONFIG_PATH", "").strip()
        if config_path:
            try:
                raw = json.loads(Path(config_path).read_text(encoding="utf-8"))
                if isinstance(raw, list):
                    configured = [dict(x) for x in raw if isinstance(x, dict) and bool(x.get("enabled", True))]
            except (OSError, ValueError, TypeError):
                configured = []

        pool_members: list[str] = []
        covered_skills: set[str] = set()
        for row in configured:
            agent_id = str(row.get("agent_id") or "").strip()
            row_skills = tuple(str(x).strip() for x in row.get("skills", ()) if str(x).strip())
            if not agent_id or not row_skills:
                continue
            # Do not instantiate irrelevant templates on every run. A wildcard is
            # intentionally allowed for user-created generalists.
            relevant = "*" in row_skills or bool(set(row_skills) & set(skills))
            if not relevant:
                continue
            tier_raw = str(row.get("tier") or "cloud").casefold()
            try:
                tier = ExecutionTier(tier_raw)
            except ValueError:
                continue
            runtime_profile = AgentProfile(
                agent_id=agent_id,
                name=str(row.get("name") or agent_id),
                skills=row_skills,
                endpoint=f"api+{base_url}",
                max_load=max(1, int(row.get("max_load", 1))),
                reliability=0.95,
                tier=tier,
                labels=(provider_id, "real-api", "agent-studio", str(row.get("role") or "generalist")),
            )
            adapter = DeepSeekAgent(
                runtime_profile.agent_id,
                role=str(row.get("role") or "generalist"),
                context_provider=self.prepare_agent_context,
            )
            requested_model = str(row.get("model") or "").strip()
            if requested_model:
                adapter.set_model(requested_model)
            self.registry_bridge.register(
                runtime_profile,
                permissions=tuple(str(x) for x in row.get("permissions", ("*",))) or ("*",),
                adapter=adapter,
            )
            covered_skills.update(skills if "*" in row_skills else (set(row_skills) & set(skills)))
            existing.add(agent_id)
            pool_members.append(agent_id)

        # Automatic role Agents fill required capabilities not covered by the
        # user's Agent Studio templates.  Each role becomes several independent
        # instances with their own identity, load, queue and context.
        for skill in skills:
            if skill in covered_skills:
                continue
            if skill == "requirement_baseline":
                # Served deterministically on-device; the multi-skill standby
                # below remains the failover if that Agent goes offline.
                continue
            instances = self._pool_size(skill, demand.get(skill, 1))
            for index in range(1, instances + 1):
                actor_id = f"agent-{provider_slug}-{skill}-{index:02d}"
                runtime_profile = AgentProfile(
                    agent_id=actor_id,
                    name=f"{provider_id} {skill} agent #{index}",
                    skills=(skill,),
                    endpoint=f"api+{base_url}",
                    # One concurrent task per instance: parallelism comes from
                    # having several instances, which is what makes them visible
                    # as a competing group instead of one saturated worker.
                    max_load=1,
                    reliability=0.95,
                    tier=ExecutionTier.CLOUD,
                    labels=(provider_id, "real-api", "main-chain", "role-pool", skill, f"instance-{index:02d}"),
                )
                kinds = self._delivery_kinds_for(skill, delivery_kinds_by_skill)
                self.registry_bridge.register(
                    runtime_profile,
                    # Scoped, not "*": a report Agent cannot run a build, so the
                    # scheduler's permission filter is a real routing constraint
                    # and a mis-routed task fails closed at ToolRuntime.
                    permissions=self._permissions_for(kinds),
                    # Later instances cost marginally more, so the solver has a
                    # deterministic preference order while every instance stays
                    # genuinely selectable when earlier ones are busy or offline.
                    fixed_cost=0.05 * (index - 1),
                    adapter=ToolPlanningAgent(
                        actor_id, role=skill, delivery_kinds=kinds,
                        context_provider=self.prepare_agent_context,
                    ),
                    metadata={"delivery_kinds": list(kinds)},
                )
                existing.add(actor_id)
                pool_members.append(actor_id)

        # Multi-skill standby: only reached when the primary role pool is
        # saturated or offline, which is exactly the failover path the console
        # visualizes.
        standby_id = f"agent-{provider_slug}-generalist-standby-01"
        runtime_profile = AgentProfile(
            agent_id=standby_id, name=f"{provider_id} multi-skill standby",
            skills=tuple(skills), endpoint=f"api+{base_url}", max_load=2, reliability=0.90,
            tier=ExecutionTier.CLOUD, labels=(provider_id, "real-api", "main-chain", "standby"),
        )
        standby_kinds = tuple(sorted({
            kind for skill in skills
            for kind in self._delivery_kinds_for(skill, delivery_kinds_by_skill)
        }))
        self.registry_bridge.register(
            runtime_profile,
            # The standby covers every role in the pool, so it holds the union of
            # their permissions — that is exactly what makes it a usable failover.
            permissions=self._permissions_for(standby_kinds),
            fixed_cost=5.0,
            adapter=ToolPlanningAgent(
                standby_id, role="generalist", delivery_kinds=standby_kinds,
                context_provider=self.prepare_agent_context,
            ),
            metadata={"delivery_kinds": list(standby_kinds)},
        )
        existing.add(standby_id)
        pool_members.append(standby_id)

        # Device-tier Agents. These never contact a provider, so they are the
        # only Agents allowed to touch restricted/secret work packages.
        requirement_agent_id = "agent-local-requirement-compiler"
        self.registry_bridge.register(
            AgentProfile(
                agent_id=requirement_agent_id, name="on-device requirement compiler",
                skills=("requirement_baseline",), endpoint="local://requirement-compiler", max_load=2,
                reliability=0.99, tier=ExecutionTier.DEVICE,
                labels=("local", "deterministic", "device-tier", "requirement-baseline"),
            ),
            permissions=("*",),
            adapter=RequirementBaselineAgent(
                requirement_agent_id, context_provider=self.prepare_agent_context
            ),
        )
        existing.add(requirement_agent_id)
        pool_members.append(requirement_agent_id)

        privacy_agent_id = "agent-local-private-executor-01"
        self.registry_bridge.register(
            AgentProfile(
                agent_id=privacy_agent_id, name="on-device private executor",
                skills=tuple(skills), endpoint="local://private-executor", max_load=2,
                reliability=0.92, tier=ExecutionTier.DEVICE,
                labels=("local", "deterministic", "device-tier", "privacy-restricted"),
            ),
            permissions=("*",),
            # Costlier than a cloud Agent so it is chosen for privacy reasons,
            # not by accident, while remaining the only legal option once the
            # cloud/edge pools reject the task's privacy level.
            fixed_cost=2.0,
            adapter=LocalDeterministicAgent(
                privacy_agent_id, role="private-executor", context_provider=self.prepare_agent_context
            ),
            metadata={
                "only_privacy_levels": ["restricted", "secret"],
                "scheduling_policy": (
                    "reserved for privacy-restricted work packages; it must not absorb "
                    "unrestricted overflow in place of a model-backed Agent"
                ),
            },
        )
        existing.add(privacy_agent_id)
        pool_members.append(privacy_agent_id)

        provider_model_id = f"{provider_slug}-model"
        if provider_model_id not in existing:
            try:
                context_limit = int(os.getenv("DEEPSEEK_CONTEXT_LIMIT", "64000"))
                cost_per_token = float(os.getenv("DEEPSEEK_COST_PER_TOKEN", "0"))
            except ValueError as exc:
                raise ValueError("DEEPSEEK_CONTEXT_LIMIT and DEEPSEEK_COST_PER_TOKEN must be numeric") from exc
            self.execution.register_actor(CapabilityProfile(
                actor_id=provider_model_id, kind=ActorKind.MODEL, task_types=frozenset({"*"}),
                capabilities=frozenset({"*"}), permissions=frozenset({"*"}), reliability=0.95,
                context_limit=max(1, context_limit), cost_per_token=max(0.0, cost_per_token),
                metadata={"provider": provider_id, "model": model_name, "base_url": base_url, "pricing": "configured_estimate"},
            ))
            existing.add(provider_model_id)
        if "task" not in existing:
            self.execution.register_actor(CapabilityProfile(
                actor_id="task", kind=ActorKind.TOOL, task_types=frozenset({"*"}),
                capabilities=frozenset({"*"}), permissions=frozenset({"*"}), reliability=0.99,
            ))
            existing.add("task")
        self._register_resource_pools(agent_count=len(pool_members))
        self.execution.events.append_event(
            "AGENT_POOL_REGISTERED",
            self._active_run_id or "bootstrap",
            actor_id="agent-pool-builder",
            payload={
                "required_skill_demand": dict(demand),
                "instances_per_role": {
                    skill: self._pool_size(skill, demand.get(skill, 1))
                    for skill in skills if skill not in covered_skills
                },
                "agent_ids": sorted(pool_members),
                "agent_count": len(pool_members),
                "resource_pools": [pool["actor_id"] for pool in self._RESOURCE_POOLS],
                "rule": (
                    "each required role is instantiated at least AGENT_POOL_MIN_INSTANCES times so "
                    "same-role Agents compete for work; device-tier Agents are deterministic and "
                    "handle privacy-restricted packages without network egress"
                ),
            },
        )

    def replace_model(self, actor_id: str, new_model_id: str) -> None:
        """Replace the model that the bound adapter will actually request.

        Metadata-only replacement is forbidden because it can make the console
        claim a model switch that never reaches the execution backend.
        """
        value = str(new_model_id).strip()
        if not value:
            raise ValueError("new_model_id must not be empty")
        profile = self.execution.capabilities.get(actor_id)
        if profile is None:
            raise KeyError(actor_id)
        adapter = self.execution.agents.get(actor_id)
        setter = getattr(adapter, "set_model", None)
        if adapter is None or not callable(setter):
            raise RuntimeError(
                f"actor {actor_id!r} has no model-switchable execution adapter; "
                "refusing metadata-only replacement"
            )
        setter(value)
        metadata = dict(profile.metadata or {})
        metadata["model_id"] = value
        metadata["model_switch_verified"] = True
        metadata["model_switch_source"] = f"{adapter.__class__.__module__}.{adapter.__class__.__qualname__}.set_model"
        metadata["model_switch_at"] = time.time()
        self.execution.register_actor(replace(profile, metadata=metadata), adapter=adapter)

    def add_agent(
        self,
        profile: AgentProfile,
        permissions: Sequence[str] = ("*",),
        *,
        adapter: Any | None = None,
        latency_ms: float | None = None,
    ) -> CapabilityProfile:
        """Register an Agent only when its concrete execution adapter is explicit.

        The old implementation silently attached ``MemoryTopologyMockAgent`` to
        arbitrary profiles.  That behaviour is intentionally rejected here.
        """
        if adapter is None:
            raise ValueError(
                "add_agent requires an explicit execution adapter; hidden MockAgent fallback is forbidden"
            )
        return self.registry_bridge.register(
            profile,
            permissions=permissions,
            adapter=adapter,
            latency_ms=latency_ms,
        )

    def remove_agent(self, actor_id: str) -> CapabilityProfile:
        """Dynamically set an agent offline and trigger topology reconfiguration."""
        return self.registry_bridge.offline(actor_id)


    def register_mqtt_agent(
        self,
        profile: AgentProfile,
        *,
        rpc: AgentRpcClient,
        permissions: Sequence[str] = ("*",),
        latency_ms: float | None = None,
        timeout_s: float = 20.0,
        fixed_cost: float = 0.0,
    ) -> CapabilityProfile:
        """Register a real remote MQTT Agent without creating a second scheduler path.

        The remote process may only *plan* ToolCall objects.  Calls are executed
        by the same local authoritative ToolRuntime used by in-process agents,
        preserving permission checks, idempotency, evidence generation and
        append-only events.
        """
        return self.registry_bridge.register(
            profile,
            permissions=permissions,
            latency_ms=latency_ms,
            adapter=MqttAgentAdapter(profile.agent_id, rpc, timeout_s=timeout_s),
            fixed_cost=fixed_cost,
        )

    def _sync_topology(self, run_id: str, *, changed_task_ids: Iterable[str] = ()) -> dict[str, Any]:
        tasks = self.execution.events.tasks(run_id)
        dependencies = {task.task_id: tuple(task.depends_on) for task in tasks}
        assignments = {
            task.task_id: (task.assignment.agent_id if task.assignment is not None else None)
            for task in tasks
        }
        priorities = {task.task_id: task.priority for task in tasks}
        agents = self.execution.capabilities.list(ActorKind.AGENT)
        online_agents = [profile.actor_id for profile in agents if profile.online]
        reliability = {profile.actor_id: profile.reliability for profile in agents}
        latency_score = {
            profile.actor_id: 1.0 / (1.0 + max(0.0, profile.latency_ms) / 100.0)
            for profile in agents
        }
        result = self.topology.rebuild_topology(
            online_agents=online_agents,
            task_dependencies=dependencies,
            assignments=assignments,
            task_priorities=priorities,
            agent_reliability=reliability,
            agent_latency_scores=latency_score,
            changed_task_ids=tuple(changed_task_ids),
        )
        contract = topology_snapshot_contract(result.snapshot)
        previous = self._topology_history[-1] if self._topology_history else None
        changed = bool(result.added or result.removed) or previous is None or previous.get("version") != contract.get("version")
        # Only distinct versions enter the replay timeline. Appending on every
        # sync produced runs of identical frames, which made scrubbing the
        # topology look stuck even though the graph really had changed.
        if changed:
            self._topology_history.append(contract)
        if changed:
            self.execution.events.append_event(
                "TOPOLOGY_REBUILT",
                run_id,
                actor_id="topology-service",
                payload={
                    "version": contract.get("version"),
                    "previous_version": previous.get("version") if previous else None,
                    "node_count": len(contract.get("nodes", ())),
                    "edge_count": len(contract.get("edges", ())),
                    "added_edges": [f"{edge.source}->{edge.target}" for edge in result.added],
                    "removed_edges": [f"{edge.source}->{edge.target}" for edge in result.removed],
                    "affected_agents": sorted(result.affected_agents),
                    "affected_task_ids": sorted(result.affected_task_ids),
                    "full_rebuild": bool(result.full_rebuild),
                    "lambda2": contract.get("lambda2_or_connectivity"),
                    "connected": contract.get("connected"),
                    "reason": "dynamic topology recomputed from online registry and task assignments",
                },
            )
        return contract

    @staticmethod
    def _constraint_messages(values: Sequence[str]) -> tuple[ConstraintDelta, ...]:
        return tuple(
            ConstraintDelta(f"constraint-{index}", DeltaOperation.REPLACE, str(value)[:1024])
            for index, value in enumerate(values, start=1)
            if str(value).strip()
        )

    @staticmethod
    def _fact_messages(values: Sequence[str]) -> tuple[FactDelta, ...]:
        return tuple(
            FactDelta(f"fact-{index}", DeltaOperation.REPLACE, str(value)[:1024])
            for index, value in enumerate(values[:6], start=1)
            if str(value).strip()
        )

    @staticmethod
    def _communication_decision(
        candidate: TaskMessage,
        *,
        action: str,
        reason: str,
        delivered: bool,
        queue_wait_ms: float | None = None,
    ) -> dict[str, Any]:
        return {
            "message_id": candidate.message_id,
            "sender": candidate.sender,
            "receiver": candidate.receiver,
            "task_id": candidate.task_id,
            "node_id": candidate.task_id,
            "priority": candidate.priority,
            "ttl": candidate.ttl,
            "summary": candidate.summary,
            "policy_action": action,
            "policy_reason": reason,
            "delivered": delivered,
            "queue_wait_ms": queue_wait_ms,
        }

    def prepare_agent_context(
        self,
        task: TaskNodeView,
        assignment: Assignment,
        trace_id: str,
    ) -> dict[str, Any]:
        """Build memory context and dependency messages before ToolRuntime execution."""
        self._sync_topology(task.run_id, changed_task_ids=(task.task_id,))
        pack = self.memory.build_context_pack(
            run_id=task.run_id,
            node_id=task.task_id,
            task_id=task.task_id,
            taskgraph_nodes=(task.task_id, *task.depends_on),
            query=task.description,
        )
        pack_dict = pack.to_dict()
        self._context_packs[task.task_id] = pack_dict

        messages: list[dict[str, Any]] = []
        for parent_id in task.depends_on:
            parent = self.execution.events.get_task(task.run_id, parent_id)
            if parent is None or parent.assignment is None:
                continue
            sender = parent.assignment.agent_id
            receiver = assignment.agent_id
            if sender == receiver:
                # No message is emitted because both ends are the same Agent
                # instance, which already holds the result in its own context.
                # Record the decision anyway: silently skipping it is what made
                # the console show "Messages = 0" with no way to tell an elided
                # handoff apart from a communication failure.
                self._communication_decisions.append({
                    "message_id": None,
                    "sender": sender,
                    "receiver": receiver,
                    "task_id": task.task_id,
                    "node_id": task.task_id,
                    "priority": max(1, min(10, task.priority)),
                    "ttl": None,
                    "summary": None,
                    "policy_action": "INTERNAL",
                    "policy_reason": (
                        f"parent {parent_id} and child {task.task_id} are executed by the same Agent "
                        f"instance {sender}; the handoff is intra-Agent and needs no network message"
                    ),
                    "delivered": False,
                    "queue_wait_ms": None,
                })
                continue
            parent_summary = parent.result.output if parent.result and parent.result.output else parent.description
            evidence_refs = tuple(
                EvidenceRef(
                    id=item.evidence_id,
                    artifact_id=item.uri or f"evidence://{item.evidence_id}",
                    note=item.kind[:1024],
                )
                for item in parent.evidence[:8]
            )
            candidate = TaskMessage.create(
                message_id=f"msg_{uuid.uuid4().hex}",
                sender=sender,
                receiver=receiver,
                task_id=task.task_id,
                summary=parent_summary[:240] if parent_summary else None,
                facts=self._fact_messages(pack.previous_results + pack.relevant_facts),
                constraints=self._constraint_messages(pack.hard_constraints + pack.prohibitions),
                evidence_refs=evidence_refs,
                priority=max(1, min(10, task.priority)),
                ttl=300,
            )
            parent_events = self.execution.events.events(run_id=task.run_id, task_id=parent_id)
            TRACE_CONTEXTS.register(
                candidate.message_id,
                task_id=task.task_id,
                parent_event_id=parent_events[-1].event_id if parent_events else None,
                model_id=assignment.model_id,
                content_hash=business_content_hash(candidate.to_dict()),
                token_budget=2048,
            )
            plan = self.topology.prepare_context(candidate)
            self._communication_decisions.append(self._communication_decision(
                candidate,
                action=plan.action.value,
                reason=plan.reason,
                delivered=plan.message is not None,
                queue_wait_ms=plan.queue_wait_ms,
            ))
            if plan.message is not None:
                self.topology.mark_delivered(plan.message)
                envelope = message_envelope_contract(plan.message, run_id=task.run_id, budget=2048)
                envelope["policy_action"] = plan.action.value
                envelope["policy_reason"] = plan.reason
                messages.append(envelope)
                self._communication.append(envelope)

        # Deterministic integration mode flushes the finite merge window before
        # the tool call. Production transports can drain this queue on their own
        # timer without changing the contract.
        for deferred in self.topology.drain_deferred(now=time.monotonic() + 1.0):
            if deferred.message is None:
                continue
            self.topology.mark_delivered(deferred.message)
            envelope = message_envelope_contract(deferred.message, run_id=task.run_id, budget=2048)
            envelope["policy_action"] = deferred.action.value
            envelope["policy_reason"] = deferred.reason
            messages.append(envelope)
            self._communication.append(envelope)
            self._communication_decisions.append(self._communication_decision(
                deferred.message,
                action=deferred.action.value,
                reason=deferred.reason,
                delivered=True,
                queue_wait_ms=deferred.queue_wait_ms,
            ))

        self.memory.set_working_state(
            task.run_id,
            task.task_id,
            current_goal=pack.goal,
            active_constraints=pack.hard_constraints + pack.prohibitions,
            recent_results=pack.previous_results[-5:],
            required_evidence=pack.evidence_refs,
            current_agent=assignment.agent_id,
        )
        self._observe(task.run_id, "context_prepared")
        return {"context_pack": pack_dict, "messages": messages, "trace_id": trace_id}

    def _sync_new_events_to_memory(self, run_id: str) -> int:
        count = 0
        for event in self.execution.events.events(run_id=run_id):
            if event.event_id in self._synced_event_ids:
                continue
            self.memory.ingest_event(execution_event_to_memory(event))
            self._synced_event_ids.add(event.event_id)
            count += 1
        return count

    def invalidate_evidence(
        self,
        run_id: str,
        evidence_id: str,
        *,
        reason: str = "evidence invalidated",
    ) -> dict[str, Any]:
        """Invalidate evidence and locally replan only its execution impact closure."""
        owner_task_id: str | None = None
        for task in self.execution.events.tasks(run_id):
            if any(item.evidence_id == evidence_id for item in task.evidence):
                owner_task_id = task.task_id
                break
        if owner_task_id is None:
            raise KeyError(f"unknown evidence: {evidence_id}")

        self.memory.invalidate_by_evidence(evidence_id, reason=reason)
        plan = self.execution.recovery.plan(
            run_id,
            owner_task_id,
            error_class=ErrorClass.REPLAN_REQUIRED,
            reason=reason,
        )
        self.execution.recovery.execute(plan, trace_id=f"trace_{uuid.uuid4().hex}")
        self._sync_new_events_to_memory(run_id)
        self._sync_topology(run_id, changed_task_ids=plan.affected_task_ids)
        self._observe(run_id, "recovery_replan", force=True)
        return plan.to_dict()

    def run_plan(
        self,
        *,
        goalspec: dict[str, Any],
        taskgraph: dict[str, Any],
        plan: Sequence[dict[str, Any]],
        run_id: str | None = None,
        max_rounds: int = 100,
        auto_register_deepseek_resources: bool = False,
        runtime_metadata: dict[str, Any] | None = None,
        round_hook: Callable[["MosaicMainChain", str, int], bool | None] | None = None,
    ) -> MainChainRunResult:
        """Execute an already-compiled TaskGraph through the single main chain.

        Scenario plugins may provide TaskNodes, but they do not get a separate
        orchestrator. This keeps GoalSpec/TaskGraph scenario-specific while all
        execution, memory, topology, verification and recovery services remain shared.
        """
        self._communication.clear()
        self._communication_decisions.clear()
        self._context_packs.clear()
        self._topology_history.clear()
        self._synced_event_ids.clear()
        runtime_metadata = dict(runtime_metadata or {})
        runtime_metadata.setdefault("goalspec_mode", "provided")
        runtime_metadata.setdefault(
            "agent_mode",
            "deepseek" if auto_register_deepseek_resources else "preconfigured",
        )
        canonical_goal = canonical_goalspec(goalspec)
        required_skills = [
            skill
            for node in plan
            for skill in node.get("required_skills", (node.get("required_skill"),))
            if skill
        ]
        # Which delivery kinds each role actually has to serve in *this* DAG.
        # Permissions are granted from the compiled plan rather than a static
        # table, so a node can never end up with no permitted executor.
        delivery_kinds_by_skill: dict[str, set[str]] = {}
        for node in plan:
            kind = str(node.get("delivery_kind") or node.get("metadata", {}).get("delivery_kind") or "")
            if not kind:
                continue
            for skill in node.get("required_skills", (node.get("required_skill"),)):
                if skill:
                    delivery_kinds_by_skill.setdefault(str(skill), set()).add(kind)
        if auto_register_deepseek_resources:
            self.register_default_deepseek_resources(required_skills, delivery_kinds_by_skill)

        run_id = run_id or f"run-{uuid.uuid4().hex[:12]}"
        self._active_run_id = run_id
        self.memory.ingest_goal_spec(run_id, "goal", "goal", goalspec)
        self.execution.create_run(
            plan,
            run_id=run_id,
            actor_id="goal-planner",
            metadata={
                "goal_id": canonical_goal["goal_id"],
                "objective": canonical_goal["objective"],
                "taskgraph_revision": taskgraph.get("revision"),
                **runtime_metadata,
            },
        )
        self._sync_new_events_to_memory(run_id)
        self._sync_topology(run_id)
        self._observe(run_id, "run_initialized", force=True)

        completed: list[str] = []
        for round_index in range(max_rounds):
            ready = self.execution.events.tasks(run_id, TaskState.READY)
            if not ready:
                break
            progress = self.execution.run_once(run_id)
            completed.extend(progress)
            self._sync_new_events_to_memory(run_id)
            self._observe(run_id, "round_complete", force=True)
            if round_hook is not None:
                keep_running = round_hook(self, run_id, round_index + 1)
                self._sync_new_events_to_memory(run_id)
                self._observe(run_id, "round_hook_applied", force=True)
                if keep_running is False:
                    break
            if not self.execution.orchestrator.last_round_made_progress:
                break

        self._sync_new_events_to_memory(run_id)
        final_topology = self._sync_topology(run_id)
        tasks = self.execution.events.tasks(run_id)
        events = self.execution.events.events(run_id=run_id)
        policies = sorted({
            task.assignment.policy for task in tasks if task.assignment is not None
        })
        self._observe(run_id, "run_complete", force=True)
        return MainChainRunResult(
            run_id=run_id,
            runtime_metadata=runtime_metadata,
            goalspec=goalspec,
            canonical_goalspec=canonical_goal,
            taskgraph=taskgraph,
            capability_profiles=[profile.to_dict() for profile in self.execution.capabilities.list()],
            tasks=[tasknode_contract(task) | {"assignment": task.assignment.to_dict() if task.assignment else None}
                   for task in tasks],
            events=[event_contract(event) for event in events],
            verification_results=verification_results(events),
            evidence_manifest=evidence_manifest(tasks),
            topology_snapshot=final_topology,
            topology_history=list(self._topology_history),
            communication=list(self._communication),
            context_packs=dict(self._context_packs),
            memory_metrics=self.memory.export_metrics(),
            completed_task_ids=completed,
            all_succeeded=bool(tasks) and all(task.state is TaskState.SUCCEEDED for task in tasks),
            scheduler_policies=policies,
        )

    def run(
        self,
        user_goal: str,
        *,
        run_id: str | None = None,
        goalspec_mode: str = "rule",
        agent_mode: str = "deepseek",
        planning_horizon: int = 10,
        max_rounds: int = 100,
        round_hook: Callable[["MosaicMainChain", str, int], bool | None] | None = None,
    ) -> MainChainRunResult:
        if agent_mode not in {"deepseek", "preconfigured"}:
            raise ValueError("agent_mode must be 'deepseek' or 'preconfigured'; mock execution is not a production mode")
        goalspec = compile_goal(user_goal, mode=goalspec_mode)
        planner = ToDAGEngine(planning_horizon=planning_horizon)
        taskgraph = planner.build(goalspec)
        plan = planner.execution_plan()
        return self.run_plan(
            goalspec=goalspec,
            taskgraph=taskgraph,
            plan=plan,
            run_id=run_id,
            max_rounds=max_rounds,
            auto_register_deepseek_resources=agent_mode == "deepseek",
            runtime_metadata={
                "goalspec_mode": goalspec_mode,
                "agent_mode": agent_mode,
                "api_provider": (os.getenv("MOSAIC_PROVIDER", "deepseek").strip() or "deepseek") if "deepseek" in {goalspec_mode, agent_mode} else None,
            },
            round_hook=round_hook,
        )
