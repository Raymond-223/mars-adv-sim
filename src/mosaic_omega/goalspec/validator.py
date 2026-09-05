"""GoalSpec 轻量校验器。"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple
import json
from importlib.resources import files

try:
    from jsonschema import Draft202012Validator  # type: ignore
except Exception:  # Offline desktop runtime intentionally has no mandatory third-party deps.
    Draft202012Validator = None  # type: ignore[assignment]

REQUIRED_TOP_FIELDS = [
    "main_goal",
    "hard_constraints",
    "soft_preferences",
    "acceptance_conditions",
    "budget",
    "prohibitions",
]
ALLOWED_GOAL_TYPES = {
    "research", "coding", "planning", "robot_task", "analysis", "report_generation", "other"
}
ALLOWED_CHECK_TYPES = {
    "manual_review", "schema_check", "file_check", "content_check", "unit_test", "simulation_check", "metric_check"
}


def validate_goalspec(spec: Dict[str, Any]) -> Tuple[bool, List[str]]:
    errors: List[str] = []

    if not isinstance(spec, dict):
        return False, ["GoalSpec must be a dict/object"]

    unexpected = sorted(set(spec) - set(REQUIRED_TOP_FIELDS))
    if unexpected:
        errors.append(f"unexpected top-level fields: {unexpected}")

    for field in REQUIRED_TOP_FIELDS:
        if field not in spec:
            errors.append(f"missing top-level field: {field}")

    mg = spec.get("main_goal")
    if not isinstance(mg, dict):
        errors.append("main_goal must be an object")
    else:
        for k in ["goal_text", "goal_type", "domain"]:
            if k not in mg:
                errors.append(f"main_goal missing field: {k}")
        if mg.get("goal_type") not in ALLOWED_GOAL_TYPES:
            errors.append(f"invalid goal_type: {mg.get('goal_type')}")
        if not isinstance(mg.get("goal_text"), str) or not mg.get("goal_text", "").strip():
            errors.append("main_goal.goal_text must be a non-empty string")

    if not isinstance(spec.get("hard_constraints"), list):
        errors.append("hard_constraints must be a list")
    else:
        for i, item in enumerate(spec["hard_constraints"]):
            if not isinstance(item, dict):
                errors.append(f"hard_constraints[{i}] must be an object")
                continue
            for k in ["constraint", "checkable", "check_method"]:
                if k not in item:
                    errors.append(f"hard_constraints[{i}] missing field: {k}")
            if "checkable" in item and not isinstance(item["checkable"], bool):
                errors.append(f"hard_constraints[{i}].checkable must be bool")

    if not isinstance(spec.get("soft_preferences"), list):
        errors.append("soft_preferences must be a list")
    else:
        for i, item in enumerate(spec["soft_preferences"]):
            if not isinstance(item, dict):
                errors.append(f"soft_preferences[{i}] must be an object")
                continue
            for k in ["preference", "priority"]:
                if k not in item:
                    errors.append(f"soft_preferences[{i}] missing field: {k}")
            if "priority" in item and not (isinstance(item["priority"], int) and 1 <= item["priority"] <= 5):
                errors.append(f"soft_preferences[{i}].priority must be int in [1, 5]")

    if not isinstance(spec.get("acceptance_conditions"), list):
        errors.append("acceptance_conditions must be a list")
    else:
        for i, item in enumerate(spec["acceptance_conditions"]):
            if not isinstance(item, dict):
                errors.append(f"acceptance_conditions[{i}] must be an object")
                continue
            for k in ["condition", "check_type", "expected_result"]:
                if k not in item:
                    errors.append(f"acceptance_conditions[{i}] missing field: {k}")
            if item.get("check_type") not in ALLOWED_CHECK_TYPES:
                errors.append(f"acceptance_conditions[{i}] invalid check_type: {item.get('check_type')}")

    budget = spec.get("budget")
    if not isinstance(budget, dict):
        errors.append("budget must be an object")
    else:
        for k in ["time_limit", "token_limit", "compute_limit", "cost_limit"]:
            if k not in budget:
                errors.append(f"budget missing field: {k}")
        if budget.get("token_limit") is not None and not isinstance(budget.get("token_limit"), int):
            errors.append("budget.token_limit must be int or null")

    if not isinstance(spec.get("prohibitions"), list):
        errors.append("prohibitions must be a list")
    else:
        for i, item in enumerate(spec["prohibitions"]):
            if not isinstance(item, dict):
                errors.append(f"prohibitions[{i}] must be an object")
                continue
            for k in ["rule", "reason"]:
                if k not in item:
                    errors.append(f"prohibitions[{i}] missing field: {k}")

    return len(errors) == 0, errors


def validate_goalspec_schema(spec: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """Validate GoalSpec against the packaged JSON Schema.

    The six top-level keys are a frozen cross-module contract. Nested objects
    intentionally remain extensible so richer source/confidence/predicate
    metadata can evolve without breaking ToDAG or runtime adapters.
    """
    # Prefer the reference Draft 2020-12 validator when available, but never make
    # desktop startup depend on PyPI.  The bundled schema is a frozen GoalSpec
    # contract, so the stdlib fallback below validates the same constraints used
    # by that schema.
    if Draft202012Validator is not None:
        schema_text = files("mosaic_omega.schemas").joinpath("goalspec.schema.json").read_text(encoding="utf-8")
        schema = json.loads(schema_text)
        validator = Draft202012Validator(schema)
        errors = sorted(validator.iter_errors(spec), key=lambda e: list(e.absolute_path))
        messages = []
        for error in errors:
            path = ".".join(str(item) for item in error.absolute_path) or "$"
            messages.append(f"{path}: {error.message}")
        return not messages, messages

    return _validate_goalspec_schema_stdlib(spec)


def _validate_goalspec_schema_stdlib(spec: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """Dependency-free validator for the frozen bundled GoalSpec schema."""
    errors: List[str] = []

    def err(path: str, message: str) -> None:
        errors.append(f"{path}: {message}")

    def require(obj: Any, keys: List[str], path: str) -> bool:
        if not isinstance(obj, dict):
            err(path, "must be an object")
            return False
        for key in keys:
            if key not in obj:
                err(path, f"'{key}' is a required property")
        return True

    if not isinstance(spec, dict):
        return False, ["$: must be an object"]

    allowed = set(REQUIRED_TOP_FIELDS)
    for key in sorted(set(spec) - allowed):
        err("$", f"Additional properties are not allowed ('{key}' was unexpected)")
    for key in REQUIRED_TOP_FIELDS:
        if key not in spec:
            err("$", f"'{key}' is a required property")

    mg = spec.get("main_goal")
    if require(mg, ["goal_text", "goal_type", "domain", "sub_goals", "confidence"], "main_goal"):
        if "goal_text" in mg and (not isinstance(mg["goal_text"], str) or len(mg["goal_text"]) < 1):
            err("main_goal.goal_text", "must be a non-empty string")
        if "goal_type" in mg and (not isinstance(mg["goal_type"], str) or mg["goal_type"] not in ALLOWED_GOAL_TYPES):
            err("main_goal.goal_type", f"must be one of {sorted(ALLOWED_GOAL_TYPES)}")
        if "domain" in mg and not isinstance(mg["domain"], str):
            err("main_goal.domain", "must be a string")
        if "sub_goals" in mg and not isinstance(mg["sub_goals"], list):
            err("main_goal.sub_goals", "must be an array")
        if "confidence" in mg and (isinstance(mg["confidence"], bool) or not isinstance(mg["confidence"], (int, float)) or not 0 <= mg["confidence"] <= 1):
            err("main_goal.confidence", "must be a number in [0, 1]")

    def validate_array(name: str, required: List[str], callback) -> None:
        value = spec.get(name)
        if not isinstance(value, list):
            err(name, "must be an array")
            return
        for i, item in enumerate(value):
            path = f"{name}.{i}"
            if require(item, required, path):
                callback(item, path)

    def hard(item: Dict[str, Any], path: str) -> None:
        if "constraint" in item and (not isinstance(item["constraint"], str) or not item["constraint"]): err(path+".constraint", "must be a non-empty string")
        for k in ("type", "check_method", "predicate", "source_span"):
            if k in item and not isinstance(item[k], str): err(path+"."+k, "must be a string")
        if "checkable" in item and not isinstance(item["checkable"], bool): err(path+".checkable", "must be a boolean")
        if "confidence" in item and (isinstance(item["confidence"], bool) or not isinstance(item["confidence"], (int, float)) or not 0 <= item["confidence"] <= 1): err(path+".confidence", "must be a number in [0, 1]")

    def soft(item: Dict[str, Any], path: str) -> None:
        if "preference" in item and (not isinstance(item["preference"], str) or not item["preference"]): err(path+".preference", "must be a non-empty string")
        if "priority" in item and (isinstance(item["priority"], bool) or not isinstance(item["priority"], int) or not 1 <= item["priority"] <= 5): err(path+".priority", "must be an integer in [1, 5]")
        if "weight" in item and (isinstance(item["weight"], bool) or not isinstance(item["weight"], (int, float))): err(path+".weight", "must be a number")

    def accept(item: Dict[str, Any], path: str) -> None:
        if "condition" in item and (not isinstance(item["condition"], str) or not item["condition"]): err(path+".condition", "must be a non-empty string")
        for k in ("predicate", "expected_result"):
            if k in item and not isinstance(item[k], str): err(path+"."+k, "must be a string")
        if "check_type" in item and (not isinstance(item["check_type"], str) or item["check_type"] not in ALLOWED_CHECK_TYPES): err(path+".check_type", f"must be one of {sorted(ALLOWED_CHECK_TYPES)}")
        if "confidence" in item and (isinstance(item["confidence"], bool) or not isinstance(item["confidence"], (int, float)) or not 0 <= item["confidence"] <= 1): err(path+".confidence", "must be a number in [0, 1]")

    def prohibit(item: Dict[str, Any], path: str) -> None:
        if "rule" in item and (not isinstance(item["rule"], str) or not item["rule"]): err(path+".rule", "must be a non-empty string")
        for k in ("type", "reason"):
            if k in item and not isinstance(item[k], str): err(path+"."+k, "must be a string")
        if "confidence" in item and (isinstance(item["confidence"], bool) or not isinstance(item["confidence"], (int, float)) or not 0 <= item["confidence"] <= 1): err(path+".confidence", "must be a number in [0, 1]")

    validate_array("hard_constraints", ["constraint", "type", "checkable", "check_method", "predicate", "source_span", "confidence"], hard)
    validate_array("soft_preferences", ["preference", "priority", "weight"], soft)
    validate_array("acceptance_conditions", ["condition", "predicate", "check_type", "expected_result", "confidence"], accept)
    validate_array("prohibitions", ["rule", "type", "reason", "confidence"], prohibit)

    budget = spec.get("budget")
    if require(budget, ["time_limit", "token_limit", "compute_limit", "cost_limit"], "budget"):
        for k in ("time_limit", "compute_limit", "cost_limit"):
            if k in budget and budget[k] is not None and not isinstance(budget[k], str): err("budget."+k, "must be a string or null")
        if "token_limit" in budget:
            value = budget["token_limit"]
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0): err("budget.token_limit", "must be a non-negative integer or null")

    return not errors, errors
