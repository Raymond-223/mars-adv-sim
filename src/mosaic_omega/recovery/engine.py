"""Impact-graph driven local recovery.

Recovery is deliberately centralized here.  It does not own task state; every
state mutation is delegated to the authoritative EventStore so retry, replace,
rollback, replan and safe-stop actions are replayable.
"""
from __future__ import annotations

from collections import defaultdict, deque
from typing import Any

from ..execution_scheduler.models import ErrorClass, TaskState, ToolCall
from .models import RecoveryAction, RecoveryPlan


class RecoveryEngine:
    def __init__(
        self,
        event_store: Any,
        capability_registry: Any,
        *,
        tool_runtime: Any | None = None,
        max_task_retries: int = 1,
    ) -> None:
        self.events = event_store
        self.capabilities = capability_registry
        self.tools = tool_runtime
        self.max_task_retries = max(0, int(max_task_retries))

    def impact_subgraph(self, run_id: str, failed_task_id: str) -> tuple[str, ...]:
        """Return failed node + descendants + evidence-dependent descendants."""
        tasks = self.events.tasks(run_id)
        by_id = {task.task_id: task for task in tasks}
        children: dict[str, set[str]] = defaultdict(set)
        evidence_owner: dict[str, str] = {}
        for task in tasks:
            for parent in task.depends_on:
                children[parent].add(task.task_id)
            for evidence in task.evidence:
                evidence_owner[evidence.evidence_id] = task.task_id
        for task in tasks:
            for evidence_id in task.evidence_dependencies:
                owner = evidence_owner.get(evidence_id)
                if owner:
                    children[owner].add(task.task_id)

        affected: list[str] = []
        seen: set[str] = set()
        queue: deque[str] = deque([failed_task_id])
        while queue:
            current = queue.popleft()
            if current in seen or current not in by_id:
                continue
            seen.add(current)
            affected.append(current)
            queue.extend(sorted(children.get(current, ())))
        return tuple(affected)

    @staticmethod
    def _action(error_class: ErrorClass | None) -> RecoveryAction:
        mapping = {
            ErrorClass.RETRYABLE: RecoveryAction.RETRY,
            ErrorClass.REPLACEABLE: RecoveryAction.REPLACE,
            ErrorClass.ROLLBACK_REQUIRED: RecoveryAction.ROLLBACK,
            ErrorClass.REPLAN_REQUIRED: RecoveryAction.REPLAN,
            ErrorClass.SAFE_STOP: RecoveryAction.SAFE_STOP,
            None: RecoveryAction.SAFE_STOP,
        }
        return mapping[error_class]

    def plan(
        self,
        run_id: str,
        task_id: str,
        *,
        error_class: ErrorClass | None,
        reason: str,
        failed_actor_id: str | None = None,
    ) -> RecoveryPlan:
        task = self.events.require_task(run_id, task_id)
        action = self._action(error_class)
        affected = self.impact_subgraph(run_id, task_id)
        retry_allowed = (
            action in {RecoveryAction.RETRY, RecoveryAction.REPLACE}
            and task.attempt <= self.max_task_retries
        )
        if action is RecoveryAction.REPLACE and failed_actor_id:
            try:
                self.capabilities.update_runtime(failed_actor_id, online=False)
            except KeyError:
                pass
        return RecoveryPlan(run_id, task_id, action, affected, reason, retry_allowed)

    def _rollback(self, plan: RecoveryPlan, *, trace_id: str | None) -> bool:
        """Execute an explicit compensation ToolCall, then locally replan.

        Rollback is never guessed. A side-effecting task must declare
        ``metadata.rollback_tool`` with the same shape as ``metadata.tool``.
        Missing compensation information triggers safe stop.
        """
        task = self.events.require_task(plan.run_id, plan.failed_task_id)
        spec = task.metadata.get("rollback_tool")
        if not isinstance(spec, dict) or not spec.get("name"):
            self._safe_stop(
                plan,
                trace_id=trace_id,
                reason=f"rollback required but rollback_tool is missing: {plan.reason}",
            )
            return True
        if self.tools is None:
            self._safe_stop(
                plan,
                trace_id=trace_id,
                reason="rollback required but ToolRuntime is unavailable",
            )
            return True
        assignment = task.assignment
        if assignment is None:
            self._safe_stop(plan, trace_id=trace_id, reason="rollback task has no assignment")
            return True
        call = ToolCall(
            run_id=plan.run_id,
            task_id=plan.failed_task_id,
            actor_id=assignment.agent_id,
            tool_name=str(spec["name"]),
            arguments=dict(spec.get("arguments", {})),
            idempotency_key=(
                f"{plan.run_id}:{plan.failed_task_id}:rollback:{max(1, task.attempt)}:{spec['name']}"
            ),
            required_permissions=frozenset(
                str(item) for item in spec.get("required_permissions", task.required_permissions)
            ),
            timeout_s=float(spec["timeout_s"]) if spec.get("timeout_s") is not None else None,
            trace_id=trace_id,
            model_id=assignment.model_id,
            schema_version=assignment.schema_version,
        )
        result, evidence = self.tools.execute(call)
        self.events.append_event(
            "ROLLBACK_EXECUTED",
            plan.run_id,
            actor_id="recovery",
            task_id=plan.failed_task_id,
            trace_id=trace_id,
            model_id=assignment.model_id,
            payload={
                "tool_call": call.to_dict(),
                "result": result.to_dict(),
                "evidence": evidence.to_dict(),
            },
        )
        if not result.success:
            self._safe_stop(
                plan,
                trace_id=trace_id,
                reason=f"rollback failed: {result.error or 'unknown rollback error'}",
            )
            return True
        self.events.replan_tasks(
            plan.run_id,
            plan.affected_task_ids,
            actor_id="recovery",
            trace_id=trace_id,
            reason=f"rollback completed; {plan.reason}",
        )
        return True

    def _safe_stop(
        self,
        plan: RecoveryPlan,
        *,
        trace_id: str | None,
        reason: str | None = None,
    ) -> None:
        task = self.events.require_task(plan.run_id, plan.failed_task_id)
        stop_reason = reason or plan.reason
        if task.state not in {TaskState.PAUSED, TaskState.SUCCEEDED, TaskState.FAILED}:
            self.events.transition(
                plan.run_id,
                plan.failed_task_id,
                TaskState.PAUSED,
                actor_id="recovery",
                trace_id=trace_id,
                reason=stop_reason,
            )
        self.events.append_event(
            "SAFE_STOP_TRIGGERED",
            plan.run_id,
            actor_id="recovery",
            task_id=plan.failed_task_id,
            trace_id=trace_id,
            payload={"reason": stop_reason, "affected_task_ids": list(plan.affected_task_ids)},
        )

    def execute(self, plan: RecoveryPlan, *, trace_id: str | None = None) -> bool:
        """Execute the planned recovery action.

        Return ``True`` when the failure was handled (retry scheduled, subgraph
        replanned, compensation executed, or task safely paused). Return
        ``False`` only when retry/replace has exhausted its configured budget;
        the Orchestrator then records terminal FAILED.
        """
        self.events.append_event(
            "RECOVERY_PLANNED",
            plan.run_id,
            actor_id="recovery",
            task_id=plan.failed_task_id,
            trace_id=trace_id,
            payload=plan.to_dict(),
        )

        if plan.action in {RecoveryAction.RETRY, RecoveryAction.REPLACE}:
            if not plan.retry_allowed:
                return False
            self.events.recover_task(
                plan.run_id,
                plan.failed_task_id,
                actor_id="recovery",
                trace_id=trace_id,
                reason=plan.reason,
            )
            return True

        if plan.action is RecoveryAction.REPLAN:
            self.events.replan_tasks(
                plan.run_id,
                plan.affected_task_ids,
                actor_id="recovery",
                trace_id=trace_id,
                reason=plan.reason,
            )
            return True

        if plan.action is RecoveryAction.ROLLBACK:
            return self._rollback(plan, trace_id=trace_id)

        self._safe_stop(plan, trace_id=trace_id)
        return True
