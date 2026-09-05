"""Capability-aware discrete assignment with OR-Tools and baselines."""

from __future__ import annotations

import itertools
from collections import defaultdict

from .capability import CapabilityRegistry
from .config import Settings
from .cost_model import CostModel
from .models import ActorKind, Assignment, CapabilityProfile, TaskNodeView


class NoFeasibleAssignment(RuntimeError):
    pass


class Scheduler:
    def __init__(self, registry: CapabilityRegistry, cost_model: CostModel, settings: Settings) -> None:
        self.registry = registry
        self.cost_model = cost_model
        self.settings = settings
        self._round_robin_offset = 0

    def _by_kind(self, kind: ActorKind) -> list[CapabilityProfile]:
        return [item for item in self.registry.list(kind) if item.online]

    def _candidates(
        self,
        task: TaskNodeView,
        groups: list[list[CapabilityProfile]] | None = None,
    ) -> dict[str, Assignment]:
        groups = groups or [
            self._by_kind(ActorKind.AGENT), self._by_kind(ActorKind.MODEL),
            self._by_kind(ActorKind.TOOL), self._by_kind(ActorKind.DEVICE),
        ]
        if any(not group for group in groups):
            return {}
        previous = task.assignment.resource_id if task.assignment else None
        best_by_device: dict[str, Assignment] = {}
        for agent, model, tool, device in itertools.product(*groups):
            evaluation = self.cost_model.evaluate(
                task, agent, model, tool, device, previous_resource_id=previous
            )
            if not evaluation.eligible:
                continue
            assignment = Assignment(
                task_id=task.task_id, agent_id=agent.actor_id, model_id=model.actor_id,
                tool_id=tool.actor_id, resource_id=device.actor_id, total_cost=evaluation.total,
                cost_breakdown=evaluation.breakdown, policy=self.settings.scheduler_policy,
                reason=(f"eligible bundle on {device.actor_id}; "
                        f"cost={evaluation.total:.4f}; hard privacy/permission/location filters passed"),
                run_id=task.run_id, schema_version=self.settings.schema_version,
            )
            old = best_by_device.get(device.actor_id)
            if old is None or assignment.total_cost < old.total_cost:
                best_by_device[device.actor_id] = assignment
        return best_by_device

    def _capacities(self) -> dict[str, int]:
        capacities = {}
        for device in self._by_kind(ActorKind.DEVICE):
            occupied = int(round(device.current_load * device.capacity))
            capacities[device.actor_id] = max(0, device.capacity - occupied)
        return capacities

    @staticmethod
    def _policy(assignment: Assignment, policy: str) -> Assignment:
        raw = assignment.to_dict()
        raw["policy"] = policy
        return Assignment.from_dict(raw)

    def assign_tasks(self, tasks: list[TaskNodeView], policy: str | None = None) -> list[Assignment]:
        # The scheduler is a pure placement component: Orchestrator advances state.
        # Ignore non-READY views defensively instead of mutating their lifecycle here.
        ordered = sorted(
            (task for task in tasks if task.state.value == "READY"),
            key=lambda task: (-task.priority, task.task_id),
        )
        selected_policy = (policy or self.settings.scheduler_policy).lower()
        if selected_policy == "round_robin":
            return self._round_robin(ordered)
        if selected_policy == "greedy":
            return self._greedy(ordered, "greedy")
        try:
            return self._ortools(ordered)
        except (ImportError, NoFeasibleAssignment, RuntimeError, ValueError):
            if not self.settings.scheduler_allow_fallback:
                raise
            greedy = self._greedy(ordered, "greedy_fallback")
            if greedy:
                return greedy
            return self._round_robin(ordered, "round_robin_fallback")

    def _greedy(self, tasks: list[TaskNodeView], policy: str) -> list[Assignment]:
        capacities = self._capacities()
        assignments = []
        for task in tasks:
            candidates = [
                item for device_id, item in self._candidates(task).items()
                if capacities.get(device_id, 0) > 0
            ]
            if not candidates:
                continue
            selected = min(candidates, key=lambda item: (item.total_cost, item.resource_id))
            capacities[selected.resource_id] -= 1
            assignments.append(self._policy(selected, policy))
        return assignments

    def _round_robin(self, tasks: list[TaskNodeView], policy: str = "round_robin") -> list[Assignment]:
        capacities = self._capacities()
        device_ids = sorted(key for key, value in capacities.items() if value > 0)
        if not device_ids:
            return []
        assignments = []
        for task in tasks:
            candidates = self._candidates(task)
            for offset in range(len(device_ids)):
                index = (self._round_robin_offset + offset) % len(device_ids)
                device_id = device_ids[index]
                if capacities.get(device_id, 0) > 0 and device_id in candidates:
                    assignments.append(self._policy(candidates[device_id], policy))
                    capacities[device_id] -= 1
                    self._round_robin_offset = (index + 1) % len(device_ids)
                    break
        return assignments

    def _ortools(self, tasks: list[TaskNodeView]) -> list[Assignment]:
        from ortools.graph.python import min_cost_flow

        capacities = self._capacities()
        groups = [
            self._by_kind(ActorKind.AGENT), self._by_kind(ActorKind.MODEL),
            self._by_kind(ActorKind.TOOL), self._by_kind(ActorKind.DEVICE),
        ]
        candidate_templates: dict[tuple[object, ...], dict[str, Assignment]] = {}
        candidates: dict[str, dict[str, Assignment]] = {}
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
                template = self._candidates(task, groups)
                candidate_templates[signature] = template
            candidates[task.task_id] = template
        schedulable = [task for task in tasks if candidates[task.task_id]]
        if not schedulable:
            raise NoFeasibleAssignment("no schedulable task")
        device_ids = sorted({device_id for task in schedulable for device_id in candidates[task.task_id]})
        # More READY tasks than concurrent device slots is normal
        # back-pressure, not an infeasible model.  Optimize the best bounded
        # subset now; remaining READY tasks stay queued for the next round.
        available_capacity = sum(capacities.get(device_id, 0) for device_id in device_ids)
        target_flow = min(len(schedulable), available_capacity)
        if target_flow <= 0:
            raise NoFeasibleAssignment("no available device capacity")
        source = 0
        task_nodes = {task.task_id: index + 1 for index, task in enumerate(schedulable)}
        device_start = len(task_nodes) + 1
        device_nodes = {device_id: device_start + index for index, device_id in enumerate(device_ids)}
        sink = device_start + len(device_nodes)
        flow = min_cost_flow.SimpleMinCostFlow()
        for task in schedulable:
            flow.add_arc_with_capacity_and_unit_cost(source, task_nodes[task.task_id], 1, 0)
            for device_id, assignment in candidates[task.task_id].items():
                flow.add_arc_with_capacity_and_unit_cost(
                    task_nodes[task.task_id], device_nodes[device_id], 1,
                    max(0, int(round(assignment.total_cost * 1000))),
                )
        for device_id, node in device_nodes.items():
            flow.add_arc_with_capacity_and_unit_cost(node, sink, capacities.get(device_id, 0), 0)
        flow.set_node_supply(source, target_flow)
        flow.set_node_supply(sink, -target_flow)
        status = flow.solve()
        if status != flow.OPTIMAL:
            raise NoFeasibleAssignment(f"OR-Tools status={status}")
        reverse_tasks = {node: task_id for task_id, node in task_nodes.items()}
        reverse_devices = {node: device_id for device_id, node in device_nodes.items()}
        results = []
        for arc in range(flow.num_arcs()):
            if flow.flow(arc) != 1:
                continue
            tail = flow.tail(arc)
            head = flow.head(arc)
            if tail in reverse_tasks and head in reverse_devices:
                task_id = reverse_tasks[tail]
                device_id = reverse_devices[head]
                raw = candidates[task_id][device_id].to_dict()
                raw["task_id"] = task_id
                raw["node_id"] = task_id
                raw["run_id"] = tasks_by_id[task_id].run_id
                raw["policy"] = "ortools"
                raw.pop("assignment_id", None)
                raw.pop("created_at", None)
                results.append(Assignment.from_dict(raw))
        return results
