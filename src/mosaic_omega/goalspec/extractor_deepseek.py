"""DeepSeek 语义抽取器。

满足：一个模型抽取 + 规则程序复合。
DeepSeek 负责理解自然语言并输出 GoalSpec 草稿；normalizer/validator 负责规则复合与校验。
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict

from mosaic_omega.providers import create_openai_compatible_client

try:
    from dotenv import load_dotenv
except ImportError:  # rule 模式仍可运行
    load_dotenv = None  # type: ignore[assignment]


GOALSPEC_EXAMPLE = {
    "main_goal": {
        "goal_text": "生成一个能被 DAG 模块读取的 GoalSpec JSON",
        "goal_type": "coding",
        "domain": "多智能体任务编排"
    },
    "hard_constraints": [
        {
            "constraint": "输出必须是合法 JSON，并且能被 DAG 模块读取",
            "checkable": True,
            "check_method": "schema_check"
        }
    ],
    "soft_preferences": [
        {
            "preference": "字段尽量简洁，方便 Reviewer 后续检查",
            "priority": 4
        }
    ],
    "acceptance_conditions": [
        {
            "condition": "JSON 包含 main_goal、hard_constraints、soft_preferences、acceptance_conditions、budget、prohibitions 六个顶层字段",
            "check_type": "schema_check",
            "expected_result": "六个字段均存在"
        }
    ],
    "budget": {
        "time_limit": "tonight",
        "token_limit": None,
        "compute_limit": "CPU only",
        "cost_limit": None
    },
    "prohibitions": [
        {
            "rule": "不要输出无结构的自然语言解释",
            "reason": "DAG 需要读取结构化 JSON"
        }
    ]
}


SYSTEM_PROMPT = f"""
你是 GoalSpec Compiler 的语义抽取器。

你的任务：把用户自然语言任务转换成严格 JSON。
注意：输出必须是 json，不要输出 Markdown，不要输出解释文字。

你要输出的 JSON 顶层字段固定为：
1. main_goal
2. hard_constraints
3. soft_preferences
4. acceptance_conditions
5. budget
6. prohibitions

字段含义：
- main_goal：用户真正想完成的核心目标。
- hard_constraints：必须满足的硬约束，违反则任务失败。
- soft_preferences：尽量满足的偏好，不满足不一定失败。
- acceptance_conditions：可以用于 Reviewer 或 DAG 检查的验收条件。
- budget：时间、token、算力、成本限制。
- prohibitions：明确禁止做的事情。

抽取原则：
- 不要只看关键词，要理解用户真实意图。
- 不要机械照抄用户原话，要整理成清楚、机器可读的规则。
- 如果用户说“别写成荣耀项目”，应理解为禁止事项或硬约束。
- 如果用户说“语言正式一点”，应理解为软偏好。
- 如果用户说“今晚、半小时内、几个小时内”，应理解为时间预算。
- 如果用户说“没有独显、不用服务器、CPU only”，应理解为算力限制。
- 如果用户说“别说已经上车部署”，应理解为禁止虚构项目进度。
- 如果某类信息没有出现，使用空数组或 null，不要乱编。
- acceptance_conditions 要尽量可检查。

JSON 输出示例：
{json.dumps(GOALSPEC_EXAMPLE, ensure_ascii=False, indent=2)}
"""


def _extract_content(response: Any) -> str:
    content = response.choices[0].message.content
    if content is None:
        return ""
    return content.strip()


def _strip_code_fence(content: str) -> str:
    text = content.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def extract_with_deepseek(user_text: str) -> Dict[str, Any]:
    """使用 DeepSeek 进行 GoalSpec 语义抽取。"""
    if not user_text or not user_text.strip():
        raise ValueError("user_text cannot be empty")

    if load_dotenv is not None:
        load_dotenv()

    api_key = os.getenv("MOSAIC_API_KEY") or os.getenv("OPENAI_API_KEY") or os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError("没有检测到当前模型服务所需的 API Key。请在 MOSAIC 设置页配置。")

    provider_id = os.getenv("MOSAIC_PROVIDER", "deepseek").strip() or "deepseek"
    model = os.getenv("LLM_MODEL_NAME") or os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")
    base_url = (os.getenv("OPENAI_BASE_URL") or os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")).rstrip("/")
    try:
        timeout_s = float(os.getenv("DEEPSEEK_TIMEOUT_S", "60"))
    except ValueError as exc:
        raise ValueError("DEEPSEEK_TIMEOUT_S must be a number") from exc
    if timeout_s <= 0:
        raise ValueError("DEEPSEEK_TIMEOUT_S must be positive")

    client = create_openai_compatible_client(
        api_key=api_key,
        base_url=base_url,
        timeout=timeout_s,
        max_retries=2,
    )

    last_error: Any = None
    for _ in range(3):
        try:
            request_kwargs = {
                "model": model,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_text},
                ],
                "response_format": {"type": "json_object"},
                "max_tokens": 2500,
                "temperature": 0.1,
            }
            if provider_id == "deepseek":
                request_kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
            response = client.chat.completions.create(**request_kwargs)
            content = _strip_code_fence(_extract_content(response))
            if not content:
                last_error = "DeepSeek 返回了空内容"
                continue
            return json.loads(content)
        except Exception as e:
            last_error = e

    raise RuntimeError(f"模型服务 GoalSpec 抽取失败：{last_error}")
