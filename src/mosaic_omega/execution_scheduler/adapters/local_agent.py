"""Device-tier Agents that execute locally without contacting any model provider.

These Agents make the end-edge-cloud story concrete instead of decorative:

* ``RequirementBaselineAgent`` compiles the frozen requirement baseline from the
  already-validated GoalSpec.  That node used to spend a full provider round trip
  restating information the deterministic compiler had just produced.
* ``LocalDeterministicAgent`` executes privacy-restricted work packages on the
  DEVICE tier.  It composes its deliverable from the ContextPack and upstream
  results, so data marked ``restricted``/``secret`` never leaves the machine.

Neither Agent claims model authorship.  Both go through the same ToolRuntime,
Evidence and Verifier path as a provider-backed Agent.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from ..models import Assignment, TaskNodeView, ToolCall

ContextProvider = Callable[[TaskNodeView, Assignment, str], dict[str, Any]]


def _deliverable_path(task: TaskNodeView, suffix: str = "md") -> str:
    return f".mosaic_deliverables/{task.run_id}/{task.task_id}.{suffix}"


def _bullets(title: str, items: Any) -> list[str]:
    values = [str(item).strip() for item in (items or ()) if str(item).strip()]
    if not values:
        return []
    return [f"## {title}", *[f"- {value}" for value in values], ""]


class _LocalAgentBase:
    """Shared local-execution contract: no network, explicit provenance."""

    authenticity_mode = "deterministic_tool_executor"
    api_transport = "local_process"
    endpoint_host = ""
    official_endpoint_verified = False

    def __init__(self, actor_id: str, *, role: str, context_provider: ContextProvider | None = None) -> None:
        self.actor_id = actor_id
        self.role = role
        self.context_provider = context_provider

    def _context(self, task: TaskNodeView, assignment: Assignment, trace_id: str) -> dict[str, Any]:
        if self.context_provider is None:
            return {}
        return self.context_provider(task, assignment, trace_id) or {}

    def _provenance(self, *, sections: int) -> dict[str, Any]:
        return {
            "provider": "local",
            "model": None,
            "request_id": None,
            "usage": {},
            "base_url": None,
            "role": self.role,
            "transport": self.api_transport,
            "endpoint_host": self.endpoint_host,
            "official_endpoint_verified": False,
            "execution_semantics": "deterministic_local_compilation",
            "network_egress": False,
            "section_count": sections,
        }

    def _write_call(
        self,
        task: TaskNodeView,
        assignment: Assignment,
        trace_id: str,
        *,
        content: str,
        sections: int,
    ) -> list[ToolCall]:
        relative = _deliverable_path(task)
        return [
            ToolCall(
                run_id=task.run_id,
                task_id=task.task_id,
                actor_id=self.actor_id,
                tool_name="write_file",
                arguments={
                    "path": relative,
                    "content": content,
                    "api_provenance": self._provenance(sections=sections),
                    "execution_intent": "persist_planner_output",
                },
                idempotency_key=f"{task.run_id}:{task.task_id}:{max(1, task.attempt)}:write_file",
                required_permissions=task.required_permissions,
                trace_id=trace_id,
                model_id=assignment.model_id,
                schema_version=assignment.schema_version,
            )
        ]


class RequirementBaselineAgent(_LocalAgentBase):
    """Freeze scope/constraints/budget/prohibitions without a provider call.

    The GoalSpec compiler has already validated and normalized these fields, so
    asking a language model to restate them adds latency and tokens without
    adding information.
    """

    def __init__(self, actor_id: str, *, context_provider: ContextProvider | None = None) -> None:
        super().__init__(actor_id, role="requirement_compiler", context_provider=context_provider)

    def plan(self, task: TaskNodeView, assignment: Assignment, trace_id: str) -> list[ToolCall]:
        context = self._context(task, assignment, trace_id)
        pack = context.get("context_pack", {}) if isinstance(context.get("context_pack"), dict) else {}
        lines: list[str] = [
            "# Requirement Baseline (deterministic compilation)",
            "",
            f"**Goal**: {pack.get('goal') or task.description}",
            "",
            "This baseline is compiled directly from the validated GoalSpec by the "
            "local device-tier requirement compiler. No model inference was used, "
            "so the frozen scope cannot drift from the user's declared intent.",
            "",
        ]
        lines += _bullets("Hard constraints", pack.get("hard_constraints"))
        lines += _bullets("Prohibitions", pack.get("prohibitions"))
        lines += _bullets("Acceptance conditions", task.acceptance_conditions)
        lines += _bullets("Relevant facts", pack.get("relevant_facts"))
        inputs = task.inputs.get("items", task.inputs) if isinstance(task.inputs, dict) else task.inputs
        lines += _bullets("Declared inputs", inputs if isinstance(inputs, list) else [json.dumps(inputs, ensure_ascii=False, default=str)])
        outputs = task.outputs.get("items", task.outputs) if isinstance(task.outputs, dict) else task.outputs
        lines += _bullets("Declared outputs", outputs if isinstance(outputs, list) else [json.dumps(outputs, ensure_ascii=False, default=str)])
        content = "\n".join(lines).rstrip() + "\n"
        sections = sum(1 for line in lines if line.startswith("## "))
        return self._write_call(task, assignment, trace_id, content=content, sections=sections)


class LocalDeterministicAgent(_LocalAgentBase):
    """Execute a work package on-device using only local context.

    Used for tasks whose privacy level forbids sending content to a cloud
    provider.  The deliverable is assembled from the ContextPack, the low-entropy
    messages already received from upstream Agents, and the task's own declared
    inputs/outputs.
    """

    def plan(self, task: TaskNodeView, assignment: Assignment, trace_id: str) -> list[ToolCall]:
        context = self._context(task, assignment, trace_id)
        pack = context.get("context_pack", {}) if isinstance(context.get("context_pack"), dict) else {}
        messages = context.get("messages", []) if isinstance(context.get("messages"), list) else []
        lines: list[str] = [
            f"# {task.description}",
            "",
            f"**Task**: `{task.task_id}` · **Executed by**: `{self.actor_id}` (device tier, no network egress)",
            f"**Privacy level**: `{task.privacy_level}`",
            "",
            "This work package was executed locally because its privacy classification "
            "forbids sending its content to a cloud model provider. The result is "
            "composed from on-device context only.",
            "",
        ]
        lines += _bullets("Goal", [pack.get("goal")] if pack.get("goal") else [])
        lines += _bullets("Hard constraints in force", pack.get("hard_constraints"))
        lines += _bullets("Prohibitions in force", pack.get("prohibitions"))
        lines += _bullets("Upstream results used", pack.get("previous_results"))
        lines += _bullets(
            "Low-entropy messages received",
            [f"{item.get('sender')} → {item.get('receiver')}: {item.get('summary') or '(structured delta only)'}"
             for item in messages if isinstance(item, dict)],
        )
        lines += _bullets("Acceptance conditions to satisfy", task.acceptance_conditions)
        lines += _bullets("Evidence references", pack.get("evidence_refs"))
        content = "\n".join(lines).rstrip() + "\n"
        sections = sum(1 for line in lines if line.startswith("## "))
        return self._write_call(task, assignment, trace_id, content=content, sections=sections)
