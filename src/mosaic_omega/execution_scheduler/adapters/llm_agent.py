"""Strict OpenAI-compatible LLM Agent adapter.

No silent fallback is permitted by default.  An explicitly enabled deterministic
fallback exists only for developer fixtures and marks every generated ToolCall
with fallback provenance, so it cannot be mistaken for a real API execution.
"""
from __future__ import annotations

import json
import os
from urllib.parse import urlsplit
from typing import Any

from ..models import Assignment, Evidence, ExecutionResult, TaskNodeView, ToolCall

from mosaic_omega.providers import create_openai_compatible_client

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


class LLMAgentAdapter:
    total_api_tokens: int = 0
    total_api_calls: int = 0

    def __init__(
        self,
        actor_id: str,
        api_key: str | None = None,
        base_url: str | None = None,
        model_name: str | None = None,
        temperature: float = 0.1,
        *,
        allow_fallback: bool = False,
    ) -> None:
        self.actor_id = actor_id
        self.provider_id = (os.environ.get("MOSAIC_PROVIDER") or "deepseek").strip() or "deepseek"
        self.api_key = api_key or os.environ.get("MOSAIC_API_KEY") or os.environ.get("OPENAI_API_KEY") or os.environ.get("DEEPSEEK_API_KEY")
        self.base_url = base_url or os.environ.get("OPENAI_BASE_URL") or os.environ.get("DEEPSEEK_BASE_URL") or "https://api.deepseek.com/v1"
        self.model_name = model_name or os.environ.get("LLM_MODEL_NAME") or os.environ.get("DEEPSEEK_MODEL") or "deepseek-v4-flash"
        self.temperature = temperature
        self.allow_fallback = bool(allow_fallback)
        self._client: Any | None = None
        if self.api_key and "your-actual-api-key" not in self.api_key:
            self._client = create_openai_compatible_client(
                api_key=self.api_key,
                base_url=self.base_url,
                timeout=float(os.environ.get("DEEPSEEK_TIMEOUT_S", "60")),
                max_retries=2,
            )

    @property
    def api_transport(self) -> str:
        if self._client is None:
            return "unbound"
        return str(getattr(self._client, "_mosaic_transport", "unknown"))

    @property
    def endpoint_host(self) -> str:
        return (urlsplit(self.base_url).hostname or "").casefold()

    @property
    def official_endpoint_verified(self) -> bool:
        # Competition-strict evidence is intentionally provider-aware: merely
        # pointing an OpenAI-compatible custom provider at api.deepseek.com must
        # not be enough to inherit the DeepSeek strict claim.
        return (
            self.provider_id == "deepseek"
            and self.endpoint_host == "api.deepseek.com"
            and self.api_transport in {"openai_sdk", "stdlib_http"}
        )

    @property
    def authenticity_mode(self) -> str:
        if self.allow_fallback:
            return "api_with_explicit_fallback"
        if self.api_transport not in {"openai_sdk", "stdlib_http"}:
            return "test_fixture" if self._client is not None else "unbound"
        if self.provider_id != "deepseek":
            return "real_api_unverified_provider"
        if not self.official_endpoint_verified:
            return "api_test_endpoint"
        return "real_api"

    def set_model(self, model_id: str) -> None:
        value = str(model_id).strip()
        if not value:
            raise ValueError("model_id must not be empty")
        self.model_name = value

    def plan(
        self,
        task: TaskNodeView,
        assignment: Assignment,
        trace_id: str,
        context_pack: dict[str, Any] | None = None,
    ) -> list[ToolCall]:
        if self._client is None:
            if self.allow_fallback:
                return self._fallback_plan(task, assignment, trace_id, reason="api_client_unavailable")
            raise RuntimeError(
                "LLM Agent 没有可用的真实 API client；严格模式禁止自动降级为确定性假 Agent。"
            )
        try:
            return self._plan_with_llm(task, assignment, trace_id, context_pack)
        except Exception as exc:
            if self.allow_fallback:
                return self._fallback_plan(
                    task, assignment, trace_id, reason=f"api_error:{type(exc).__name__}"
                )
            raise RuntimeError(f"LLM API 调用失败，严格模式不降级：{type(exc).__name__}: {exc}") from exc

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

    def _plan_with_llm(
        self,
        task: TaskNodeView,
        assignment: Assignment,
        trace_id: str,
        context_pack: dict[str, Any] | None,
    ) -> list[ToolCall]:
        system_prompt = (
            "You are an autonomous AI Agent executing long-horizon tasks in MOSAIC-Ω.\n"
            "Analyze the task description, acceptance conditions, and context.\n"
            "Respond ONLY with a JSON object: {\"tool_name\": str, \"arguments\": dict}.\n"
            "Do not claim an external action occurred unless the tool runtime actually executes it."
        )
        user_content = {
            "task_id": task.task_id,
            "description": task.description,
            "required_tool": assignment.tool_id,
            "acceptance_conditions": list(task.acceptance_conditions),
            "suggested_tool_spec": task.metadata.get("tool", {}) if isinstance(task.metadata, dict) else {},
            "context_pack": context_pack or {},
        }
        response = self._client.chat.completions.create(
            model=self.model_name,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(user_content, ensure_ascii=False)},
            ],
            temperature=self.temperature,
        )
        LLMAgentAdapter.total_api_calls += 1
        usage = self._usage(response)
        total_tokens = usage.get("total_tokens")
        if isinstance(total_tokens, (int, float)):
            LLMAgentAdapter.total_api_tokens += int(total_tokens)

        content = response.choices[0].message.content or "{}"
        if content.startswith("```json"):
            content = content.replace("```json", "", 1).rsplit("```", 1)[0].strip()
        elif content.startswith("```"):
            content = content.replace("```", "", 1).rsplit("```", 1)[0].strip()
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            raise RuntimeError("LLM 返回内容不是合法 JSON；严格模式不伪造 ToolCall") from exc

        tool_name = str(parsed.get("tool_name") or assignment.tool_id)
        arguments = dict(parsed.get("arguments", {}))
        suggested = task.metadata.get("tool", {}) if isinstance(task.metadata, dict) else {}
        # Tool metadata may complete an incomplete model plan; this is deterministic input
        # completion, not a model-call fallback.  It remains auditable in event payloads.
        if (not arguments or (tool_name == "shell" and "command" not in arguments)) and isinstance(suggested, dict):
            for k, v in dict(suggested.get("arguments", {})).items():
                arguments.setdefault(k, v)
        arguments["api_provenance"] = {
            "provider": self.provider_id,
            "model": getattr(response, "model", None) or self.model_name,
            "request_id": getattr(response, "id", None),
            "usage": usage,
            "base_url": self.base_url,
            "transport": self.api_transport,
            "endpoint_host": self.endpoint_host,
            "official_endpoint_verified": self.official_endpoint_verified,
        }

        return [ToolCall(
            run_id=task.run_id,
            task_id=task.task_id,
            actor_id=self.actor_id,
            tool_name=tool_name,
            arguments=arguments,
            idempotency_key=f"{task.run_id}:{task.task_id}:{max(1, task.attempt)}:{tool_name}",
            required_permissions=task.required_permissions,
            trace_id=trace_id,
            model_id=assignment.model_id or self.model_name,
            schema_version=assignment.schema_version,
        )]

    def _fallback_plan(
        self,
        task: TaskNodeView,
        assignment: Assignment,
        trace_id: str,
        *,
        reason: str,
    ) -> list[ToolCall]:
        spec = task.metadata.get("tool", {}) if isinstance(task.metadata, dict) else {}
        tool_name = str(spec.get("name") or assignment.tool_id)
        arguments = dict(spec.get("arguments", {}))
        if not arguments:
            arguments = {
                "description": task.description,
                "acceptance_conditions": list(task.acceptance_conditions),
            }
        arguments["execution_provenance"] = {
            "mode": "explicit_deterministic_fallback",
            "reason": reason,
        }
        return [ToolCall(
            run_id=task.run_id,
            task_id=task.task_id,
            actor_id=self.actor_id,
            tool_name=tool_name,
            arguments=arguments,
            idempotency_key=f"{task.run_id}:{task.task_id}:{max(1, task.attempt)}:{tool_name}",
            required_permissions=task.required_permissions,
            trace_id=trace_id,
            model_id=assignment.model_id,
            schema_version=assignment.schema_version,
        )]

    def verify(
        self,
        task: TaskNodeView,
        result: ExecutionResult,
        evidence: tuple[Evidence, ...],
    ) -> tuple[bool, dict[str, Any]]:
        checks = [{"condition": "execution_success", "passed": result.success}]
        for condition in task.acceptance_conditions:
            checks.append({
                "condition": condition,
                "passed": condition.casefold() in (result.output or "").casefold(),
            })
        passed = bool(evidence) and all(item["passed"] for item in checks)
        return passed, {
            "passed": passed,
            "checks": checks,
            "evidence_ids": [item.evidence_id for item in evidence],
            "verifier_actor": self.actor_id,
        }
