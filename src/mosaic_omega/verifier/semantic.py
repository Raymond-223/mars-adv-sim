"""Independent provider-backed semantic acceptance judge.

This component is used only for acceptance conditions that cannot be reduced to
the deterministic verifier DSL. It is deliberately separate from the task Agent
call and records provider/request provenance. A model judgment is never presented
as a deterministic check in observability.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from mosaic_omega.providers import create_openai_compatible_client


class ProviderSemanticJudge:
    def __init__(self, workspace: str | Path) -> None:
        self.workspace = Path(workspace).resolve()
        self.provider_id = os.getenv("MOSAIC_PROVIDER", "deepseek").strip() or "deepseek"
        self.model = (os.getenv("LLM_MODEL_NAME") or os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")).strip()
        self.base_url = (os.getenv("OPENAI_BASE_URL") or os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")).rstrip("/")
        self.api_key = (os.getenv("MOSAIC_API_KEY") or os.getenv("OPENAI_API_KEY") or os.getenv("DEEPSEEK_API_KEY") or "").strip()
        self.available = bool(self.model and self.base_url and self.api_key)
        self._client: Any | None = None

    def _get_client(self):
        if not self.available:
            raise RuntimeError("semantic verifier provider unavailable")
        if self._client is None:
            self._client = create_openai_compatible_client(
                api_key=self.api_key,
                base_url=self.base_url,
                timeout=float(os.getenv("DEEPSEEK_TIMEOUT_S", "60")),
                max_retries=1,
            )
        return self._client

    def _deliverable_text(self, result: Any) -> str:
        metadata = dict(getattr(result, "metadata", {}) or {})
        relative = metadata.get("deliverable_relative")
        if relative:
            path = (self.workspace / str(relative).replace("\\", "/")).resolve()
            if path == self.workspace or self.workspace in path.parents:
                if path.is_file() and path.stat().st_size <= 1_000_000:
                    return path.read_text(encoding="utf-8", errors="replace")
        return str(getattr(result, "output", "") or "")

    @staticmethod
    def _usage(response: Any) -> dict[str, Any]:
        usage = getattr(response, "usage", None)
        if usage is None:
            return {}
        if hasattr(usage, "model_dump"):
            return dict(usage.model_dump())
        if isinstance(usage, dict):
            return dict(usage)
        return {k: getattr(usage, k) for k in ("prompt_tokens", "completion_tokens", "total_tokens") if getattr(usage, k, None) is not None}

    def judge(self, *, task: Any, predicate: str, result: Any, evidence: tuple[Any, ...]) -> dict[str, Any]:
        if not self.available:
            return {"available": False, "passed": False, "rationale": "provider unavailable"}
        deliverable = self._deliverable_text(result)
        # Avoid sending arbitrary huge output back to a verifier model.
        deliverable = deliverable[:40_000]
        payload = {
            "acceptance_condition": predicate,
            "task_description": getattr(task, "description", ""),
            "execution_success": bool(getattr(result, "success", False)),
            "exit_code": getattr(result, "exit_code", None),
            "deliverable_or_execution_output": deliverable,
            "evidence_count": len(evidence),
        }
        system = (
            "You are an independent acceptance verifier. Judge ONLY from the supplied execution output/deliverable and evidence count. "
            "Do not accept a task merely because it claims it is complete or repeats the acceptance condition. "
            "Return strict JSON with keys passed:boolean and rationale:string. If evidence is insufficient, passed must be false."
        )
        kwargs = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
            "response_format": {"type": "json_object"},
            "temperature": 0,
            "max_tokens": 300,
        }
        if self.provider_id == "deepseek":
            kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
        response = self._get_client().chat.completions.create(**kwargs)
        content = response.choices[0].message.content or "{}"
        if content.startswith("```"):
            content = content.replace("```json", "", 1).replace("```", "", 1).rsplit("```", 1)[0].strip()
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            parsed = {"passed": False, "rationale": "verifier returned invalid JSON"}
        return {
            "available": True,
            "passed": bool(parsed.get("passed", False)),
            "rationale": str(parsed.get("rationale") or ""),
            "provider": self.provider_id,
            "model": getattr(response, "model", None) or self.model,
            "request_id": getattr(response, "id", None),
            "usage": self._usage(response),
            "verification_semantics": "independent_model_semantic_judgment",
        }
