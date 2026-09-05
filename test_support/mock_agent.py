"""Deterministic Agent for tests/demos; it never calls a real LLM."""

from __future__ import annotations

from mosaic_omega.execution_scheduler.models import Assignment, Evidence, ExecutionResult, TaskNodeView, ToolCall


class MockAgent:
    authenticity_mode = "mock"

    def __init__(self, actor_id: str) -> None:
        self.actor_id = actor_id

    def plan(
        self, task: TaskNodeView, assignment: Assignment, trace_id: str
    ) -> list[ToolCall]:
        return [
            ToolCall(
                run_id=task.run_id,
                task_id=task.task_id,
                actor_id=self.actor_id,
                tool_name=assignment.tool_id,
                arguments={
                    "description": task.description,
                    "acceptance_conditions": list(task.acceptance_conditions),
                    "test_fixture_verifier": True,
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

    def verify(
        self,
        task: TaskNodeView,
        result: ExecutionResult,
        evidence: tuple[Evidence, ...],
    ) -> tuple[bool, dict]:
        checks = [{"condition": "execution_success", "passed": result.success}]
        for condition in task.acceptance_conditions:
            checks.append(
                {
                    "condition": condition,
                    "passed": condition.casefold() in result.output.casefold(),
                }
            )
        passed = bool(evidence) and all(item["passed"] for item in checks)
        return passed, {
            "passed": passed,
            "checks": checks,
            "evidence_ids": [item.evidence_id for item in evidence],
        }
