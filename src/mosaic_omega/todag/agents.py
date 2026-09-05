"""Deterministic compiler stages that transform GoalSpec into a replayable TaskGraph.

These stages are not runtime execution Agents and must never be presented as such in UI/evidence.
"""

from __future__ import annotations

import hashlib
import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .graph import affected_closure, critical_path, topological_sort, validate_single_terminal
from .models import DAGNode, LongTaskInput, item_text, rich_metadata


class RequirementAgent:
    name = "requirement_agent"

    def run(self, raw: Mapping[str, Any]) -> tuple[LongTaskInput, dict[str, Any]]:
        specification = LongTaskInput.from_dict(raw)
        # GoalSpec is the semantic authority.  ToDAG does not invent corrections;
        # it only reports exact duplicate declarations and explicit conflict tags.
        hard = {item.casefold() for item in specification.hard_constraint_texts}
        prohibited = {item.casefold() for item in specification.prohibition_texts}
        duplicate_policy = sorted(hard & prohibited)
        explicit_conflicts: list[str] = []
        for collection_name, items in (
            ("hard_constraints", specification.hard_constraints),
            ("prohibitions", specification.prohibitions),
        ):
            for index, item in enumerate(items):
                metadata = rich_metadata(item)
                conflict = metadata.get("conflict") or metadata.get("conflicts_with")
                if conflict:
                    explicit_conflicts.append(f"{collection_name}[{index}]: {conflict}")
        if explicit_conflicts:
            raise ValueError(f"GoalSpec contains explicit unresolved conflicts: {explicit_conflicts}")

        needs_clarification = not specification.acceptance_conditions
        return specification, {
            "stage": self.name,
            "executor_mode": "deterministic_compiler_stage",
            "action": "validate_frozen_six_field_goalspec",
            "top_level_fields": list(specification.to_dict()),
            "hard_constraint_count": len(specification.hard_constraints),
            "acceptance_condition_count": len(specification.acceptance_conditions),
            "duplicate_hard_and_prohibition": duplicate_policy,
            "needs_clarification": needs_clarification,
        }


class DecompositionAgent:
    name = "decomposition_agent"

    _SKILL_KEYWORDS: dict[str, tuple[str, ...]] = {
        "search": ("search", "research", "source", "survey", "retrieve", "检索", "调研", "搜索", "资料"),
        "code": ("code", "implement", "program", "software", "api", "patch", "代码", "实现", "修复", "编译"),
        "calculation": ("calculate", "budget", "cost", "metric", "number", "统计", "计算", "指标", "成本"),
        "report": ("report", "document", "write", "summary", "报告", "文档", "总结", "交付"),
        "review": ("review", "verify", "validate", "test", "acceptance", "审查", "验收", "验证", "测试"),
        "robotics": ("ros", "robot", "navigation", "slam", "机器人", "导航", "雷达", "机械臂"),
        "data": ("data", "dataset", "clean", "analysis", "数据", "清洗", "分析", "建模"),
        "plan": ("plan", "decompose", "scope", "requirement", "规划", "拆分", "需求", "范围"),
    }

    @classmethod
    def _skill(cls, text: str) -> str:
        folded = text.casefold()
        for skill, words in cls._SKILL_KEYWORDS.items():
            if any(word.casefold() in folded for word in words):
                return skill
        return "analysis"

    @staticmethod
    def _stable_id(prefix: str, semantic_key: str) -> str:
        digest = hashlib.sha1(semantic_key.encode("utf-8")).hexdigest()[:10]
        return f"task_{prefix}_{digest}"

    @staticmethod
    def _source_refs(item: Any, field_name: str, index: int) -> list[dict[str, Any]]:
        metadata = rich_metadata(item)
        ref: dict[str, Any] = {"field": field_name, "index": index}
        for key in ("source_span", "source", "version", "confidence"):
            if key in metadata:
                ref[key] = deepcopy(metadata[key])
        return [ref]

    @staticmethod
    def _predicate(item: Any, condition: str) -> dict[str, Any]:
        metadata = rich_metadata(item)
        predicate = metadata.get("predicate")
        check_type = metadata.get("check_type") or metadata.get("checker")
        expected = metadata.get("expected_result")
        args = deepcopy(metadata.get("args", {})) if isinstance(metadata.get("args", {}), Mapping) else {}
        if isinstance(predicate, Mapping):
            result = deepcopy(dict(predicate))
            result.setdefault("condition", condition)
            if check_type is not None:
                result.setdefault("check_type", check_type)
            if expected is not None:
                result.setdefault("expected_result", deepcopy(expected))
            return result
        if not isinstance(predicate, str) or not predicate.strip():
            folded = condition.casefold()
            if any(word in folded for word in ("pytest", "test", "测试", "单测")):
                predicate = "test_pass"
                check_type = check_type or "execution"
            elif any(word in folded for word in ("build", "compile", "colcon", "编译", "构建")):
                predicate = "build_success"
                check_type = check_type or "execution"
            elif any(word in folded for word in ("file", "report", "document", "artifact", "文件", "报告", "交付物")):
                predicate = "artifact_exists"
                check_type = check_type or "artifact"
            elif any(word in folded for word in ("evidence", "source", "reference", "证据", "来源", "引用")):
                predicate = "evidence_present"
                check_type = check_type or "evidence"
            else:
                predicate = "condition_satisfied"
                check_type = check_type or "verifier"
        result = {
            "condition": condition,
            "predicate": predicate.strip(),
            "check_type": str(check_type),
            "args": args,
        }
        if expected is not None:
            result["expected_result"] = deepcopy(expected)
        confidence = metadata.get("confidence")
        if isinstance(confidence, (int, float)) and not isinstance(confidence, bool):
            result["confidence"] = float(confidence)
        return result

    @staticmethod
    def _risk(text: str, metadata: Mapping[str, Any], inherited_constraints: Sequence[str]) -> dict[str, Any]:
        raw = metadata.get("risk")
        if isinstance(raw, Mapping):
            result = deepcopy(dict(raw))
            result.setdefault("level", "medium")
            result.setdefault("reasons", [])
            return result
        if isinstance(raw, str) and raw.strip().lower() in {"low", "medium", "high", "critical"}:
            return {"level": raw.strip().lower(), "reasons": ["declared_by_goalspec"]}
        folded = " ".join([text, *inherited_constraints]).casefold()
        critical_words = ("safety", "安全", "credential", "secret", "密码", "密钥", "payment", "支付")
        high_words = ("privacy", "隐私", "sensitive", "敏感", "delete", "删除", "deploy", "部署", "interface", "接口")
        medium_words = ("modify", "write", "patch", "cost", "修改", "写入", "修复", "成本")
        if any(word in folded for word in critical_words):
            return {"level": "critical", "reasons": ["safety_or_secret_sensitive"]}
        if any(word in folded for word in high_words):
            return {"level": "high", "reasons": ["privacy_or_irreversible_change"]}
        if any(word in folded for word in medium_words):
            return {"level": "medium", "reasons": ["state_or_cost_change"]}
        return {"level": "low", "reasons": []}

    @staticmethod
    def _resource_requirements(
        metadata: Mapping[str, Any],
        specification: LongTaskInput,
    ) -> dict[str, Any]:
        raw = metadata.get("resource_requirements") or metadata.get("placement") or {}
        result = deepcopy(dict(raw)) if isinstance(raw, Mapping) else {}
        budget = specification.budget
        for key in (
            "allowed_tiers",
            "preferred_tier",
            "max_latency_ms",
            "min_memory_mb",
            "min_gpu_count",
            "data_sensitivity",
            "require_local_data",
        ):
            if key not in result and key in budget:
                result[key] = deepcopy(budget[key])

        # Only use deterministic privacy metadata/phrasing; this is a guard, not
        # a free-form semantic re-interpretation of GoalSpec.
        privacy_local = False
        for item in (*specification.hard_constraints, *specification.prohibitions):
            meta = rich_metadata(item)
            text = item_text(
                item,
                text_keys=("constraint", "prohibition", "text", "description", "condition", "rule"),
            ).casefold()
            kind = str(meta.get("type", meta.get("category", ""))).casefold()
            pred = str(meta.get("predicate", meta.get("check_method", ""))).casefold()
            if kind == "privacy" and any(token in pred for token in ("local", "no_upload", "not_upload")):
                privacy_local = True
            if any(token in text for token in ("不得上传", "不能上传", "仅本地", "local only", "must stay local")):
                privacy_local = True
        if privacy_local:
            result.setdefault("require_local_data", True)
            result.setdefault("allowed_tiers", ["device", "edge"])
            result.setdefault("data_sensitivity", "restricted")
        return result

    @staticmethod
    def _estimated_cost(metadata: Mapping[str, Any]) -> dict[str, Any]:
        raw = metadata.get("estimated_cost")
        if isinstance(raw, Mapping):
            result = deepcopy(dict(raw))
        else:
            result = {}
        if "duration_s" not in result:
            duration = metadata.get("estimated_duration_s", metadata.get("duration_s", 1.0))
            if isinstance(duration, (int, float)) and not isinstance(duration, bool) and duration >= 0:
                result["duration_s"] = float(duration)
            else:
                result["duration_s"] = 1.0
        return result

    def run(self, specification: LongTaskInput) -> tuple[dict[str, DAGNode], dict[str, Any]]:
        hard_texts = list(specification.hard_constraint_texts)
        soft_texts = list(specification.soft_preference_texts)
        prohibition_texts = list(specification.prohibition_texts)

        def common() -> dict[str, Any]:
            return {
                "hard_constraints": list(hard_texts),
                "soft_preferences": list(soft_texts),
                "budget": deepcopy(specification.budget),
                "prohibitions": list(prohibition_texts),
            }

        requirement_id = "task_requirements"
        requirement_predicate = {
            "condition": "GoalSpec is complete, internally consistent, and represented without dropping hard constraints",
            "predicate": "goalspec_integrity",
            "check_type": "schema_and_rule",
            "expected_result": True,
        }
        nodes: dict[str, DAGNode] = {
            requirement_id: DAGNode(
                task_id=requirement_id,
                title="Requirement baseline",
                description=f"Freeze scope, constraints, budget, and prohibitions for: {specification.main_goal_text}",
                required_skill="plan",
                required_skills=["plan"],
                agent_role="requirement_compiler_role",
                node_type="milestone",
                semantic_key="requirement_baseline",
                depends_on=[],
                priority=10,
                inputs=[{"source": "GoalSpec", "fields": list(specification.to_dict())}],
                outputs=["frozen_requirement_baseline"],
                acceptance_conditions=[requirement_predicate["condition"]],
                acceptance_predicates=[requirement_predicate],
                risk={"level": "high", "reasons": ["hard_constraints_enter_execution_chain"]},
                estimated_cost={"duration_s": 0.2},
                rollback_checkpoint=True,
                **common(),
            )
        }

        explicit_subgoals = list(specification.sub_goals)
        if explicit_subgoals:
            work_items = explicit_subgoals
            work_source = "main_goal.sub_goals"
        else:
            # Deterministic fallback: GoalSpec acceptance conditions become the
            # smallest verifiable work packages.  ToDAG does not invent domain
            # steps that were absent from GoalSpec.
            work_items = list(specification.acceptance_conditions)
            work_source = "acceptance_conditions"

        work_ids: list[str] = []
        work_text_to_id: dict[str, str] = {}
        work_meta_by_id: dict[str, dict[str, Any]] = {}
        pending_dependency_refs: dict[str, list[str]] = {}
        pending_mutex_refs: dict[str, list[str]] = {}

        if not work_items:
            # Keep a structurally valid graph, but the engine will expose
            # NEED_CLARIFICATION and refuse execution-plan export.
            work_items = ["Clarify executable sub-goals and acceptance conditions"]
            work_source = "generated_clarification_guard"

        for index, item in enumerate(work_items):
            text = item_text(
                item,
                text_keys=("name", "text", "goal", "description", "objective", "condition", "predicate"),
            )
            metadata = rich_metadata(item)
            semantic_key = str(metadata.get("semantic_key") or f"work:{text.casefold()}")
            task_id = self._stable_id("work", semantic_key)
            if task_id in nodes:
                task_id = f"{task_id}_{index + 1}"
            work_ids.append(task_id)
            work_text_to_id[text.casefold()] = task_id
            work_text_to_id[semantic_key.casefold()] = task_id
            work_meta_by_id[task_id] = metadata

            required_skills_raw = metadata.get("required_skills")
            if isinstance(required_skills_raw, str):
                required_skills = [required_skills_raw]
            elif isinstance(required_skills_raw, list):
                required_skills = [str(skill) for skill in required_skills_raw if str(skill).strip()]
            else:
                required_skills = []
            primary_skill = str(metadata.get("required_skill") or (required_skills[0] if required_skills else self._skill(text)))
            if not required_skills:
                required_skills = [primary_skill]

            raw_depends = metadata.get("depends_on", [])
            if isinstance(raw_depends, str):
                raw_depends = [raw_depends]
            pending_dependency_refs[task_id] = [str(ref) for ref in raw_depends] if isinstance(raw_depends, list) else []
            raw_mutex = metadata.get("mutex_with", [])
            if isinstance(raw_mutex, str):
                raw_mutex = [raw_mutex]
            pending_mutex_refs[task_id] = [str(ref) for ref in raw_mutex] if isinstance(raw_mutex, list) else []

            source_refs = []
            if work_source == "main_goal.sub_goals":
                source_refs = [{"field": work_source, "index": index}]
            elif work_source == "acceptance_conditions":
                source_refs = self._source_refs(item, work_source, index)

            nodes[task_id] = DAGNode(
                task_id=task_id,
                title=str(metadata.get("title") or text[:80]),
                description=str(metadata.get("description") or f"Execute verifiable work package: {text}"),
                required_skill=primary_skill,
                required_skills=required_skills,
                agent_role=str(metadata.get("agent_role") or "execution_role_hint"),
                node_type=str(metadata.get("node_type") or "work"),
                semantic_key=semantic_key,
                depends_on=[requirement_id],
                dependency_types={requirement_id: "data"},
                priority=int(metadata.get("priority", max(5, 9 - min(index, 4)))),
                inputs=deepcopy(metadata.get("inputs", ["frozen_requirement_baseline"])),
                outputs=deepcopy(metadata.get("outputs", [f"result:{semantic_key}", f"evidence:{semantic_key}"])),
                resource_requirements=self._resource_requirements(metadata, specification),
                risk=self._risk(text, metadata, [*hard_texts, *prohibition_texts]),
                estimated_cost=self._estimated_cost(metadata),
                candidate_executors=[str(item) for item in metadata.get("candidate_executors", [])]
                if isinstance(metadata.get("candidate_executors", []), list)
                else [],
                source_refs=source_refs,
                rollback_checkpoint=bool(metadata.get("rollback_checkpoint", False)),
                **common(),
            )

        # Resolve optional subgoal-to-subgoal references after every stable ID is known.
        for task_id in work_ids:
            node = nodes[task_id]
            refs = pending_dependency_refs.get(task_id, [])
            resolved: list[str] = []
            for ref in refs:
                candidate = work_text_to_id.get(ref.casefold())
                if candidate is None and ref in nodes:
                    candidate = ref
                if candidate and candidate != task_id:
                    resolved.append(candidate)
            if resolved:
                node.depends_on = [requirement_id, *dict.fromkeys(resolved)]
                node.dependency_types = {requirement_id: "data", **{parent: "exec" for parent in resolved}}
            mutex_resolved: list[str] = []
            for ref in pending_mutex_refs.get(task_id, []):
                candidate = work_text_to_id.get(ref.casefold())
                if candidate is None and ref in nodes:
                    candidate = ref
                if candidate and candidate != task_id:
                    mutex_resolved.append(candidate)
            node.mutex_with = list(dict.fromkeys(mutex_resolved))

        verification_ids: list[str] = []
        for index, item in enumerate(specification.acceptance_conditions):
            condition = item_text(
                item,
                text_keys=("condition", "text", "description", "predicate", "name"),
            )
            metadata = rich_metadata(item)
            semantic_key = str(metadata.get("semantic_key") or f"verify:{condition.casefold()}")
            task_id = self._stable_id("verify", semantic_key)
            if task_id in nodes:
                task_id = f"{task_id}_{index + 1}"

            target_refs = metadata.get("depends_on", metadata.get("target_sub_goals", []))
            if isinstance(target_refs, str):
                target_refs = [target_refs]
            target_ids: list[str] = []
            if isinstance(target_refs, list):
                for ref in target_refs:
                    ref_text = str(ref)
                    target = work_text_to_id.get(ref_text.casefold())
                    if target is None and ref_text in nodes:
                        target = ref_text
                    if target:
                        target_ids.append(target)
            if not target_ids:
                # When no explicit HTN/sub-goal structure is available, each
                # fallback work package came from the same acceptance item; keep
                # verification local instead of creating an unnecessary all-to-all
                # evidence barrier.  With explicit sub-goals, conservative default
                # verification still sees all work unless GoalSpec supplies targets.
                target_ids = [work_ids[index]] if not explicit_subgoals and index < len(work_ids) else list(work_ids)

            predicate = self._predicate(item, condition)
            verification_ids.append(task_id)
            nodes[task_id] = DAGNode(
                task_id=task_id,
                title=f"Verify: {condition[:72]}",
                description=f"Independently verify evidence for acceptance condition: {condition}",
                required_skill="review",
                required_skills=["review"],
                agent_role=str(metadata.get("verifier_role") or "validation_role_hint"),
                node_type="verification",
                semantic_key=semantic_key,
                depends_on=list(dict.fromkeys(target_ids)),
                dependency_types={parent: "evidence" for parent in target_ids},
                evidence_dependencies=list(dict.fromkeys(target_ids)),
                priority=int(metadata.get("verification_priority", 8)),
                inputs=[f"evidence:{parent}" for parent in target_ids],
                outputs=[f"verification:{semantic_key}"],
                acceptance_conditions=[condition],
                acceptance_predicates=[predicate],
                resource_requirements=self._resource_requirements(metadata, specification),
                risk=self._risk(condition, metadata, [*hard_texts, *prohibition_texts]),
                estimated_cost={"duration_s": float(metadata.get("verification_duration_s", 0.5))}
                if isinstance(metadata.get("verification_duration_s", 0.5), (int, float))
                else {"duration_s": 0.5},
                source_refs=self._source_refs(item, "acceptance_conditions", index),
                **common(),
            )

        final_id = "task_final_review"
        final_dependencies = verification_ids or work_ids
        final_acceptance = list(specification.acceptance_condition_texts)
        if not final_acceptance:
            final_acceptance = ["GoalSpec must supply at least one executable acceptance condition"]
        final_predicates = [
            self._predicate(item, condition)
            for item, condition in zip(specification.acceptance_conditions, specification.acceptance_condition_texts)
        ]
        if not final_predicates:
            final_predicates = [
                {
                    "condition": final_acceptance[0],
                    "predicate": "need_clarification",
                    "check_type": "guard",
                    "expected_result": False,
                }
            ]

        nodes[final_id] = DAGNode(
            task_id=final_id,
            title="Final integration and acceptance",
            description=f"Integrate verified deliverables and prove the main goal: {specification.main_goal_text}",
            required_skill="review",
            required_skills=["review", "report"],
            agent_role="validation_role_hint",
            node_type="milestone",
            semantic_key="final_integration",
            depends_on=list(final_dependencies),
            dependency_types={parent: "evidence" for parent in final_dependencies},
            evidence_dependencies=list(final_dependencies),
            priority=10,
            inputs=[f"verification:{parent}" for parent in final_dependencies],
            outputs=["final_deliverable", "evidence_manifest"],
            acceptance_conditions=final_acceptance,
            acceptance_predicates=final_predicates,
            resource_requirements=self._resource_requirements({}, specification),
            risk={"level": "high", "reasons": ["final_acceptance_gate"]},
            estimated_cost={"duration_s": 0.5},
            rollback_checkpoint=True,
            **common(),
        )

        # Make mutex declarations symmetric for schedulers/visualisation.
        for task_id, node in list(nodes.items()):
            for peer in list(node.mutex_with):
                if peer in nodes and task_id not in nodes[peer].mutex_with:
                    nodes[peer].mutex_with.append(task_id)

        return nodes, {
            "stage": self.name,
            "executor_mode": "deterministic_compiler_stage",
            "action": "compile_goalspec_to_typed_taskgraph",
            "used_explicit_sub_goals": bool(explicit_subgoals),
            "work_node_ids": work_ids,
            "verification_node_ids": verification_ids,
            "created_node_ids": list(nodes),
            "needs_clarification": not specification.acceptance_conditions,
        }


class DAGValidationAgent:
    name = "dag_validation_agent"

    def run(self, nodes: Mapping[str, DAGNode]) -> tuple[list[str], str, dict[str, Any]]:
        order = topological_sort(nodes)
        terminal = validate_single_terminal(nodes)
        path = critical_path(nodes)
        return order, terminal, {
            "stage": self.name,
            "executor_mode": "deterministic_compiler_stage",
            "action": "validate_typed_dag_and_topologically_sort",
            "topological_order": order,
            "terminal_node_id": terminal,
            "critical_path": path,
        }


@dataclass(frozen=True)
class RecomputePlan:
    affected: set[str]
    invalidated: set[str]
    preserved: set[str]
    order: list[str]


class IncrementalRecomputeAgent:
    name = "incremental_recompute_agent"

    def run(
        self,
        old_nodes: Mapping[str, DAGNode],
        new_nodes: Mapping[str, DAGNode],
        changed_task_ids: str | Sequence[str],
    ) -> tuple[RecomputePlan, dict[str, Any]]:
        changed = [changed_task_ids] if isinstance(changed_task_ids, str) else list(changed_task_ids)
        affected: set[str] = set(changed)
        affected.update(affected_closure(old_nodes, changed))
        affected.update(affected_closure(new_nodes, changed))
        affected.intersection_update(new_nodes)
        order = [task_id for task_id in topological_sort(new_nodes) if task_id in affected]
        plan = RecomputePlan(
            affected=affected,
            invalidated=affected - set(changed),
            preserved=set(new_nodes) - affected,
            order=order,
        )
        return plan, {
            "stage": self.name,
            "executor_mode": "deterministic_compiler_stage",
            "action": "recompute_changed_nodes_and_impact_closure",
            "changed_node_ids": sorted(set(changed)),
            "recomputed_node_ids": order,
            "invalidated_node_ids": sorted(plan.invalidated),
            "preserved_node_ids": sorted(plan.preserved),
        }
