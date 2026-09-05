"""GoalSpec Compiler 总入口。"""
from __future__ import annotations

from typing import Any, Dict

from .extractor import extract
from .normalizer import normalize
from .validator import validate_goalspec, validate_goalspec_schema


def compile_goal(user_text: str, *, strict: bool = True, mode: str = "rule") -> Dict[str, Any]:
    """把用户自然语言编译成标准 GoalSpec JSON。

    mode:
    - rule: 本地规则抽取，不需要 API
    - deepseek: DeepSeek 模型语义抽取 + 规则复合校验
    - auto: 优先 DeepSeek；失败后回退到 rule
    """
    if not user_text or not user_text.strip():
        raise ValueError("user_text cannot be empty")

    if mode == "deepseek":
        from .extractor_deepseek import extract_with_deepseek
        draft = extract_with_deepseek(user_text)
    elif mode == "auto":
        try:
            from .extractor_deepseek import extract_with_deepseek
            draft = extract_with_deepseek(user_text)
        except Exception as e:
            print(f"[WARN] DeepSeek 抽取失败，自动回退到规则抽取。原因：{e}")
            draft = extract(user_text)
    else:
        draft = extract(user_text)

    spec = normalize(draft)
    valid, errors = validate_goalspec(spec)
    schema_valid, schema_errors = validate_goalspec_schema(spec)
    all_errors = [*errors, *schema_errors]

    if strict and (not valid or not schema_valid):
        raise ValueError("GoalSpec validation failed: " + "; ".join(all_errors))

    return spec


def compile_goal_debug(user_text: str, *, mode: str = "rule") -> Dict[str, Any]:
    if not user_text or not user_text.strip():
        raise ValueError("user_text cannot be empty")

    used_mode = mode
    if mode == "deepseek":
        from .extractor_deepseek import extract_with_deepseek
        draft = extract_with_deepseek(user_text)
    elif mode == "auto":
        try:
            from .extractor_deepseek import extract_with_deepseek
            draft = extract_with_deepseek(user_text)
            used_mode = "deepseek"
        except Exception as e:
            print(f"[WARN] DeepSeek 抽取失败，自动回退到规则抽取。原因：{e}")
            draft = extract(user_text)
            used_mode = "rule_fallback"
    else:
        draft = extract(user_text)

    spec = normalize(draft)
    valid, errors = validate_goalspec(spec)
    schema_valid, schema_errors = validate_goalspec_schema(spec)
    return {
        "mode": used_mode,
        "draft": draft,
        "goalspec": spec,
        "validation": {
            "valid": valid and schema_valid,
            "errors": [*errors, *schema_errors],
        },
    }
