"""Data contracts for the ToDAG layer.

The external GoalSpec contract deliberately stays fixed at exactly six top-level
fields.  The values inside those fields may be either the original compact
strings or richer objects produced by an upgraded GoalSpec compiler.
"""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

INPUT_FIELDS = (
    "main_goal",
    "hard_constraints",
    "soft_preferences",
    "acceptance_conditions",
    "budget",
    "prohibitions",
)
INPUT_FIELD_SET = frozenset(INPUT_FIELDS)
NODE_STATUSES = frozenset(
    {
        "pending",
        "ready",
        "running",
        "verifying",
        "completed",
        "failed",
        "stale",
        "paused",
        # Kept for compatibility with the previous ToDAG build.
        "invalidated",
    }
)
DEPENDENCY_TYPES = frozenset({"exec", "data", "evidence"})
EDGE_TYPES = frozenset({*DEPENDENCY_TYPES, "mutex"})
RISK_LEVELS = frozenset({"low", "medium", "high", "critical"})

#: What a node is expected to hand over.  This is the contract the runtime uses
#: to decide which real tools an Agent may plan, so that a software task can read,
#: patch, build and test instead of only producing prose about doing so.
DELIVERY_KINDS = frozenset({
    "document",       # a written deliverable file
    "software",       # read/modify/build/test source
    "data",           # load data, run analysis, emit results
    "research",       # evidence-backed structured findings
    "robotics",       # drive a simulation / robot interface
    "verification",   # inspect produced artifacts and judge them
    "reasoning",      # no external effect; reasoning persisted as a deliverable
})


def _non_empty_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _validate_json_value(value: Any, path: str) -> Any:
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, (int, float)):
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError(f"{path} must contain finite numbers")
        return value
    if isinstance(value, list):
        return [_validate_json_value(item, f"{path}[]") for item in value]
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            key_text = _non_empty_text(str(key), f"{path}.key")
            result[key_text] = _validate_json_value(item, f"{path}.{key_text}")
        return result
    raise ValueError(f"{path} must contain only JSON-compatible values")


def _validate_budget_value(value: Any, path: str) -> Any:
    value = _validate_json_value(value, path)
    if isinstance(value, (int, float)) and value < 0:
        raise ValueError(f"{path} cannot be negative")
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_budget_value(item, f"{path}[{index}]")
    if isinstance(value, Mapping):
        for key, item in value.items():
            _validate_budget_value(item, f"{path}.{key}")
    return value


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _normalise_rich_item(
    value: Any,
    field_name: str,
    index: int,
    text_keys: Sequence[str],
) -> Any:
    path = f"{field_name}[{index}]"
    if isinstance(value, str):
        return _non_empty_text(value, path)
    if not isinstance(value, Mapping):
        raise ValueError(f"{path} must be a non-empty string or JSON object")
    result = dict(_validate_json_value(value, path))
    text = item_text(result, text_keys=text_keys, required=False)
    if not text:
        raise ValueError(f"{path} object must contain one of: {', '.join(text_keys)}")
    return result


def _rich_list(value: Any, field_name: str, text_keys: Sequence[str]) -> tuple[Any, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be a JSON array")
    result: list[Any] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        normalised = _normalise_rich_item(item, field_name, index, text_keys)
        key = _canonical(normalised)
        if key not in seen:
            seen.add(key)
            result.append(normalised)
    return tuple(result)


def item_text(
    value: Any,
    *,
    text_keys: Sequence[str] = ("text", "description", "condition", "constraint", "name", "objective"),
    required: bool = True,
) -> str:
    """Return the human-readable text from a compact string or rich object."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, Mapping):
        for key in text_keys:
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
    if required:
        raise ValueError(f"cannot extract text from {value!r}")
    return ""


def rich_metadata(value: Any) -> dict[str, Any]:
    return deepcopy(dict(value)) if isinstance(value, Mapping) else {}


@dataclass(frozen=True)
class LongTaskInput:
    """Immutable view of the six-field GoalSpec contract.

    The field names never change.  Rich nested objects are preserved so that
    ToDAG can consume GoalSpec compiler metadata without forcing downstream
    modules to change the six-field interface.
    """

    main_goal: Any
    hard_constraints: tuple[Any, ...]
    soft_preferences: tuple[Any, ...]
    acceptance_conditions: tuple[Any, ...]
    budget: dict[str, Any]
    prohibitions: tuple[Any, ...]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "LongTaskInput":
        if not isinstance(raw, Mapping):
            raise ValueError("input document must be a JSON object")
        missing = [name for name in INPUT_FIELDS if name not in raw]
        if missing:
            raise ValueError(f"input document is missing required fields: {', '.join(missing)}")
        unexpected = sorted(set(raw) - INPUT_FIELD_SET)
        if unexpected:
            raise ValueError(
                "GoalSpec top level must contain only the six frozen fields; "
                f"unexpected fields: {unexpected}"
            )

        main_goal_raw = raw["main_goal"]
        if isinstance(main_goal_raw, str):
            main_goal = _non_empty_text(main_goal_raw, "main_goal")
        elif isinstance(main_goal_raw, Mapping):
            main_goal = dict(_validate_json_value(main_goal_raw, "main_goal"))
            if not item_text(
                main_goal,
                text_keys=("goal_text", "text", "objective", "goal", "description", "name"),
                required=False,
            ):
                raise ValueError(
                    "main_goal object must contain goal_text/text/objective/goal/description/name"
                )
        else:
            raise ValueError("main_goal must be a non-empty string or JSON object")

        budget = raw["budget"]
        if not isinstance(budget, Mapping):
            raise ValueError("budget must be a JSON object")

        return cls(
            main_goal=main_goal,
            hard_constraints=_rich_list(
                raw["hard_constraints"],
                "hard_constraints",
                ("constraint", "text", "description", "condition", "rule"),
            ),
            soft_preferences=_rich_list(
                raw["soft_preferences"],
                "soft_preferences",
                ("preference", "text", "description", "objective", "constraint"),
            ),
            acceptance_conditions=_rich_list(
                raw["acceptance_conditions"],
                "acceptance_conditions",
                ("condition", "text", "description", "predicate", "name"),
            ),
            budget=dict(_validate_budget_value(budget, "budget")),
            prohibitions=_rich_list(
                raw["prohibitions"],
                "prohibitions",
                ("prohibition", "text", "description", "constraint", "rule"),
            ),
        )

    @property
    def main_goal_text(self) -> str:
        return item_text(
            self.main_goal,
            text_keys=("goal_text", "text", "objective", "goal", "description", "name"),
        )

    @property
    def main_goal_metadata(self) -> dict[str, Any]:
        return rich_metadata(self.main_goal)

    @property
    def sub_goals(self) -> tuple[Any, ...]:
        metadata = self.main_goal_metadata
        raw = metadata.get("sub_goals", metadata.get("subgoals", ()))
        if raw is None:
            return ()
        if isinstance(raw, (str, Mapping)):
            raw = [raw]
        if not isinstance(raw, list):
            return ()
        result: list[Any] = []
        for index, item in enumerate(raw):
            try:
                result.append(
                    _normalise_rich_item(
                        item,
                        "main_goal.sub_goals",
                        index,
                        ("name", "text", "goal", "description", "objective"),
                    )
                )
            except ValueError:
                continue
        return tuple(result)

    @property
    def deliverables(self) -> tuple[Any, ...]:
        metadata = self.main_goal_metadata
        raw = metadata.get("deliverables", ())
        if isinstance(raw, str):
            return (raw.strip(),) if raw.strip() else ()
        if isinstance(raw, list):
            return tuple(deepcopy(raw))
        return ()

    @property
    def hard_constraint_texts(self) -> tuple[str, ...]:
        return tuple(
            item_text(item, text_keys=("constraint", "text", "description", "condition", "rule"))
            for item in self.hard_constraints
        )

    @property
    def soft_preference_texts(self) -> tuple[str, ...]:
        return tuple(
            item_text(item, text_keys=("preference", "text", "description", "objective", "constraint"))
            for item in self.soft_preferences
        )

    @property
    def acceptance_condition_texts(self) -> tuple[str, ...]:
        return tuple(
            item_text(item, text_keys=("condition", "text", "description", "predicate", "name"))
            for item in self.acceptance_conditions
        )

    @property
    def prohibition_texts(self) -> tuple[str, ...]:
        return tuple(
            item_text(item, text_keys=("prohibition", "text", "description", "constraint", "rule"))
            for item in self.prohibitions
        )

    def to_dict(self) -> dict[str, Any]:
        # Exactly six top-level keys by design.
        return {
            "main_goal": deepcopy(self.main_goal),
            "hard_constraints": deepcopy(list(self.hard_constraints)),
            "soft_preferences": deepcopy(list(self.soft_preferences)),
            "acceptance_conditions": deepcopy(list(self.acceptance_conditions)),
            "budget": deepcopy(self.budget),
            "prohibitions": deepcopy(list(self.prohibitions)),
        }


@dataclass
class DAGNode:
    # Core planner fields. ``required_skill`` is the primary capability;
    task_id: str
    title: str
    description: str
    required_skill: str
    agent_role: str
    depends_on: list[str]
    priority: int

    # GoalSpec-derived policy fields.
    hard_constraints: list[str] = field(default_factory=list)
    soft_preferences: list[str] = field(default_factory=list)
    acceptance_conditions: list[str] = field(default_factory=list)
    budget: dict[str, Any] = field(default_factory=dict)
    prohibitions: list[str] = field(default_factory=list)

    # Rich TaskGraph fields required by MOSAIC-style scheduling/recovery/verification.
    node_type: str = "task"
    semantic_key: str = ""
    required_skills: list[str] = field(default_factory=list)
    #: What this node is expected to produce, which decides the tools its Agent
    #: may plan (document / software / data / research / robotics / verification /
    #: reasoning).  Without it every node fell back to persisting Agent prose.
    delivery_kind: str = "reasoning"
    #: ToolRuntime permissions this node needs.  The scheduler treats them as a
    #: hard constraint, so a document Agent is never handed a build task.
    required_permissions: list[str] = field(default_factory=list)
    inputs: list[Any] = field(default_factory=list)
    outputs: list[Any] = field(default_factory=list)
    dependency_types: dict[str, str] = field(default_factory=dict)
    evidence_dependencies: list[str] = field(default_factory=list)
    mutex_with: list[str] = field(default_factory=list)
    resource_requirements: dict[str, Any] = field(default_factory=dict)
    risk: dict[str, Any] = field(default_factory=lambda: {"level": "low", "reasons": []})
    estimated_cost: dict[str, Any] = field(default_factory=dict)
    candidate_executors: list[str] = field(default_factory=list)
    acceptance_predicates: list[dict[str, Any]] = field(default_factory=list)
    source_refs: list[dict[str, Any]] = field(default_factory=list)
    rollback_checkpoint: bool = False

    # Runtime/replay fields.
    status: str = "pending"
    version: int = 1
    result: Any = None
    evidence: list[dict[str, Any]] = field(default_factory=list)
    fingerprint: str = ""
    recompute_reason: str | None = None

    def validate(self) -> None:
        self.task_id = _non_empty_text(self.task_id, "task_id")
        self.title = _non_empty_text(self.title, f"{self.task_id}.title")
        self.description = _non_empty_text(self.description, f"{self.task_id}.description")
        self.required_skill = _non_empty_text(self.required_skill, f"{self.task_id}.required_skill")
        self.agent_role = _non_empty_text(self.agent_role, f"{self.task_id}.agent_role")
        self.node_type = _non_empty_text(self.node_type, f"{self.task_id}.node_type")
        self.semantic_key = (self.semantic_key or self.task_id).strip()

        self.depends_on = _unique_text_list(self.depends_on, f"{self.task_id}.depends_on")
        self.evidence_dependencies = _unique_text_list(
            self.evidence_dependencies, f"{self.task_id}.evidence_dependencies"
        )
        self.mutex_with = _unique_text_list(self.mutex_with, f"{self.task_id}.mutex_with")
        self.hard_constraints = _unique_text_list(
            self.hard_constraints, f"{self.task_id}.hard_constraints"
        )
        self.soft_preferences = _unique_text_list(
            self.soft_preferences, f"{self.task_id}.soft_preferences"
        )
        self.acceptance_conditions = _unique_text_list(
            self.acceptance_conditions, f"{self.task_id}.acceptance_conditions"
        )
        self.prohibitions = _unique_text_list(self.prohibitions, f"{self.task_id}.prohibitions")
        self.required_skills = _unique_text_list(
            self.required_skills or [self.required_skill], f"{self.task_id}.required_skills"
        )
        if self.required_skill not in self.required_skills:
            self.required_skills.insert(0, self.required_skill)
        self.candidate_executors = _unique_text_list(
            self.candidate_executors, f"{self.task_id}.candidate_executors"
        )
        self.delivery_kind = _non_empty_text(self.delivery_kind, f"{self.task_id}.delivery_kind").lower()
        if self.delivery_kind not in DELIVERY_KINDS:
            raise ValueError(
                f"{self.task_id}.delivery_kind must be one of {sorted(DELIVERY_KINDS)}"
            )
        self.required_permissions = _unique_text_list(
            self.required_permissions, f"{self.task_id}.required_permissions"
        )

        self.inputs = list(_validate_json_value(self.inputs, f"{self.task_id}.inputs"))
        self.outputs = list(_validate_json_value(self.outputs, f"{self.task_id}.outputs"))
        self.budget = dict(_validate_budget_value(self.budget, f"{self.task_id}.budget"))
        self.resource_requirements = dict(
            _validate_json_value(self.resource_requirements, f"{self.task_id}.resource_requirements")
        )
        self.estimated_cost = dict(
            _validate_json_value(self.estimated_cost, f"{self.task_id}.estimated_cost")
        )
        self.risk = dict(_validate_json_value(self.risk, f"{self.task_id}.risk"))
        risk_level = str(self.risk.get("level", "low")).strip().lower()
        if risk_level not in RISK_LEVELS:
            raise ValueError(f"{self.task_id}.risk.level must be one of {sorted(RISK_LEVELS)}")
        self.risk["level"] = risk_level

        if not isinstance(self.acceptance_predicates, list) or not all(
            isinstance(item, Mapping) for item in self.acceptance_predicates
        ):
            raise ValueError(f"{self.task_id}.acceptance_predicates must be an array of objects")
        self.acceptance_predicates = [
            dict(_validate_json_value(item, f"{self.task_id}.acceptance_predicates[]"))
            for item in self.acceptance_predicates
        ]
        if not isinstance(self.source_refs, list) or not all(isinstance(item, Mapping) for item in self.source_refs):
            raise ValueError(f"{self.task_id}.source_refs must be an array of objects")
        self.source_refs = [
            dict(_validate_json_value(item, f"{self.task_id}.source_refs[]")) for item in self.source_refs
        ]

        if not isinstance(self.dependency_types, Mapping):
            raise ValueError(f"{self.task_id}.dependency_types must be an object")
        dependency_types: dict[str, str] = {}
        for parent, relation in self.dependency_types.items():
            parent_id = _non_empty_text(str(parent), f"{self.task_id}.dependency_types.key")
            relation_text = _non_empty_text(str(relation), f"{self.task_id}.dependency_types.{parent_id}").lower()
            if relation_text not in DEPENDENCY_TYPES:
                raise ValueError(
                    f"{self.task_id}.dependency_types[{parent_id}] must be one of {sorted(DEPENDENCY_TYPES)}"
                )
            dependency_types[parent_id] = relation_text
        for parent in self.depends_on:
            dependency_types.setdefault(parent, "exec")
        extra_types = sorted(set(dependency_types) - set(self.depends_on))
        if extra_types:
            raise ValueError(
                f"{self.task_id}.dependency_types contains non-dependencies: {extra_types}"
            )
        self.dependency_types = dependency_types

        missing_evidence = sorted(set(self.evidence_dependencies) - set(self.depends_on))
        if missing_evidence:
            raise ValueError(
                f"{self.task_id}.evidence_dependencies must also appear in depends_on: {missing_evidence}"
            )
        for parent in self.evidence_dependencies:
            self.dependency_types[parent] = "evidence"

        if isinstance(self.priority, bool) or not isinstance(self.priority, int) or not 1 <= self.priority <= 10:
            raise ValueError(f"{self.task_id}.priority must be an integer from 1 to 10")
        if self.status not in NODE_STATUSES:
            raise ValueError(f"{self.task_id}.status is invalid")
        if not isinstance(self.version, int) or self.version < 1:
            raise ValueError(f"{self.task_id}.version must be a positive integer")
        if not isinstance(self.rollback_checkpoint, bool):
            raise ValueError(f"{self.task_id}.rollback_checkpoint must be boolean")
        self.evidence = list(_validate_json_value(self.evidence, f"{self.task_id}.evidence"))

    def definition_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "title": self.title,
            "description": self.description,
            "required_skill": self.required_skill,
            "required_skills": list(self.required_skills),
            "agent_role": self.agent_role,
            "node_type": self.node_type,
            "semantic_key": self.semantic_key,
            "delivery_kind": self.delivery_kind,
            "required_permissions": list(self.required_permissions),
            "depends_on": list(self.depends_on),
            "dependency_types": dict(self.dependency_types),
            "evidence_dependencies": list(self.evidence_dependencies),
            "mutex_with": list(self.mutex_with),
            "priority": self.priority,
            "inputs": deepcopy(self.inputs),
            "outputs": deepcopy(self.outputs),
            "hard_constraints": list(self.hard_constraints),
            "soft_preferences": list(self.soft_preferences),
            "acceptance_conditions": list(self.acceptance_conditions),
            "acceptance_predicates": deepcopy(self.acceptance_predicates),
            "budget": deepcopy(self.budget),
            "prohibitions": list(self.prohibitions),
            "resource_requirements": deepcopy(self.resource_requirements),
            "risk": deepcopy(self.risk),
            "estimated_cost": deepcopy(self.estimated_cost),
            "candidate_executors": list(self.candidate_executors),
            "source_refs": deepcopy(self.source_refs),
            "rollback_checkpoint": self.rollback_checkpoint,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.definition_dict(),
            "status": self.status,
            "version": self.version,
            "result": deepcopy(self.result),
            "evidence": deepcopy(self.evidence),
            "fingerprint": self.fingerprint,
            "recompute_reason": self.recompute_reason,
        }


def _unique_text_list(value: Any, field_name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be a JSON array")
    result: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        text = _non_empty_text(item, f"{field_name}[{index}]")
        if text not in seen:
            seen.add(text)
            result.append(text)
    return result
