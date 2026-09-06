"""Capability-aware joint assignment with OR-Tools and baselines.

The solved decision is the full bundle required by the handbook:

``Task -> Agent instance -> Model -> Tool -> Resource (DEVICE/EDGE/CLOUD)``

Earlier revisions collapsed every candidate on a device into a single cheapest
Agent before optimizing.  That made same-role Agent instances decorative: they
could be registered and displayed, but they could never compete, run in
parallel, or take over for each other.  Candidates are therefore now kept per
``(agent, resource)`` pair, and Agent concurrency (``max_load``) is a first-class
constraint of the optimization model alongside resource capacity.
"""

from __future__ import annotations

import itertools
import time
from dataclasses import dataclass, field
from typing import Any

from .capability import CapabilityRegistry
from .config import Settings
from .cost_model import CostModel
from .models import ActorKind, Assignment, CapabilityProfile, TaskNodeView


class NoFeasibleAssignment(RuntimeError):
    pass


def bundle_key(agent_id: str, resource_id: str) -> str:
    """Stable identity of one schedulable Agent-on-resource placement."""
    return f"{agent_id}::{resource_id}"


@dataclass(frozen=True)
class CandidateRejection:
    """One eliminated (agent, resource) pair with the hard constraint that killed it."""

    agent_id: str
    resource_id: str
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "resource_id": self.resource_id,
            "reasons": list(self.reasons),
        }


@dataclass
class TaskScheduleDiagnostic:
    """Why a READY task did or did not receive an Assignment this round.

    This is what lets the console separate "waiting for its turn" from "no Agent
    in the pool can legally run this task", instead of showing one opaque
    ``未分配`` label for every non-running node.
    """

    task_id: str
    state: str = "QUEUED"
    candidates: list[dict[str, Any]] = field(default_factory=list)
    rejected: list[dict[str, Any]] = field(default_factory=list)
    selected: dict[str, Any] | None = None
    standby: list[dict[str, Any]] = field(default_factory=list)
    reason: str = ""

    #: Coarse buckets for the elimination list.  A wide pool produces dozens of
    #: individually true rejections; the grouped counts make them readable
    #: without discarding the per-candidate detail.
    _REASON_KINDS: tuple[tuple[str, str], ...] = (
        ("tier", "Agent/资源层级不匹配"),
        ("capability", "技能或离线"),
        ("permission", "权限不足"),
        ("privacy", "数据敏感等级不允许"),
        ("allowed_tiers", "超出任务允许层级"),
        ("data-location", "数据位置策略"),
        ("latency", "超过时延上限"),
        ("context limit", "上下文长度不足"),
        ("reserved for privacy", "该 Agent 仅服务受限数据"),
    )

    def rejection_summary(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in self.rejected:
            for reason in item.get("reasons", ()):
                folded = str(reason).casefold()
                label = next(
                    (label for key, label in self._REASON_KINDS if key in folded),
                    "其他硬约束",
                )
                counts[label] = counts.get(label, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: -kv[1]))

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "state": self.state,
            "candidate_count": len(self.candidates),
            "candidates": self.candidates,
            "rejected_count": len(self.rejected),
            "rejected": self.rejected,
            "rejection_summary": self.rejection_summary(),
            "selected": self.selected,
            "standby": self.standby,
            "reason": self.reason,
        }


@dataclass
class SchedulingRound:
    """Full explainable record of one scheduler invocation."""

    policy: str
    solver_status: str
    solve_ms: float
    ready_task_count: int
    assigned_task_count: int
    objective_cost: float | None
    agent_capacity: dict[str, int]
    resource_capacity: dict[str, int]
    diagnostics: list[TaskScheduleDiagnostic] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy": self.policy,
            "solver_status": self.solver_status,
            "solve_ms": round(self.solve_ms, 3),
            "ready_task_count": self.ready_task_count,
            "assigned_task_count": self.assigned_task_count,
            "queued_task_count": max(0, self.ready_task_count - self.assigned_task_count),
            "objective_cost": self.objective_cost,
            "agent_capacity": dict(self.agent_capacity),
            "resource_capacity": dict(self.resource_capacity),
            "started_at": self.started_at,
            "tasks": [item.to_dict() for item in self.diagnostics],
        }


class Scheduler:
    def __init__(self, registry: CapabilityRegistry, cost_model: CostModel, settings: Settings) -> None:
        self.registry = registry
        self.cost_model = cost_model
        self.settings = settings
        self._round_robin_offset = 0
        # Last explainable scheduling round.  The Orchestrator publishes it as an
        # authoritative event; nothing reads it as mutable scheduler state.
        self.last_round: SchedulingRound | None = None

    def _by_kind(self, kind: ActorKind) -> list[CapabilityProfile]:
        return [item for item in self.registry.list(kind) if item.online]

    @staticmethod
    def _tier_of(profile: CapabilityProfile) -> str:
        raw = str((profile.metadata or {}).get("tier", profile.device_location or "")).casefold()
        return raw if raw in {"device", "edge", "cloud"} else ""

    @classmethod
    def _tier_compatible(cls, agent: CapabilityProfile, device: CapabilityProfile) -> tuple[bool, str]:
        """Reject placing an Agent on a resource pool of a different tier.

        Only enforced when the resource pool actually declares a DEVICE/EDGE/CLOUD
        location.  Generic resources (``device_location='local'`` and similar) stay
        compatible with every Agent so existing deployments keep working.
        """
        device_tier = str(device.device_location or "").casefold()
        if device_tier not in {"device", "edge", "cloud"}:
            return True, ""
        agent_tier = cls._tier_of(agent)
        if not agent_tier or agent_tier == device_tier:
            return True, ""
        return False, (
            f"{agent.actor_id} runs on tier={agent_tier} and cannot execute on "
            f"{device_tier} resource pool {device.actor_id}"
        )

    def _candidates(
        self,
        task: TaskNodeView,
        groups: list[list[CapabilityProfile]] | None = None,
        *,
        collect_rejections: bool = False,
    ) -> tuple[dict[str, Assignment], list[CandidateRejection]]:
        """Enumerate every feasible ``(agent, resource)`` placement for one task.

        Model and tool are still optimized inside the pair (cheapest eligible
        combination wins), but Agent identity is never collapsed away: two Agents
        of the same role on the same resource remain two distinct candidates that
        the solver can choose between.
        """
        groups = groups or [
            self._by_kind(ActorKind.AGENT), self._by_kind(ActorKind.MODEL),
            self._by_kind(ActorKind.TOOL), self._by_kind(ActorKind.DEVICE),
        ]
        rejections: dict[tuple[str, str], list[str]] = {}
        if any(not group for group in groups):
            missing = [
                name for name, group in zip(("agent", "model", "tool", "resource"), groups) if not group
            ]
            if collect_rejections and missing:
                rejections[("*", "*")] = [f"no online {name} actor registered" for name in missing]
            return {}, [
                CandidateRejection("*", "*", tuple(reasons)) for reasons in rejections.values()
            ]
        previous = task.assignment.resource_id if task.assignment else None
        best_by_bundle: dict[str, Assignment] = {}
        for agent, model, tool, device in itertools.product(*groups):
            key = bundle_key(agent.actor_id, device.actor_id)
            tier_ok, tier_reason = self._tier_compatible(agent, device)
            if not tier_ok:
                if collect_rejections and key not in best_by_bundle:
                    rejections.setdefault((agent.actor_id, device.actor_id), []).append(tier_reason)
                continue
            evaluation = self.cost_model.evaluate(
                task, agent, model, tool, device, previous_resource_id=previous
            )
            if not evaluation.eligible:
                if collect_rejections:
                    rejections.setdefault((agent.actor_id, device.actor_id), []).extend(
                        evaluation.reasons
                    )
                continue
            recommended_tier = evaluation.tier.value
            raw_actual_tier = str((agent.metadata or {}).get("tier", agent.device_location or recommended_tier)).casefold()
            actual_tier = raw_actual_tier if raw_actual_tier in {"device", "edge", "cloud"} else recommended_tier
            fallback = actual_tier != recommended_tier
            assignment = Assignment(
                task_id=task.task_id, agent_id=agent.actor_id, model_id=model.actor_id,
                tool_id=tool.actor_id, resource_id=device.actor_id, total_cost=evaluation.total,
                cost_breakdown=evaluation.breakdown, policy="candidate_uncommitted",
                reason=(f"eligible bundle {agent.actor_id} on {device.actor_id}; cost={evaluation.total:.4f}; "
                        f"recommended_tier={recommended_tier}; actual_tier={actual_tier}; "
                        f"placement_fallback={str(fallback).lower()}; "
                        f"partition_recommendation={evaluation.partition_policy.value}; "
                        "hard privacy/permission/location filters passed"),
                execution_tier=actual_tier,
                recommended_tier=recommended_tier,
                actual_execution_tier=actual_tier,
                placement_fallback=fallback,
                placement_evidence={
                    "actual_tier_source": "selected_agent_runtime_profile",
                    "agent_id": agent.actor_id,
                    "agent_declared_tier": actual_tier,
                    "recommended_tier_source": "edge_cloud_placement_engine",
                },
                partition_policy=evaluation.partition_policy.value,
                partition_descriptor=evaluation.partition_descriptor.to_dict(),
                run_id=task.run_id, schema_version=self.settings.schema_version,
            )
            old = best_by_bundle.get(key)
            if old is None or assignment.total_cost < old.total_cost:
                best_by_bundle[key] = assignment
        # A pair that ended up feasible through some model/tool combination is not
        # a rejection, even if other combinations for that pair were filtered.
        rejected = [
            CandidateRejection(agent_id, resource_id, tuple(dict.fromkeys(reasons)))
            for (agent_id, resource_id), reasons in sorted(rejections.items())
            if bundle_key(agent_id, resource_id) not in best_by_bundle
        ]
        return best_by_bundle, rejected

    def candidates_for(self, task: TaskNodeView) -> tuple[dict[str, Assignment], list[CandidateRejection]]:
        """Public candidate enumeration used by diagnostics and the console."""
        return self._candidates(task, collect_rejections=True)

    def _capacities(self) -> dict[str, int]:
        capacities = {}
        for device in self._by_kind(ActorKind.DEVICE):
            occupied = int(round(device.current_load * device.capacity))
            capacities[device.actor_id] = max(0, device.capacity - occupied)
        return capacities

    def _agent_capacities(self) -> dict[str, int]:
        """Remaining concurrent slots per Agent instance.

        ``AgentProfile.max_load`` is projected onto ``CapabilityProfile.capacity``
        by the registry bridge, so this is the Agent's own declared concurrency —
        not the capacity of the machine it happens to sit on.
        """
        capacities = {}
        for agent in self._by_kind(ActorKind.AGENT):
            occupied = int(round(agent.current_load * agent.capacity))
            capacities[agent.actor_id] = max(0, agent.capacity - occupied)
        return capacities

    @staticmethod
    def _mutex_of(task: TaskNodeView) -> set[str]:
        raw = (task.metadata or {}).get("mutex_with", ())
        if isinstance(raw, str):
            raw = (raw,)
        return {str(item) for item in raw or () if str(item)}

    @staticmethod
    def _candidate_row(key: str, assignment: Assignment) -> dict[str, Any]:
        return {
            "bundle_key": key,
            "agent_id": assignment.agent_id,
            "model_id": assignment.model_id,
            "tool_id": assignment.tool_id,
            "resource_id": assignment.resource_id,
            "execution_tier": assignment.actual_execution_tier,
            "recommended_tier": assignment.recommended_tier,
            "total_cost": assignment.total_cost,
            "cost_breakdown": dict(assignment.cost_breakdown),
        }

    @staticmethod
    def _policy(assignment: Assignment, policy: str) -> Assignment:
        raw = assignment.to_dict()
        raw["policy"] = policy
        raw["solver_provenance"] = {
            "engine": "mosaic_builtin",
            "algorithm": policy,
            "status": "SELECTED",
        }
        return Assignment.from_dict(raw)

    def assign_tasks(self, tasks: list[TaskNodeView], policy: str | None = None) -> list[Assignment]:
        # The scheduler is a pure placement component: Orchestrator advances state.
        # Ignore non-READY views defensively instead of mutating their lifecycle here.
        ordered = sorted(
            (task for task in tasks if task.state.value == "READY"),
            key=lambda task: (-task.priority, task.task_id),
        )
        selected_policy = (policy or self.settings.scheduler_policy).lower()
        started = time.perf_counter()
        if selected_policy == "round_robin":
            assignments = self._round_robin(ordered)
        elif selected_policy == "greedy":
            assignments = self._greedy(ordered, "greedy")
        else:
            try:
                assignments = self._ortools(ordered)
            except (ImportError, NoFeasibleAssignment, RuntimeError, ValueError) as exc:
                if not self.settings.scheduler_allow_fallback:
                    self.last_round = self._empty_round(
                        ordered, selected_policy, f"{type(exc).__name__}: {exc}"
                    )
                    raise
                assignments = self._greedy(ordered, "greedy_fallback")
                if not assignments:
                    assignments = self._round_robin(ordered, "round_robin_fallback")
        if self.last_round is not None:
            self.last_round.solve_ms = (time.perf_counter() - started) * 1000.0
        return assignments

    def _empty_round(self, tasks: list[TaskNodeView], policy: str, reason: str) -> SchedulingRound:
        return SchedulingRound(
            policy=policy,
            solver_status="NO_ASSIGNMENT",
            solve_ms=0.0,
            ready_task_count=len(tasks),
            assigned_task_count=0,
            objective_cost=None,
            agent_capacity=self._agent_capacities(),
            resource_capacity=self._capacities(),
            diagnostics=[
                TaskScheduleDiagnostic(task_id=task.task_id, state="QUEUED", reason=reason)
                for task in tasks
            ],
        )

    def _diagnose(
        self,
        task: TaskNodeView,
        candidates: dict[str, Assignment],
        rejected: list[CandidateRejection],
        *,
        selected: Assignment | None,
        agent_capacity: dict[str, int],
        resource_capacity: dict[str, int],
    ) -> TaskScheduleDiagnostic:
        rows = sorted(
            (self._candidate_row(key, item) for key, item in candidates.items()),
            key=lambda row: (row["total_cost"], row["bundle_key"]),
        )
        diagnostic = TaskScheduleDiagnostic(
            task_id=task.task_id,
            candidates=rows,
            rejected=[item.to_dict() for item in rejected],
        )
        if selected is not None:
            diagnostic.state = "ASSIGNED"
            diagnostic.selected = self._candidate_row(
                bundle_key(selected.agent_id, selected.resource_id), selected
            )
            # Runner-up candidates are the live standby set for this task; the
            # recovery path re-solves rather than trusting a frozen list, but the
            # console can show who was in contention and who would take over.
            diagnostic.standby = [
                row for row in rows
                if row["bundle_key"] != diagnostic.selected["bundle_key"]
            ][:5]
            diagnostic.reason = (
                f"selected {selected.agent_id} on {selected.resource_id}; "
                f"cost={selected.total_cost:.4f}; {len(rows) - 1} competing candidate(s) not selected"
            )
        elif not candidates:
            diagnostic.state = "NO_FEASIBLE_AGENT"
            diagnostic.reason = (
                "every registered Agent/resource pair was eliminated by a hard constraint"
                if rejected else "no online Agent/Model/Tool/Resource actor is registered"
            )
        else:
            blocked_agents = [
                row["agent_id"] for row in rows if agent_capacity.get(row["agent_id"], 0) <= 0
            ]
            blocked_resources = [
                row["resource_id"] for row in rows if resource_capacity.get(row["resource_id"], 0) <= 0
            ]
            diagnostic.state = "QUEUED"
            diagnostic.standby = rows[:5]
            if blocked_agents or blocked_resources:
                diagnostic.reason = (
                    "feasible but waiting for capacity; saturated agents="
                    f"{sorted(set(blocked_agents))}; saturated resources={sorted(set(blocked_resources))}"
                )
            else:
                diagnostic.reason = (
                    "feasible candidates exist but the optimizer placed higher-priority work first"
                )
        return diagnostic

    def _greedy(self, tasks: list[TaskNodeView], policy: str) -> list[Assignment]:
        resource_capacity = self._capacities()
        agent_capacity = self._agent_capacities()
        agent_snapshot = dict(agent_capacity)
        resource_snapshot = dict(resource_capacity)
        assignments: list[Assignment] = []
        diagnostics: list[TaskScheduleDiagnostic] = []
        claimed: set[str] = set()
        for task in tasks:
            candidates, rejected = self._candidates(task, collect_rejections=True)
            if self._mutex_of(task) & claimed:
                diagnostics.append(TaskScheduleDiagnostic(
                    task_id=task.task_id, state="QUEUED",
                    candidates=[self._candidate_row(k, v) for k, v in candidates.items()],
                    reason="deferred by a mutual-exclusion constraint with a task assigned this round",
                ))
                continue
            affordable = [
                item for item in candidates.values()
                if resource_capacity.get(item.resource_id, 0) > 0
                and agent_capacity.get(item.agent_id, 0) > 0
            ]
            selected = min(affordable, key=lambda item: (item.total_cost, item.agent_id, item.resource_id)) if affordable else None
            if selected is not None:
                resource_capacity[selected.resource_id] -= 1
                agent_capacity[selected.agent_id] -= 1
                claimed.add(task.task_id)
                assignments.append(self._policy(selected, policy))
            diagnostics.append(self._diagnose(
                task, candidates, rejected, selected=selected,
                agent_capacity=agent_capacity, resource_capacity=resource_capacity,
            ))
        self.last_round = SchedulingRound(
            policy=policy,
            solver_status="GREEDY_SELECTED" if assignments else "NO_ASSIGNMENT",
            solve_ms=0.0,
            ready_task_count=len(tasks),
            assigned_task_count=len(assignments),
            objective_cost=round(sum(item.total_cost for item in assignments), 6) if assignments else None,
            agent_capacity=agent_snapshot,
            resource_capacity=resource_snapshot,
            diagnostics=diagnostics,
        )
        return assignments

    def _round_robin(
        self, tasks: list[TaskNodeView], policy: str = "round_robin"
    ) -> list[Assignment]:
        resource_capacity = self._capacities()
        agent_capacity = self._agent_capacities()
        agent_snapshot = dict(agent_capacity)
        resource_snapshot = dict(resource_capacity)
        # Round-robin now rotates over Agent instances rather than resources, so
        # the baseline actually spreads load across same-role Agents.
        agent_ids = sorted(key for key, value in agent_capacity.items() if value > 0)
        assignments: list[Assignment] = []
        diagnostics: list[TaskScheduleDiagnostic] = []
        for task in tasks:
            candidates, rejected = self._candidates(task, collect_rejections=True)
            by_agent: dict[str, list[Assignment]] = {}
            for item in candidates.values():
                by_agent.setdefault(item.agent_id, []).append(item)
            selected: Assignment | None = None
            for offset in range(len(agent_ids)):
                index = (self._round_robin_offset + offset) % len(agent_ids)
                agent_id = agent_ids[index]
                if agent_capacity.get(agent_id, 0) <= 0:
                    continue
                options = [
                    item for item in by_agent.get(agent_id, ())
                    if resource_capacity.get(item.resource_id, 0) > 0
                ]
                if not options:
                    continue
                selected = min(options, key=lambda item: (item.total_cost, item.resource_id))
                agent_capacity[agent_id] -= 1
                resource_capacity[selected.resource_id] -= 1
                self._round_robin_offset = (index + 1) % len(agent_ids)
                assignments.append(self._policy(selected, policy))
                break
            diagnostics.append(self._diagnose(
                task, candidates, rejected, selected=selected,
                agent_capacity=agent_capacity, resource_capacity=resource_capacity,
            ))
        self.last_round = SchedulingRound(
            policy=policy,
            solver_status="ROUND_ROBIN_SELECTED" if assignments else "NO_ASSIGNMENT",
            solve_ms=0.0,
            ready_task_count=len(tasks),
            assigned_task_count=len(assignments),
            objective_cost=round(sum(item.total_cost for item in assignments), 6) if assignments else None,
            agent_capacity=agent_snapshot,
            resource_capacity=resource_snapshot,
            diagnostics=diagnostics,
        )
        return assignments

    def _ortools(self, tasks: list[TaskNodeView]) -> list[Assignment]:
        """Joint Task->Agent->Model->Tool->Resource assignment via CP-SAT.

        A min-cost-flow model cannot express Agent concurrency and resource
        capacity at the same time without collapsing one of them, which is why
        same-role Agents used to be pre-compressed away.  CP-SAT keeps every
        ``(agent, resource)`` pair as its own decision variable and constrains
        Agent ``max_load``, resource capacity and task mutual exclusion together.
        """
        from ortools.sat.python import cp_model

        resource_capacity = self._capacities()
        agent_capacity = self._agent_capacities()
        groups = [
            self._by_kind(ActorKind.AGENT), self._by_kind(ActorKind.MODEL),
            self._by_kind(ActorKind.TOOL), self._by_kind(ActorKind.DEVICE),
        ]
        candidate_templates: dict[tuple[object, ...], tuple[dict[str, Assignment], list[CandidateRejection]]] = {}
        candidates: dict[str, dict[str, Assignment]] = {}
        rejections: dict[str, list[CandidateRejection]] = {}
        tasks_by_id = {task.task_id: task for task in tasks}
        for task in tasks:
            previous = task.assignment.resource_id if task.assignment else None
            signature = (
                task.task_type,
                task.required_capabilities,
                task.required_permissions,
                task.privacy_level,
                task.data_location,
                task.estimated_tokens,
                task.max_latency_ms,
                previous,
            )
            template = candidate_templates.get(signature)
            if template is None:
                template = self._candidates(task, groups, collect_rejections=True)
                candidate_templates[signature] = template
            candidates[task.task_id], rejections[task.task_id] = template

        schedulable = [task for task in tasks if candidates[task.task_id]]
        if not schedulable:
            raise NoFeasibleAssignment("no schedulable task")
        available_capacity = min(
            sum(max(0, value) for value in resource_capacity.values()),
            sum(max(0, value) for value in agent_capacity.values()),
        )
        if available_capacity <= 0:
            raise NoFeasibleAssignment("no available Agent or resource capacity")

        model = cp_model.CpModel()
        variables: dict[tuple[str, str], Any] = {}
        by_agent: dict[str, list[Any]] = {}
        by_resource: dict[str, list[Any]] = {}
        by_task: dict[str, list[Any]] = {}
        objective_terms = []
        # Assigning work is worth more than saving cost: the reward dominates any
        # single bundle cost so the solver never idles a feasible Agent to save
        # a fraction of a cost unit.
        costs_scaled = {
            (task.task_id, key): max(0, int(round(item.total_cost * 1000)))
            for task in schedulable
            for key, item in candidates[task.task_id].items()
        }
        assignment_reward = max(costs_scaled.values(), default=0) + 1000
        for task in schedulable:
            for key, item in candidates[task.task_id].items():
                if agent_capacity.get(item.agent_id, 0) <= 0 or resource_capacity.get(item.resource_id, 0) <= 0:
                    continue
                variable = model.NewBoolVar(f"x[{task.task_id}][{key}]")
                variables[(task.task_id, key)] = variable
                by_task.setdefault(task.task_id, []).append(variable)
                by_agent.setdefault(item.agent_id, []).append(variable)
                by_resource.setdefault(item.resource_id, []).append(variable)
                objective_terms.append(
                    (assignment_reward - costs_scaled[(task.task_id, key)]) * variable
                )
        if not variables:
            raise NoFeasibleAssignment("no candidate bundle has both Agent and resource capacity")

        for task_id, group in by_task.items():
            model.AddAtMostOne(group)
        for agent_id, group in by_agent.items():
            model.Add(sum(group) <= max(0, agent_capacity.get(agent_id, 0)))
        for resource_id, group in by_resource.items():
            model.Add(sum(group) <= max(0, resource_capacity.get(resource_id, 0)))
        # Mutually exclusive tasks may not both start in the same round.
        for task in schedulable:
            peers = self._mutex_of(task)
            for peer in sorted(peers):
                if peer <= task.task_id or peer not in by_task:
                    continue
                model.Add(sum(by_task[task.task_id]) + sum(by_task[peer]) <= 1)

        model.Maximize(sum(objective_terms))
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = max(0.5, float(self.settings.scheduler_solver_timeout_s))
        solver.parameters.num_search_workers = 4
        status = solver.Solve(model)
        if status not in {cp_model.OPTIMAL, cp_model.FEASIBLE}:
            raise NoFeasibleAssignment(f"OR-Tools CP-SAT status={solver.StatusName(status)}")

        status_name = "OPTIMAL" if status == cp_model.OPTIMAL else "FEASIBLE"
        results: list[Assignment] = []
        selected_by_task: dict[str, Assignment] = {}
        for (task_id, key), variable in variables.items():
            if not solver.BooleanValue(variable):
                continue
            raw = candidates[task_id][key].to_dict()
            raw["task_id"] = task_id
            raw["node_id"] = task_id
            raw["run_id"] = tasks_by_id[task_id].run_id
            raw["policy"] = "ortools"
            raw["solver_provenance"] = {
                "engine": "ortools.sat.python.cp_model.CpSolver",
                "algorithm": "cp_sat_joint_task_agent_model_tool_resource_assignment",
                "status": status_name,
                "available_capacity": available_capacity,
                "decision_variables": len(variables),
                "agent_capacity_constraints": len(by_agent),
                "resource_capacity_constraints": len(by_resource),
                "objective_value": solver.ObjectiveValue(),
                "wall_time_s": solver.WallTime(),
            }
            raw.pop("assignment_id", None)
            raw.pop("created_at", None)
            assignment = Assignment.from_dict(raw)
            results.append(assignment)
            selected_by_task[task_id] = assignment

        remaining_agent = dict(agent_capacity)
        remaining_resource = dict(resource_capacity)
        for assignment in results:
            remaining_agent[assignment.agent_id] = remaining_agent.get(assignment.agent_id, 0) - 1
            remaining_resource[assignment.resource_id] = remaining_resource.get(assignment.resource_id, 0) - 1
        diagnostics = [
            self._diagnose(
                task,
                candidates[task.task_id],
                rejections.get(task.task_id, []),
                selected=selected_by_task.get(task.task_id),
                agent_capacity=remaining_agent,
                resource_capacity=remaining_resource,
            )
            for task in tasks
        ]
        results.sort(key=lambda item: item.task_id)
        self.last_round = SchedulingRound(
            policy="ortools",
            solver_status=status_name,
            solve_ms=solver.WallTime() * 1000.0,
            ready_task_count=len(tasks),
            assigned_task_count=len(results),
            objective_cost=round(sum(item.total_cost for item in results), 6) if results else None,
            agent_capacity=agent_capacity,
            resource_capacity=resource_capacity,
            diagnostics=diagnostics,
        )
        return results
