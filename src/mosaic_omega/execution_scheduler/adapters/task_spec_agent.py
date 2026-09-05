"""Generic deterministic Agent that converts TaskNode tool metadata into ToolCall.

This is a reusable scenario adapter, not a scenario-specific orchestrator.
"""
from __future__ import annotations

from ..models import Assignment, TaskNodeView, ToolCall


class TaskSpecAgent:
    authenticity_mode = "deterministic_tool_executor"

    def __init__(self, actor_id: str) -> None:
        self.actor_id = actor_id

    def plan(self, task: TaskNodeView, assignment: Assignment, trace_id: str) -> list[ToolCall]:
        spec = task.metadata.get("tool", {})
        if not isinstance(spec, dict):
            raise ValueError("task.metadata.tool must be an object")
        tool_name = str(spec.get("name") or assignment.tool_id)
        arguments = dict(spec.get("arguments", {}))
        return [
            ToolCall(
                run_id=task.run_id,
                task_id=task.task_id,
                actor_id=self.actor_id,
                tool_name=tool_name,
                arguments=arguments,
                idempotency_key=f"{task.run_id}:{task.task_id}:{max(1, task.attempt)}:{tool_name}",
                required_permissions=task.required_permissions,
                timeout_s=spec.get("timeout_s"),
                trace_id=trace_id,
                model_id=assignment.model_id,
                schema_version=assignment.schema_version,
            )
        ]
