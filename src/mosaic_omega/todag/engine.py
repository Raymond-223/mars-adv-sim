"""Stateful ToDAG engine with typed edges, rolling view and local replanning."""

from __future__ import annotations

import hashlib
import json
import time
from copy import deepcopy
from threading import RLock
from typing import Any, Mapping

from .agents import DAGValidationAgent, DecompositionAgent, IncrementalRecomputeAgent, RequirementAgent
from .graph import critical_path, edges, graph_levels, ready_task_ids, rolling_window
from .models import DAGNode, LongTaskInput


EDITABLE_NODE_FIELDS = frozenset(
    {
        "title",
        "description",
        "required_skill",
        "required_skills",
        "agent_role",
        "node_type",
        "depends_on",
        "dependency_types",
        "evidence_dependencies",
        "mutex_with",
        "priority",
        "inputs",
        "outputs",
        "hard_constraints",
        "soft_preferences",
        "acceptance_conditions",
        "acceptance_predicates",
        "budget",
        "prohibitions",
        "resource_requirements",
        "risk",
        "estimated_cost",
        "candidate_executors",
        "source_refs",
        "rollback_checkpoint",
    }
)

_PLACEMENT_FIELDS = frozenset(
    {
        "allowed_tiers",
        "preferred_tier",
        "max_latency_ms",
        "min_memory_mb",
        "min_gpu_count",
        "data_sensitivity",
        "require_local_data",
    }
)


class ToDAGEngine:
    def __init__(self, planning_horizon: int = 10) -> None:
        if planning_horizon < 1:
            raise ValueError("planning_horizon must be >= 1")
        self.requirement_agent = RequirementAgent()
        self.decomposition_agent = DecompositionAgent()
        self.validation_agent = DAGValidationAgent()
        self.recompute_agent = IncrementalRecomputeAgent()
        self._lock = RLock()
        self._specification: LongTaskInput | None = None
        self._nodes: dict[str, DAGNode] = {}
        self._revision = 0
        self._terminal_node_id: str | None = None
        self._planning_horizon = planning_horizon
        self._needs_clarification = False
        self._planner_trace: list[dict[str, Any]] = []
        self._change_set: dict[str, Any] = self._empty_change_set()
        now = time.time()
        self._dag_status_provenance: dict[str, Any] = {
            "status": "empty",
            "entered_at": now,
            "operation": "engine_init",
            "reason": "no GoalSpec has been built",
            "source": "ToDAGEngine.__init__",
        }
        self._node_status_provenance: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _empty_change_set() -> dict[str, Any]:
        return {
            "changed_node_ids": [],
            "added_node_ids": [],
            "removed_node_ids": [],
            "invalidated_node_ids": [],
            "recomputed_node_ids": [],
            "preserved_node_ids": [],
        }

    def _record_dag_status(self, status: str, *, operation: str, reason: str) -> None:
        current = self._dag_status_provenance
        if current.get("status") == status:
            return
        self._dag_status_provenance = {
            "status": status,
            "entered_at": time.time(),
            "operation": operation,
            "reason": reason,
            "source": "ToDAGEngine",
        }

    def _record_node_status(self, task_id: str, status: str, *, operation: str, reason: str) -> None:
        current = self._node_status_provenance.get(task_id)
        if current and current.get("status") == status:
            return
        self._node_status_provenance[task_id] = {
            "status": status,
            "entered_at": time.time(),
            "operation": operation,
            "reason": reason,
            "source": "ToDAGEngine",
        }

    @property
    def is_built(self) -> bool:
        return self._specification is not None

    @property
    def planning_horizon(self) -> int:
        return self._planning_horizon

    @staticmethod
    def _fingerprint(node: DAGNode, nodes: Mapping[str, DAGNode]) -> str:
        document = {
            "definition": node.definition_dict(),
            "dependency_fingerprints": [nodes[parent].fingerprint for parent in node.depends_on],
        }
        raw = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def _refresh_fingerprints(self, nodes: dict[str, DAGNode], order: list[str], affected: set[str]) -> None:
        for task_id in order:
            if task_id in affected:
                nodes[task_id].fingerprint = self._fingerprint(nodes[task_id], nodes)

    @staticmethod
    def _copy_runtime(source: DAGNode, target: DAGNode) -> None:
        target.status = source.status
        target.version = source.version
        target.result = deepcopy(source.result)
        target.evidence = deepcopy(source.evidence)
        target.fingerprint = source.fingerprint
        target.recompute_reason = source.recompute_reason

    def build(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            specification, requirement_trace = self.requirement_agent.run(raw)
            nodes, decomposition_trace = self.decomposition_agent.run(specification)
            order, terminal, validation_trace = self.validation_agent.run(nodes)
            self._refresh_fingerprints(nodes, order, set(nodes))
            self._specification = specification
            self._nodes = nodes
            self._revision = 1
            self._terminal_node_id = terminal
            self._needs_clarification = bool(requirement_trace.get("needs_clarification"))
            self._planner_trace = [requirement_trace, decomposition_trace, validation_trace]
            self._node_status_provenance = {}
            for task_id, node in nodes.items():
                self._record_node_status(
                    task_id,
                    node.status,
                    operation="build",
                    reason="node status created by deterministic ToDAG compilation",
                )
            dag_status = "needs_clarification" if self._needs_clarification else "ready"
            self._record_dag_status(
                dag_status,
                operation="build",
                reason="requirement compiler requested clarification" if self._needs_clarification else "GoalSpec validated and DAG materialized",
            )
            self._change_set = {
                "changed_node_ids": [],
                "added_node_ids": order,
                "removed_node_ids": [],
                "invalidated_node_ids": [],
                "recomputed_node_ids": order,
                "preserved_node_ids": [],
            }
            return self.snapshot()

    @staticmethod
    def _apply_patch(node: DAGNode, patch: Mapping[str, Any]) -> None:
        unknown = sorted(set(patch) - EDITABLE_NODE_FIELDS)
        if unknown:
            raise ValueError(f"node patch contains non-editable fields: {unknown}")
        for field_name, value in patch.items():
            setattr(node, field_name, deepcopy(value))
        node.validate()

    def _mark_recompute(
        self,
        candidate: dict[str, DAGNode],
        old_nodes: Mapping[str, DAGNode],
        changed_ids: set[str],
        affected_order: list[str],
        reason: str,
    ) -> None:
        for task_id in affected_order:
            node = candidate[task_id]
            old = old_nodes.get(task_id)
            node.version = (old.version + 1) if old is not None else 1
            node.result = None
            node.evidence = []
            node.status = "pending" if task_id in changed_ids else "stale"
            node.recompute_reason = reason if task_id in changed_ids else f"upstream_changed:{reason}"
            self._record_node_status(
                task_id,
                node.status,
                operation="incremental_recompute",
                reason=node.recompute_reason,
            )

    def update_node(self, task_id: str, patch: Mapping[str, Any]) -> dict[str, Any]:
        """Atomically patch one TaskNode and invalidate only its impact closure."""
        with self._lock:
            if not self.is_built:
                raise RuntimeError("build a DAG before updating nodes")
            if task_id not in self._nodes:
                raise KeyError(task_id)
            if not isinstance(patch, Mapping) or not patch:
                raise ValueError("node patch must be a non-empty JSON object")

            old_nodes = deepcopy(self._nodes)
            candidate = deepcopy(self._nodes)
            before = candidate[task_id].definition_dict()
            self._apply_patch(candidate[task_id], patch)
            if candidate[task_id].definition_dict() == before:
                self._change_set = {
                    **self._empty_change_set(),
                    "preserved_node_ids": sorted(candidate),
                }
                return self.snapshot()

            order, terminal, validation_trace = self.validation_agent.run(candidate)
            plan, recompute_trace = self.recompute_agent.run(old_nodes, candidate, task_id)
            self._mark_recompute(
                candidate,
                old_nodes,
                {task_id},
                plan.order,
                reason=f"node_definition_changed:{task_id}",
            )
            self._refresh_fingerprints(candidate, order, plan.affected)

            self._nodes = candidate
            self._terminal_node_id = terminal
            self._revision += 1
            self._planner_trace.extend([validation_trace, recompute_trace])
            self._change_set = {
                "changed_node_ids": [task_id],
                "added_node_ids": [],
                "removed_node_ids": [],
                "invalidated_node_ids": sorted(plan.invalidated),
                "recomputed_node_ids": plan.order,
                "preserved_node_ids": sorted(plan.preserved),
            }
            return self.snapshot()

    def update_specification(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        """Compile a changed six-field GoalSpec and locally replan affected nodes.

        Stable semantic task IDs allow unchanged branches to keep their results,
        versions and fingerprints.  No full restart is required when a goal,
        constraint or acceptance condition changes.
        """
        with self._lock:
            if not self.is_built:
                return self.build(raw)

            specification, requirement_trace = self.requirement_agent.run(raw)
            candidate, decomposition_trace = self.decomposition_agent.run(specification)
            order, terminal, validation_trace = self.validation_agent.run(candidate)
            old_nodes = deepcopy(self._nodes)

            old_ids = set(old_nodes)
            new_ids = set(candidate)
            added = new_ids - old_ids
            removed = old_ids - new_ids
            changed: set[str] = set(added)

            for task_id in sorted(old_ids & new_ids):
                if candidate[task_id].definition_dict() == old_nodes[task_id].definition_dict():
                    self._copy_runtime(old_nodes[task_id], candidate[task_id])
                else:
                    changed.add(task_id)

            # A removed upstream node can still invalidate a surviving descendant.
            for removed_id in removed:
                try:
                    old_descendants = self.recompute_agent.run(
                        old_nodes, old_nodes, removed_id
                    )[0].affected
                except KeyError:
                    old_descendants = set()
                changed.update(old_descendants & new_ids)

            if changed:
                plan, recompute_trace = self.recompute_agent.run(old_nodes, candidate, sorted(changed))
                affected = set(plan.affected)
                self._mark_recompute(
                    candidate,
                    old_nodes,
                    changed,
                    plan.order,
                    reason="goalspec_changed",
                )
                self._refresh_fingerprints(candidate, order, affected)
                preserved = set(candidate) - affected
                invalidated = plan.invalidated
                recomputed = plan.order
            else:
                recompute_trace = {
                    "stage": "incremental_recompute_agent",
                    "executor_mode": "deterministic_compiler_stage",
                    "action": "no_semantic_taskgraph_change",
                    "changed_node_ids": [],
                    "recomputed_node_ids": [],
                    "invalidated_node_ids": [],
                    "preserved_node_ids": sorted(candidate),
                }
                preserved = set(candidate)
                invalidated = set()
                recomputed = []

            self._specification = specification
            self._nodes = candidate
            self._terminal_node_id = terminal
            self._needs_clarification = bool(requirement_trace.get("needs_clarification"))
            self._revision += 1
            self._node_status_provenance = {
                task_id: provenance
                for task_id, provenance in self._node_status_provenance.items()
                if task_id in candidate
            }
            # Added/semantically changed nodes are recorded by _mark_recompute;
            # preserved nodes retain their original status-entered timestamp.
            for task_id, node in candidate.items():
                if task_id not in self._node_status_provenance:
                    self._record_node_status(
                        task_id,
                        node.status,
                        operation="update_specification",
                        reason="node introduced by GoalSpec update",
                    )
            dag_status = "needs_clarification" if self._needs_clarification else "ready"
            self._record_dag_status(
                dag_status,
                operation="update_specification",
                reason="requirement compiler requested clarification" if self._needs_clarification else "updated GoalSpec validated",
            )
            self._planner_trace.extend(
                [
                    requirement_trace,
                    decomposition_trace,
                    validation_trace,
                    {
                        **recompute_trace,
                        "action": "local_replan_after_goalspec_change",
                        "added_node_ids": sorted(added),
                        "removed_node_ids": sorted(removed),
                    },
                ]
            )
            self._change_set = {
                "changed_node_ids": sorted(changed - added),
                "added_node_ids": sorted(added),
                "removed_node_ids": sorted(removed),
                "invalidated_node_ids": sorted(invalidated),
                "recomputed_node_ids": recomputed,
                "preserved_node_ids": sorted(preserved),
            }
            return self.snapshot()


    def _release_stale_nodes(self) -> None:
        """Move invalidated descendants back to pending once all parents are fresh/completed."""
        if not self._nodes:
            return
        order, _, _ = self.validation_agent.run(self._nodes)
        for task_id in order:
            node = self._nodes[task_id]
            if node.status not in {"stale", "invalidated"}:
                continue
            if all(self._nodes[parent].status == "completed" for parent in node.depends_on):
                node.status = "pending"
                self._record_node_status(
                    task_id,
                    node.status,
                    operation="release_stale_node",
                    reason="all dependencies are completed after upstream recomputation",
                )

    def set_node_result(
        self,
        task_id: str,
        result: Any,
        status: str = "completed",
        evidence: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            if task_id not in self._nodes:
                raise KeyError(task_id)
            if status not in {"completed", "failed"}:
                raise ValueError("result status must be completed or failed")
            node = self._nodes[task_id]
            if status == "completed":
                incomplete = [
                    parent for parent in node.depends_on if self._nodes[parent].status != "completed"
                ]
                if incomplete:
                    raise ValueError(
                        f"task {task_id} cannot complete before dependencies: {incomplete}"
                    )
            resolved_evidence = deepcopy(evidence or _evidence_from_result(result))
            if status == "completed" and not resolved_evidence:
                raise ValueError(
                    "completed result requires explicit evidence; ToDAG refuses evidence-free completion"
                )
            node.result = deepcopy(result)
            node.evidence = resolved_evidence
            node.status = status
            node.recompute_reason = None
            self._record_node_status(
                task_id,
                status,
                operation="set_node_result",
                reason="result endpoint accepted explicit node result status",
            )
            if status == "completed":
                self._release_stale_nodes()
            self._change_set = {
                **self._empty_change_set(),
                "preserved_node_ids": sorted(self._nodes),
            }
            return self.snapshot()

    def invalidate_node(self, task_id: str, reason: str) -> dict[str, Any]:
        """Invalidate one node/evidence producer and only its affected descendants."""
        with self._lock:
            if task_id not in self._nodes:
                raise KeyError(task_id)
            reason = str(reason).strip()
            if not reason:
                raise ValueError("reason must be non-empty")
            old_nodes = deepcopy(self._nodes)
            candidate = deepcopy(self._nodes)
            order, terminal, validation_trace = self.validation_agent.run(candidate)
            plan, recompute_trace = self.recompute_agent.run(old_nodes, candidate, task_id)
            self._mark_recompute(
                candidate,
                old_nodes,
                {task_id},
                plan.order,
                reason=f"invalidated:{reason}",
            )
            self._refresh_fingerprints(candidate, order, plan.affected)
            self._nodes = candidate
            self._terminal_node_id = terminal
            self._revision += 1
            self._planner_trace.extend([validation_trace, recompute_trace])
            self._change_set = {
                "changed_node_ids": [task_id],
                "added_node_ids": [],
                "removed_node_ids": [],
                "invalidated_node_ids": sorted(plan.invalidated),
                "recomputed_node_ids": plan.order,
                "preserved_node_ids": sorted(plan.preserved),
            }
            return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            if self._specification is None:
                return {"schema_version": "2.0", "status": "empty", "status_provenance": deepcopy(self._dag_status_provenance), "revision": 0}
            order, terminal, _ = self.validation_agent.run(self._nodes)
            level_map = graph_levels(self._nodes, order)
            entries = [task_id for task_id in order if not self._nodes[task_id].depends_on]
            graph_edges = edges(self._nodes)
            typed_counts: dict[str, int] = {}
            for edge in graph_edges:
                typed_counts[edge["type"]] = typed_counts.get(edge["type"], 0) + 1
            status = "needs_clarification" if self._needs_clarification else "ready"
            return {
                "schema_version": "2.0",
                "taskgraph_profile": "mosaic-dynamic-v1",
                "status": status,
                "status_provenance": deepcopy(self._dag_status_provenance),
                "revision": self._revision,
                # The nested input remains exactly the frozen six-field GoalSpec.
                "input": self._specification.to_dict(),
                "nodes": [
                    {
                        **self._nodes[task_id].to_dict(),
                        "level": level_map[task_id],
                        "status_provenance": deepcopy(self._node_status_provenance.get(task_id, {})),
                    }
                    for task_id in order
                ],
                "edges": graph_edges,
                "entry_task_ids": entries,
                "final_task_id": terminal,
                "topological_order": order,
                "ready_task_ids": ready_task_ids(self._nodes),
                "rolling_window_task_ids": rolling_window(self._nodes, self._planning_horizon),
                "planning_horizon": self._planning_horizon,
                "critical_path": critical_path(self._nodes),
                "graph_metrics": {
                    "node_count": len(self._nodes),
                    "edge_count": len(graph_edges),
                    "typed_edge_counts": typed_counts,
                },
                "change_set": deepcopy(self._change_set),
                "planner_trace": deepcopy(self._planner_trace),
            }

    @staticmethod
    def traceability_contract() -> dict[str, Any]:
        """Return the UI-to-backend truth contract for the ToDAG console."""
        return {
            "numeric_fields": [
                {"ui": "revision", "endpoint": "/api/dag", "field": "revision", "calculation": "build sets revision=1; update_node/update_specification/invalidate_node increment by one; result updates do not alter topology revision"},
                {"ui": "node count", "endpoint": "/api/dag", "field": "nodes", "calculation": "len(nodes)"},
                {"ui": "edge count", "endpoint": "/api/dag", "field": "edges", "calculation": "len(edges)"},
                {"ui": "changed count", "endpoint": "/api/dag", "field": "change_set.changed_node_ids", "calculation": "len(changed_node_ids)"},
                {"ui": "recomputed count", "endpoint": "/api/dag", "field": "change_set.recomputed_node_ids", "calculation": "len(recomputed_node_ids)"},
                {"ui": "invalidated count", "endpoint": "/api/dag", "field": "change_set.invalidated_node_ids", "calculation": "len(invalidated_node_ids)"},
                {"ui": "preserved count", "endpoint": "/api/dag", "field": "change_set.preserved_node_ids", "calculation": "len(preserved_node_ids)"},
                {"ui": "node level", "endpoint": "/api/dag", "field": "nodes[].level", "calculation": "graph_levels(nodes, topological_order)[task_id]"},
                {"ui": "node version", "endpoint": "/api/dag", "field": "nodes[].version", "calculation": "incremented only for nodes recomputed by an accepted structural/specification change"},
                {"ui": "node priority", "endpoint": "/api/dag", "field": "nodes[].priority", "calculation": "direct backend field; after local user edit it is explicitly an unsubmitted draft until PATCH succeeds"},
                {"ui": "zoom percent", "endpoint": "client-only", "field": "#zoom.value", "calculation": "local view scale only: state.zoom = Number(value)/100; it is not a backend metric"},
            ],
            "statuses": [
                {"ui": "DAG status", "endpoint": "/api/dag", "field": "status + status_provenance", "entered_when": "empty at engine initialization; needs_clarification when requirement compilation requests clarification; ready after validated GoalSpec/DAG compilation", "source": "ToDAGEngine"},
                {"ui": "node status", "endpoint": "/api/dag", "field": "nodes[].status + nodes[].status_provenance", "entered_when": "recorded by build, incremental_recompute, release_stale_node, or set_node_result; completed is rejected unless explicit evidence is supplied", "source": "ToDAGEngine"},
            ],
            "animations": [],
            "buttons": [
                {"id": "build", "action": "POST /api/build", "result": "replace UI DAG with returned authoritative snapshot"},
                {"id": "replan", "action": "PUT /api/specification", "result": "locally replan changed impact closure and render returned snapshot"},
                {"id": "format", "action": "client-only JSON parse/stringify", "result": "format input only; no backend state mutation"},
                {"id": "export", "action": "client-only download of current /api/dag snapshot", "result": "download current ready DAG"},
                {"id": "export-execution", "action": "GET /api/execution-plan", "result": "download exact backend execution-plan response"},
                {"id": "update-node", "action": "PATCH /api/nodes/{task_id}", "result": "render returned incrementally recomputed snapshot"},
            ],
        }

    def execution_plan(self) -> list[dict[str, Any]]:
        snapshot = self.snapshot()
        if snapshot.get("status") == "empty":
            raise RuntimeError("build a DAG before exporting it")
        if snapshot.get("status") == "needs_clarification":
            raise RuntimeError("GoalSpec needs clarification before execution-plan export")
        active_window = set(snapshot["rolling_window_task_ids"])
        plan: list[dict[str, Any]] = []
        for node in snapshot["nodes"]:
            placement = {
                key: deepcopy(value)
                for key, value in node["resource_requirements"].items()
                if key in _PLACEMENT_FIELDS
            }
            plan.append(
                {
                    # Project the planner node into the single execution-boundary contract.
                    "task_id": node["task_id"],
                    "description": node["description"],
                    "required_skill": node["required_skill"],
                    "required_skills": list(node["required_skills"]),
                    "depends_on": list(node["depends_on"]),
                    "priority": node["priority"],
                    "placement": placement,
                    # Planner-only detail stays in metadata; execution state remains owned by EventStore.
                    "metadata": {
                        "todag_revision": snapshot["revision"],
                        "todag_node_version": node["version"],
                        "node_type": node["node_type"],
                        "semantic_key": node["semantic_key"],
                        "dependency_types": deepcopy(node["dependency_types"]),
                        "evidence_dependencies": list(node["evidence_dependencies"]),
                        "mutex_with": list(node["mutex_with"]),
                        "acceptance_conditions": list(node["acceptance_conditions"]),
                        "acceptance_predicates": deepcopy(node["acceptance_predicates"]),
                        "hard_constraints": list(node["hard_constraints"]),
                        "soft_preferences": list(node["soft_preferences"]),
                        "budget": deepcopy(node["budget"]),
                        "prohibitions": list(node["prohibitions"]),
                        "inputs": deepcopy(node["inputs"]),
                        "outputs": deepcopy(node["outputs"]),
                        "source_refs": deepcopy(node["source_refs"]),
                        "risk": deepcopy(node["risk"]),
                        "estimated_cost": deepcopy(node["estimated_cost"]),
                        "resource_requirements": deepcopy(node["resource_requirements"]),
                        "candidate_executors": list(node["candidate_executors"]),
                        "rollback_checkpoint": bool(node["rollback_checkpoint"]),
                        "in_rolling_window": node["task_id"] in active_window,
                    },
                }
            )
        return plan


def _evidence_from_result(result: Any) -> list[dict[str, Any]]:
    if not isinstance(result, Mapping):
        return []
    raw = result.get("evidence") or result.get("evidence_refs")
    if raw is None:
        return []
    if isinstance(raw, Mapping):
        return [deepcopy(dict(raw))]
    if isinstance(raw, list):
        return [deepcopy(dict(item)) for item in raw if isinstance(item, Mapping)]
    return []
