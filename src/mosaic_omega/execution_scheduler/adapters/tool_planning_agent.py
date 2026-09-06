"""Provider-backed Agent that plans *real* ToolRuntime calls, not just prose.

The previous behaviour was: every task, whatever its type, produced one ``task``
tool call whose only effect was persisting the model's own text.  A node that
said "apply the patch and run the tests" therefore never read, wrote, built or
tested anything.

This adapter asks the model for a structured plan over the tool menu its task's
delivery kind allows, validates every step locally, and returns the surviving
steps as ToolCalls.  Validation is fail-closed:

* the tool must be in the menu for this delivery kind;
* required arguments must be present;
* paths must stay inside the workspace;
* commands must be a non-empty list of strings.

ToolRuntime still applies the authoritative permission, timeout, idempotency and
evidence rules on top of this; nothing here bypasses that boundary.  A plan that
survives validation with no usable step falls back to persisting the reasoning
output, and the fallback reason is recorded rather than hidden.
"""
from __future__ import annotations

import json
from typing import Any

from ..models import Assignment, TaskNodeView, ToolCall
from .deepseek_agent import DeepSeekAgent

#: Tools each delivery kind may plan.  ``task`` is always implicitly available as
#: the reasoning-persistence fallback and is never part of a menu.
TOOL_MENUS: dict[str, tuple[str, ...]] = {
    "document": ("write_file",),
    "research": ("write_file",),
    "data": ("read_file", "write_file", "shell"),
    "software": ("read_file", "write_file", "shell", "build", "test"),
    "robotics": ("write_file", "shell"),
    "verification": ("read_file", "write_file"),
    "reasoning": (),
}

TOOL_ARGUMENTS: dict[str, tuple[str, ...]] = {
    "read_file": ("path",),
    "write_file": ("path", "content"),
    "shell": ("command",),
    "build": (),
    "test": (),
}

TOOL_HELP: dict[str, str] = {
    "read_file": 'read_file{"path": "<workspace-relative file>"} — inspect an existing file',
    "write_file": 'write_file{"path": "<workspace-relative file>", "content": "<full text>"} — create or replace a file',
    "shell": 'shell{"command": ["python", "-c", "..."]} — run an allow-listed executable inside the workspace',
    "build": 'build{} — run the project build (defaults to python -m compileall)',
    "test": 'test{} — run the project tests (defaults to python -m unittest discover)',
}

MAX_STEPS = 6


def _normalize_relative(raw: Any) -> str | None:
    """Return a safe workspace-relative path, or None when it escapes."""
    text = str(raw or "").strip().replace("\\", "/")
    if not text:
        return None
    if text.startswith("/") or (len(text) > 1 and text[1] == ":"):
        return None
    parts = [part for part in text.split("/") if part not in {"", "."}]
    if not parts or any(part == ".." for part in parts):
        return None
    return "/".join(parts)


class ToolPlanningAgent(DeepSeekAgent):
    """DeepSeek-backed Agent whose output is a validated plan of real tool calls."""

    def __init__(self, actor_id: str, *, role: str, delivery_kinds: tuple[str, ...] = (), **kwargs: Any) -> None:
        super().__init__(actor_id, role=role, **kwargs)
        # Kinds this Agent is allowed to act on at all.  Empty means "whatever the
        # task declares", which the ToolRuntime permission check still bounds.
        self.delivery_kinds = tuple(delivery_kinds)

    # ------------------------------------------------------------------ prompt

    @staticmethod
    def _delivery_kind(task: TaskNodeView) -> str:
        raw = str((task.metadata or {}).get("delivery_kind") or "reasoning").strip().casefold()
        return raw if raw in TOOL_MENUS else "reasoning"

    @staticmethod
    def _deliverable_path(task: TaskNodeView) -> str:
        return f".mosaic_deliverables/{task.run_id}/{task.task_id}.md"

    def _tool_system_prompt(self, kind: str, menu: tuple[str, ...]) -> str:
        return (
            "你是 MOSAIC-Ω 多智能体系统中的异构执行 Agent。"
            f"你的角色是 {self.role}，当前任务的交付类型是 {kind}。\n"
            "你必须输出一个真正会被执行的工具调用计划，而不是描述你打算做什么。\n"
            "可用工具：\n" + "\n".join(f"- {TOOL_HELP[name]}" for name in menu) + "\n"
            "规则：\n"
            "- 所有路径必须是工作区相对路径，禁止绝对路径和 ..\n"
            "- shell 只能运行 python；需要复杂逻辑时先 write_file 写脚本再 shell 运行它\n"
            f"- 最多 {MAX_STEPS} 步\n"
            "- 不要声称完成未实际发生的操作\n"
            '严格返回 JSON：{"steps": [{"tool": "<name>", "arguments": {...}, "purpose": "<why>"}], '
            '"deliverable_markdown": "<本节点的最终交付文档全文>"}'
        )

    def _tool_user_prompt(self, task: TaskNodeView, context: dict[str, Any]) -> str:
        upstream = [
            {"task_id": parent, "deliverable_path": f".mosaic_deliverables/{task.run_id}/{parent}.md"}
            for parent in task.depends_on
        ]
        payload = {
            "task": {
                "task_id": task.task_id,
                "description": task.description,
                "acceptance_conditions": list(task.acceptance_conditions),
                "outputs": task.outputs,
                "risk": task.risk,
            },
            "upstream_deliverables": upstream,
            "deliverable_path_for_this_task": self._deliverable_path(task),
            "context_pack": context.get("context_pack", {}),
            "low_entropy_messages": context.get("messages", []),
        }
        document = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
        if len(document) > self.prompt_char_limit:
            document = document[: self.prompt_char_limit] + "\n[上下文因预算限制已截断]"
        return "请为以下任务节点生成可执行的工具调用计划与最终交付文档：\n" + document

    # ---------------------------------------------------------------- validate

    @staticmethod
    def _validate_step(raw: Any, menu: tuple[str, ...]) -> tuple[dict[str, Any] | None, str | None]:
        if not isinstance(raw, dict):
            return None, "step is not an object"
        name = str(raw.get("tool") or "").strip()
        if name not in menu:
            return None, f"tool {name!r} is not in the menu for this delivery kind"
        arguments = raw.get("arguments")
        if not isinstance(arguments, dict):
            arguments = {}
        missing = [key for key in TOOL_ARGUMENTS.get(name, ()) if key not in arguments]
        if missing:
            return None, f"{name} is missing required arguments {missing}"
        clean: dict[str, Any] = {}
        if "path" in arguments:
            relative = _normalize_relative(arguments["path"])
            if relative is None:
                return None, f"{name} path {arguments['path']!r} escapes the workspace"
            clean["path"] = relative
        if "content" in arguments:
            clean["content"] = str(arguments["content"])
        if "command" in arguments:
            command = arguments["command"]
            if isinstance(command, str):
                return None, "command must be a list of strings, not a shell string"
            if not isinstance(command, list) or not command or not all(isinstance(x, str) for x in command):
                return None, "command must be a non-empty list of strings"
            clean["command"] = list(command)
        return {"tool": name, "arguments": clean, "purpose": str(raw.get("purpose") or "")[:200]}, None

    # ------------------------------------------------------------------- plan

    def plan(self, task: TaskNodeView, assignment: Assignment, trace_id: str) -> list[ToolCall]:
        kind = self._delivery_kind(task)
        menu = TOOL_MENUS[kind]
        if self.delivery_kinds and kind not in self.delivery_kinds:
            # Not this Agent's kind of work: behave as a reasoning Agent rather
            # than silently exercising tools it was not registered for.
            menu = ()
        if not menu:
            return super().plan(task, assignment, trace_id)

        context = (
            self.context_provider(task, assignment, trace_id)
            if self.context_provider is not None else {}
        )
        try:
            request_kwargs: dict[str, Any] = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": self._tool_system_prompt(kind, menu)},
                    {"role": "user", "content": self._tool_user_prompt(task, context)},
                ],
                "max_tokens": self.max_tokens,
                "temperature": self.temperature,
                "response_format": {"type": "json_object"},
            }
            if self.provider_id == "deepseek":
                request_kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
            response = self.client.chat.completions.create(**request_kwargs)
        except Exception as exc:
            raise RuntimeError(f"工具规划 Agent 调用失败：{type(exc).__name__}: {exc}") from exc

        content = (response.choices[0].message.content or "").strip()
        if not content:
            raise RuntimeError("工具规划 Agent 返回空内容")
        if content.startswith("```"):
            content = content.replace("```json", "", 1).replace("```", "", 1).rsplit("```", 1)[0].strip()
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            parsed = {}

        steps: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        for raw in (parsed.get("steps") or [])[: MAX_STEPS * 2] if isinstance(parsed, dict) else []:
            if len(steps) >= MAX_STEPS:
                rejected.append({"step": raw, "reason": f"exceeds the {MAX_STEPS}-step budget"})
                continue
            step, reason = self._validate_step(raw, menu)
            if step is None:
                rejected.append({"step": raw, "reason": reason})
            else:
                steps.append(step)

        deliverable = str(parsed.get("deliverable_markdown") or "").strip() if isinstance(parsed, dict) else ""
        deliverable_path = self._deliverable_path(task)
        # Drop any step that would write the deliverable itself; it is appended
        # once, last, so the node always ends with exactly one artifact.
        steps = [s for s in steps if not (s["tool"] == "write_file" and s["arguments"].get("path") == deliverable_path)]

        provenance = {
            **self._plan_provenance(response),
            "delivery_kind": kind,
            "tool_menu": list(menu),
            "planned_step_count": len(steps),
            "rejected_step_count": len(rejected),
            "rejected_steps": rejected[:6],
        }
        if not deliverable:
            provenance["deliverable_fallback"] = (
                "model returned no deliverable_markdown; the raw plan document was persisted instead"
            )
            deliverable = content

        calls: list[ToolCall] = []
        for index, step in enumerate(steps):
            calls.append(ToolCall(
                run_id=task.run_id,
                task_id=task.task_id,
                actor_id=self.actor_id,
                tool_name=step["tool"],
                arguments={
                    **step["arguments"],
                    # Only the first call carries api_provenance so one model
                    # request stays one counted API request.
                    **({"api_provenance": provenance} if index == 0 else {"planner_provenance": {
                        "planned_by": self.actor_id, "purpose": step["purpose"],
                    }}),
                    "execution_intent": "act_on_environment",
                },
                idempotency_key=f"{task.run_id}:{task.task_id}:{max(1, task.attempt)}:{index}:{step['tool']}",
                required_permissions=task.required_permissions,
                trace_id=trace_id,
                model_id=assignment.model_id,
                schema_version=assignment.schema_version,
            ))

        calls.append(ToolCall(
            run_id=task.run_id,
            task_id=task.task_id,
            actor_id=self.actor_id,
            tool_name="write_file",
            arguments={
                "path": deliverable_path,
                "content": deliverable,
                "execution_intent": "persist_planner_output",
                **({"api_provenance": provenance} if not calls else {"planner_provenance": {
                    "planned_by": self.actor_id, "purpose": "persist the node deliverable",
                }}),
            },
            idempotency_key=f"{task.run_id}:{task.task_id}:{max(1, task.attempt)}:deliverable",
            required_permissions=task.required_permissions,
            trace_id=trace_id,
            model_id=assignment.model_id,
            schema_version=assignment.schema_version,
        ))
        return calls

    def _plan_provenance(self, response: Any) -> dict[str, Any]:
        return {
            "provider": self.provider_id,
            "model": getattr(response, "model", None) or self.model,
            "request_id": getattr(response, "id", None),
            "usage": self._usage(response),
            "base_url": self.base_url,
            "role": self.role,
            "transport": self.api_transport,
            "endpoint_host": self.endpoint_host,
            "official_endpoint_verified": self.official_endpoint_verified,
        }
