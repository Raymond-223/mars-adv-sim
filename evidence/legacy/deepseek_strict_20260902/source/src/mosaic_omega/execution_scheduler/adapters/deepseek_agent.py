"""DeepSeek-backed planning adapter for the authoritative execution chain.

The model produces task content, but it does not execute tools or declare a task
successful.  The returned :class:`ToolCall` still passes through ToolRuntime,
Evidence generation, and the deterministic Verifier.
"""
from __future__ import annotations

import json
import os
from collections.abc import Callable
from typing import Any

from ..models import Assignment, TaskNodeView, ToolCall

try:
    from openai import OpenAI
except ImportError:  # keep non-DeepSeek modes importable
    OpenAI = None  # type: ignore[assignment]

try:
    from dotenv import load_dotenv
except ImportError:  # keep non-DeepSeek modes importable
    load_dotenv = None  # type: ignore[assignment]


ContextProvider = Callable[[TaskNodeView, Assignment, str], dict[str, Any]]


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    try:
        return float(raw) if raw is not None else default
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    try:
        return int(raw) if raw is not None else default
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


class DeepSeekAgent:
    """Use DeepSeek for one role-specific task while preserving local controls."""

    def __init__(
        self,
        actor_id: str,
        *,
        role: str,
        context_provider: ContextProvider | None = None,
        client: Any | None = None,
    ) -> None:
        if load_dotenv is not None:
            load_dotenv()
        self.actor_id = actor_id
        self.role = role
        self.model = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash").strip() or "deepseek-v4-flash"
        self.base_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
        self.max_tokens = _env_int("DEEPSEEK_AGENT_MAX_TOKENS", 1200)
        self.temperature = _env_float("DEEPSEEK_AGENT_TEMPERATURE", 0.1)
        self.prompt_char_limit = _env_int("DEEPSEEK_PROMPT_CHAR_LIMIT", 30000)
        self.context_provider = context_provider

        if self.max_tokens <= 0:
            raise ValueError("DEEPSEEK_AGENT_MAX_TOKENS must be positive")
        if self.prompt_char_limit < 2000:
            raise ValueError("DEEPSEEK_PROMPT_CHAR_LIMIT must be at least 2000")

        if client is not None:
            self.client = client
            return
        if OpenAI is None:
            raise RuntimeError(
                '缺少 openai Python SDK，请运行：py -m pip install -e ".[deepseek]"'
            )
        if load_dotenv is None:
            raise RuntimeError('缺少 python-dotenv，请运行：py -m pip install -e ".[deepseek]"')
        api_key = os.getenv("DEEPSEEK_API_KEY")
        if not api_key:
            raise RuntimeError(
                "没有检测到 DEEPSEEK_API_KEY。请在当前 PowerShell 会话中设置后再运行。"
            )
        timeout_s = _env_float("DEEPSEEK_TIMEOUT_S", 60.0)
        if timeout_s <= 0:
            raise ValueError("DEEPSEEK_TIMEOUT_S must be positive")
        self.client = OpenAI(
            api_key=api_key,
            base_url=self.base_url,
            timeout=timeout_s,
            max_retries=2,
        )

    def _system_prompt(self) -> str:
        return (
            "你是 MOSAIC-Ω 多智能体系统中的异构执行 Agent。"
            f"你的角色是 {self.role}。\n"
            "只完成当前任务节点，不改写总目标，不泄露密钥，不声称完成未实际发生的外部操作。"
            "必须利用给定约束、上游结果和低熵消息，输出可供后续节点直接使用的具体成果。"
            "结论应简洁、可验证；如果证据不足，明确说明依据和边界。"
        )

    def _user_prompt(
        self,
        task: TaskNodeView,
        assignment: Assignment,
        context: dict[str, Any],
    ) -> str:
        payload = {
            "task": {
                "task_id": task.task_id,
                "task_type": task.task_type,
                "description": task.description,
                "required_capabilities": sorted(task.required_capabilities),
                "acceptance_conditions": list(task.acceptance_conditions),
                "inputs": task.inputs,
                "outputs": task.outputs,
                "risk": task.risk,
            },
            "assignment": {
                "agent_id": assignment.agent_id,
                "model_id": assignment.model_id,
                "tool_id": assignment.tool_id,
            },
            "context_pack": context.get("context_pack", {}),
            "low_entropy_messages": context.get("messages", []),
        }
        document = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
        if len(document) > self.prompt_char_limit:
            document = document[: self.prompt_char_limit] + "\n[上下文因预算限制已截断]"
        return "请执行以下任务节点，并直接给出本节点成果：\n" + document

    @staticmethod
    def _usage(response: Any) -> dict[str, Any]:
        usage = getattr(response, "usage", None)
        if usage is None:
            return {}
        if hasattr(usage, "model_dump"):
            return dict(usage.model_dump())
        if isinstance(usage, dict):
            return dict(usage)
        return {
            key: getattr(usage, key)
            for key in ("prompt_tokens", "completion_tokens", "total_tokens")
            if getattr(usage, key, None) is not None
        }

    def plan(self, task: TaskNodeView, assignment: Assignment, trace_id: str) -> list[ToolCall]:
        context = (
            self.context_provider(task, assignment, trace_id)
            if self.context_provider is not None
            else {}
        )
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": self._system_prompt()},
                    {"role": "user", "content": self._user_prompt(task, assignment, context)},
                ],
                max_tokens=self.max_tokens,
                temperature=self.temperature,
                extra_body={"thinking": {"type": "disabled"}},
            )
        except Exception as exc:
            raise RuntimeError(f"DeepSeek Agent 调用失败：{type(exc).__name__}: {exc}") from exc

        content = response.choices[0].message.content
        output = content.strip() if content else ""
        if not output:
            raise RuntimeError("DeepSeek Agent 返回空内容")

        provenance = {
            "provider": "deepseek",
            "model": getattr(response, "model", None) or self.model,
            "request_id": getattr(response, "id", None),
            "usage": self._usage(response),
            "base_url": self.base_url,
            "role": self.role,
        }
        return [
            ToolCall(
                run_id=task.run_id,
                task_id=task.task_id,
                actor_id=self.actor_id,
                tool_name=assignment.tool_id,
                arguments={
                    "description": output,
                    "acceptance_conditions": list(task.acceptance_conditions),
                    "api_provenance": provenance,
                },
                idempotency_key=(
                    f"{task.run_id}:{task.task_id}:{max(1, task.attempt)}:{assignment.tool_id}"
                ),
                required_permissions=task.required_permissions,
                trace_id=trace_id,
                model_id=assignment.model_id,
                schema_version=assignment.schema_version,
            )
        ]
