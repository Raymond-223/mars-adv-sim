"""System conductor: advance READY tasks and delegate all algorithmic work."""

from __future__ import annotations

import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from typing import Any

from ..recovery import RecoveryEngine
from ..verifier import VerifierService
from .event_store import EventStore
from .models import ErrorClass, Evidence, ExecutionResult, TaskState
from .posterior import BetaPosteriorUpdater
from .scheduler import Scheduler
from .tool_runtime import ToolRuntime


class Orchestrator:
    """Coordinates the authoritative execution chain.

    Scheduling, tool execution, verification and recovery are separate services;
    the orchestrator only sequences them and records state transitions/events.
    """

    def __init__(
        self,
        events: EventStore,
        scheduler: Scheduler,
        tools: ToolRuntime,
        posterior: BetaPosteriorUpdater,
        agents: dict[str, Any],
        *,
        verifier: VerifierService,
        recovery: RecoveryEngine,
        max_task_retries: int = 1,
    ) -> None:
        self.events = events
        self.scheduler = scheduler
        self.tools = tools
        self.posterior = posterior
        self.agents = agents
        self.verifier = verifier
        self.recovery = recovery
        self.max_task_retries = max(0, int(max_task_retries))
        self.last_round_made_progress = False

    @staticmethod
    def _exception_class(exc: Exception) -> ErrorClass:
        text = f"{type(exc).__name__}: {exc}".casefold()
        if any(token in text for token in ("timeout", "connection", "temporar", "network", "mqtt")):
            return ErrorClass.RETRYABLE
        if any(token in text for token in ("offline", "unavailable", "not found")):
            return ErrorClass.REPLACEABLE
        if any(token in text for token in ("permission", "invalid", "unknown tool")):
            return ErrorClass.SAFE_STOP
        return ErrorClass.RETRYABLE

    def _recover_or_fail(
        self,
        *,
        run_id: str,
        task_id: str,
        trace_id: str,
        assignment: Any,
        error_class: ErrorClass | None,
        reason: str,
    ) -> bool:
        plan = self.recovery.plan(
            run_id,
            task_id,
            error_class=error_class,
            reason=reason,
            failed_actor_id=assignment.agent_id if assignment else None,
        )
        if self.recovery.execute(plan, trace_id=trace_id):
            self.last_round_made_progress = True
            return True
        current = self.events.get_task(run_id, task_id)
        if current and current.state not in {TaskState.SUCCEEDED, TaskState.FAILED}:
            self.events.transition(
                run_id,
                task_id,
                TaskState.FAILED,
                actor_id="orchestrator",
                trace_id=trace_id,
                reason=reason,
            )
            self.last_round_made_progress = True
        return False

    def _execute_assignment(self, run_id: str, assignment: Any) -> str | None:
        """Execute one already-selected READY assignment.

        EventStore/database adapters serialize their own writes.  This method is
        therefore safe to run in a worker thread while independent DAG nodes are
        executed concurrently.  Dependencies are released after the task has
        reached SUCCEEDED, so downstream work still observes the DAG barrier.
        """
        trace_id = f"trace_{uuid.uuid4().hex}"
        task_id = assignment.task_id
        running = None
        combined: ExecutionResult | None = None
        try:
            self.events.assign(
                run_id, task_id, assignment, actor_id="scheduler", trace_id=trace_id
            )
            running = self.events.transition(
                run_id, task_id, TaskState.RUNNING, actor_id="orchestrator",
                trace_id=trace_id, reason="assignment accepted",
            )
            agent = self.agents.get(assignment.agent_id)
            if agent is None:
                raise RuntimeError(f"assigned agent has no execution adapter: {assignment.agent_id}")

            calls = agent.plan(running, assignment, trace_id)
            if not calls:
                raise RuntimeError("agent returned no ToolCall")

            results: list[ExecutionResult] = []
            evidence: list[Evidence] = []
            for call in calls:
                result, item = self.tools.execute(call)
                results.append(result)
                evidence.append(item)
                self.events.append_event(
                    "TOOL_EXECUTED", run_id, actor_id=assignment.agent_id,
                    task_id=task_id, trace_id=trace_id, model_id=assignment.model_id,
                    payload={"tool_call": call.to_dict(), "result": result.to_dict(), "evidence": item.to_dict()},
                )

            combined = self._combine(results)
            verifying = self.events.transition(
                run_id, task_id, TaskState.VERIFYING, actor_id="orchestrator",
                trace_id=trace_id, reason="tool execution finished",
            )
            verification = self.verifier.verify(verifying, combined, tuple(evidence))
            passed = bool(verification.passed)
            verified_evidence = tuple(
                replace(item, verification_status="VERIFIED" if passed else "REJECTED")
                for item in evidence
            )
            self.events.record_result(
                run_id, task_id, combined, verified_evidence, actor_id="orchestrator", trace_id=trace_id
            )
            self.events.append_event(
                "TASK_VERIFIED", run_id, actor_id="verifier", task_id=task_id,
                trace_id=trace_id, model_id=assignment.model_id,
                payload={
                    "verification": verification.to_dict(), "passed": passed,
                    "evidence_ids": [item.evidence_id for item in verified_evidence],
                },
            )
            self._update_posteriors(
                run_id, task_id, trace_id, assignment, running.task_type, passed, combined
            )
            if passed:
                self.events.transition(
                    run_id, task_id, TaskState.SUCCEEDED, actor_id="orchestrator",
                    trace_id=trace_id, reason="verification predicates passed",
                )
                self._release_dependents(run_id)
                return task_id

            error_class = combined.error_class or ErrorClass.SAFE_STOP
            self._recover_or_fail(
                run_id=run_id, task_id=task_id, trace_id=trace_id, assignment=assignment,
                error_class=error_class, reason=combined.error or "verification failed",
            )
            return None
        except Exception as exc:
            error_class = self._exception_class(exc)
            self.events.append_event(
                "TASK_EXECUTION_ERROR", run_id, actor_id="orchestrator", task_id=task_id,
                trace_id=trace_id, model_id=assignment.model_id,
                payload={"error": f"{type(exc).__name__}: {exc}", "error_class": error_class.value},
            )
            current = self.events.get_task(run_id, task_id)
            if current and current.state in {TaskState.RUNNING, TaskState.VERIFYING}:
                failed_result = combined or ExecutionResult(
                    call_id=f"failure_{uuid.uuid4().hex}", success=False,
                    error=f"{type(exc).__name__}: {exc}", error_class=error_class,
                )
                self._update_posteriors(
                    run_id, task_id, trace_id, assignment, current.task_type, False, failed_result
                )
                self._recover_or_fail(
                    run_id=run_id, task_id=task_id, trace_id=trace_id, assignment=assignment,
                    error_class=error_class, reason=f"{type(exc).__name__}: {exc}",
                )
            return None

    def run_once(self, run_id: str) -> list[str]:
        self.last_round_made_progress = False
        ready = self.events.tasks(run_id, TaskState.READY)
        assignments = self.scheduler.assign_tasks(ready)
        if not assignments:
            return []

        self.last_round_made_progress = True
        # Scheduler already enforces device capacity and only returns currently
        # feasible assignments.  Execute that independent READY batch in
        # parallel; dependency-constrained tasks remain blocked until the next
        # round, which is the DAG barrier.
        workers = max(1, len(assignments))
        if workers == 1:
            item = self._execute_assignment(run_id, assignments[0])
            return [item] if item else []

        completed: list[str] = []
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="mosaic-task") as pool:
            futures = [pool.submit(self._execute_assignment, run_id, assignment) for assignment in assignments]
            for future in as_completed(futures):
                task_id = future.result()
                if task_id:
                    completed.append(task_id)
        completed.sort()
        return completed

    @staticmethod
    def _combine(results: list[ExecutionResult]) -> ExecutionResult:
        if not results:
            raise ValueError("cannot combine an empty result list")
        first = results[0]
        success = all(item.success for item in results)
        errors = [item.error for item in results if item.error]
        error_class = next(
            (item.error_class for item in results if item.error_class is not None), None
        )
        return ExecutionResult(
            call_id=first.call_id,
            success=success,
            output="\n".join(item.output for item in results if item.output),
            error="; ".join(errors) if errors else None,
            exit_code=next(
                (item.exit_code for item in results if item.exit_code not in {None, 0}), 0
            ),
            started_at=min(item.started_at for item in results),
            finished_at=max(item.finished_at for item in results),
            metadata=(
                ({"tool_call_count": len(results), "tool_results": [dict(item.metadata or {}) for item in results]}
                 | (dict(first.metadata or {}) if len(results) == 1 else {})
                 | ({"deliverable_relatives": [str(item.metadata.get("deliverable_relative")) for item in results if (item.metadata or {}).get("deliverable_relative")]} if len(results) > 1 else {}))
            ),
            error_class=error_class,
        )

    def _update_posteriors(
        self,
        run_id: str,
        task_id: str,
        trace_id: str,
        assignment: Any,
        task_type: str,
        success: bool,
        result: ExecutionResult,
    ) -> None:
        quality = 1.0 if success else 0.0
        timeout = bool(result.error and "timeout" in result.error.casefold())
        for actor_id in {
            assignment.agent_id,
            assignment.model_id,
            assignment.tool_id,
            assignment.resource_id,
        }:
            profile = self.posterior.update(
                actor_id,
                task_type,
                success=success,
                quality=quality,
                timeout=timeout,
                observed_at=time.time(),
            )
            self.events.append_event(
                "CAPABILITY_UPDATED",
                run_id,
                actor_id="capability",
                task_id=task_id,
                trace_id=trace_id,
                model_id=assignment.model_id,
                payload={
                    "actor_id": actor_id,
                    "task_type": task_type,
                    "success": success,
                    "profile": profile.to_dict(),
                },
            )

    def _release_dependents(self, run_id: str) -> None:
        tasks = {task.task_id: task for task in self.events.tasks(run_id)}
        for task in tasks.values():
            if task.state is not TaskState.PLANNED:
                continue
            if all(tasks[parent].state is TaskState.SUCCEEDED for parent in task.depends_on):
                self.events.transition(
                    run_id,
                    task.task_id,
                    TaskState.READY,
                    actor_id="orchestrator",
                    reason="all dependencies succeeded",
                )

    def run_until_blocked(self, run_id: str, *, max_rounds: int = 100) -> list[str]:
        completed: list[str] = []
        for _ in range(max_rounds):
            if not self.events.tasks(run_id, TaskState.READY):
                break
            progress = self.run_once(run_id)
            completed.extend(progress)
            if not self.last_round_made_progress:
                break
        return completed

    def pause_task(
        self, run_id: str, task_id: str, reason: str = "manual pause"
    ) -> None:
        self.events.transition(
            run_id, task_id, TaskState.PAUSED, actor_id="orchestrator", reason=reason
        )

    def resume_task(self, run_id: str, task_id: str) -> None:
        task = self.events.require_task(run_id, task_id)
        if task.state is not TaskState.PAUSED or task.paused_from is None:
            raise ValueError(f"task is not resumable: {task_id}")
        self.events.transition(
            run_id,
            task_id,
            task.paused_from,
            actor_id="orchestrator",
            reason="resume from pause",
        )
