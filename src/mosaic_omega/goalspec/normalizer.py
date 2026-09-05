"""GoalSpec 规范化与轻量编译层。
保持顶层字段兼容，同时增强约束追踪、置信度和可执行验收信息。
"""
from __future__ import annotations

from typing import Any, Dict, List

from .constraint_compiler import classify_constraint, predicate_for

ALLOWED_GOAL_TYPES = {"research", "coding", "planning", "robot_task", "analysis", "report_generation", "other"}
ALLOWED_CHECK_TYPES = {"manual_review", "schema_check", "file_check", "content_check", "unit_test", "simulation_check", "metric_check"}


def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _infer_check_method(text: str) -> str:
    lower = text.lower()
    if "json" in lower or "schema" in lower or "字段" in text:
        return "schema_check"
    if any(x in text for x in ["文件", "输出", "生成", "保存"]):
        return "file_check"
    if any(x in text for x in ["测试", "运行", "编译", "build"]):
        return "unit_test"
    if any(x in text for x in ["准确率", "比例", "指标", "性能"]):
        return "metric_check"
    return "content_check"


def _confidence(item: Dict[str, Any]) -> float:
    try:
        return max(0.0, min(1.0, float(item.get("confidence", 0.9))))
    except Exception:
        return 0.9


def normalize_main_goal(value: Any) -> Dict[str, Any]:
    if isinstance(value, str):
        value = {"goal_text": value}
    value = value if isinstance(value, dict) else {}
    goal_type = value.get("goal_type", "other")
    if goal_type not in ALLOWED_GOAL_TYPES:
        goal_type = "other"
    return {
        "goal_text": str(value.get("goal_text") or value.get("text") or "未明确的用户任务").strip(),
        "goal_type": goal_type,
        "domain": str(value.get("domain") or "通用任务编排"),
        "sub_goals": value.get("sub_goals", []),
        "confidence": _confidence(value),
    }


def normalize_hard_constraints(value: Any) -> List[Dict[str, Any]]:
    result = []
    for idx, item in enumerate(_as_list(value)):
        item = item if isinstance(item, dict) else {"constraint": str(item)}
        text = str(item.get("constraint") or item.get("text") or "").strip()
        if not text:
            continue
        result.append({
            "constraint": text,
            "type": item.get("type") or classify_constraint(text),
            "checkable": bool(item.get("checkable", True)),
            "check_method": item.get("check_method") or _infer_check_method(text),
            "predicate": predicate_for(text),
            "source_span": item.get("source_span", ""),
            "confidence": _confidence(item),
        })
    return result


def normalize_soft_preferences(value: Any) -> List[Dict[str, Any]]:
    result = []
    for item in _as_list(value):
        item = item if isinstance(item, dict) else {"preference": str(item)}
        text = str(item.get("preference") or item.get("text") or "").strip()
        if text:
            result.append({
                "preference": text,
                "priority": max(1, min(5, int(item.get("priority", 3)))),
                "weight": float(item.get("weight", 0.5)),
            })
    return result


def normalize_acceptance_conditions(value: Any, hard_constraints: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    result = []
    for item in _as_list(value):
        item = item if isinstance(item, dict) else {"condition": str(item)}
        text = str(item.get("condition") or item.get("text") or "").strip()
        if not text:
            continue
        check_type = item.get("check_type") or _infer_check_method(text)
        if check_type not in ALLOWED_CHECK_TYPES:
            check_type = "manual_review"
        result.append({
            "condition": text,
            "predicate": item.get("predicate") or check_type,
            "check_type": check_type,
            "expected_result": item.get("expected_result", "条件被满足"),
            "confidence": _confidence(item),
        })

    if not result:
        for hc in hard_constraints:
            result.append({
                "condition": hc["constraint"],
                "predicate": hc.get("check_method", "content_check"),
                "check_type": hc.get("check_method", "content_check"),
                "expected_result": "硬约束被满足",
                "confidence": hc.get("confidence", 0.9),
            })
    return result


def normalize_budget(value: Any) -> Dict[str, Any]:
    value = value if isinstance(value, dict) else {}
    return {
        "time_limit": value.get("time_limit"),
        "token_limit": int(value.get("token_limit")) if str(value.get("token_limit")).isdigit() else None,
        "compute_limit": value.get("compute_limit"),
        "cost_limit": value.get("cost_limit"),
    }


def normalize_prohibitions(value: Any) -> List[Dict[str, Any]]:
    result = []
    for item in _as_list(value):
        item = item if isinstance(item, dict) else {"rule": str(item)}
        rule = str(item.get("rule") or item.get("text") or "").strip()
        if rule:
            result.append({
                "rule": rule,
                "type": item.get("type", "operation"),
                "reason": item.get("reason", "用户明确禁止该行为"),
                "confidence": _confidence(item),
            })
    return result


def normalize(draft: Dict[str, Any]) -> Dict[str, Any]:
    hard = normalize_hard_constraints(draft.get("hard_constraints"))
    return {
        "main_goal": normalize_main_goal(draft.get("main_goal")),
        "hard_constraints": hard,
        "soft_preferences": normalize_soft_preferences(draft.get("soft_preferences")),
        "acceptance_conditions": normalize_acceptance_conditions(draft.get("acceptance_conditions"), hard),
        "budget": normalize_budget(draft.get("budget")),
        "prohibitions": normalize_prohibitions(draft.get("prohibitions")),
    }
