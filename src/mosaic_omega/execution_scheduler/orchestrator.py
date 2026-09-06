"""System conductor: advance READY tasks and delegate all algorithmic work."""

from __future__ import annotations

import threading
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from typing import Any

from ..recovery import RecoveryEngine
from ..verifier import VerifierService
from .capability import CapabilityRegistry
from .event_store import EventStore
from .models import ActorKind, ErrorClass, Evidence, ExecutionResult, TaskState
from .posterior import BetaPosteriorUpdater
from .scheduler import Scheduler
from .tool_runtime import ToolRuntime


def _combined_metadata(results: list[ExecutionResult], first: ExecutionResult) -> dict[str, Any]:
    """Merge per-call metadata for one task.

    A multi-step tool plan must still expose ``deliverable_relative``.  When an
    Agent could only ever emit a single ToolCall this fell out of merging the
    first result's metadata; once Agents plan several steps, the node's artifact
    is produced by the *last* step, and dropping the singular key made the
    ``artifact_exists`` verifier predicate fail on tasks that had in fact written
    their deliverable.
    """
    deliverables = [
        str((item.metadata or {}).get("deliverable_relative"))
        for item in results
        if (item.metadata or {}).get("deliverable_relative")
    ]
    metadata: dict[str, Any] = {
        "tool_call_count": len(results),
        "tool_results": [dict(item.metadata or {}) for item in results],
    }
    if len(results) == 1:
        metadata |= dict(first.metadata or {})
        return metadata
    if deliverables:
        metadata["deliverable_relatives"] = deliverables
        # The node's own deliverable is the last artifact its plan produced.
        metadata["deliverable_relative"] = deliverables[-1]
    for key in ("test_fixture_verifier",):
        if any((item.metadata or {}).get(key) for item in results):
            metadata[key] = True
    return metadata


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
        capabilities: CapabilityRegistry | None = None,
        max_concurrency: int = 6,
    ) -> None:
        self.events = events
        self.scheduler = scheduler
        self.tools = tools
        self.posterior = posterior
        self.agents = agents
        self.verifier = verifier
        self.recovery = recovery
        self.capabilities = capabilities if capabilities is not None else scheduler.registry
        self.max_task_retries = max(0, int(max_task_retries))
        self.max_concurrency = max(1, int(max_concurrency))
        self.last_round_made_progress = False
        self.round_index = 0
        # Live per-Agent occupancy so the registry (and therefore the console)
        # reflects what each Agent instance is actually doing right now instead
        # of a static configured load.
        self._live_load: Counter[str] = Counter()
        self._load_lock = threading.Lock()

    def _set_agent_load(self, agent_id: str, delta: int) -> None:
        """Publish real Agent occupancy to the CapabilityRegistry.

        Load is a scheduling input (``_agent_capacities``) and a UI signal, so it
        must track actual execution.  Failures here are non-fatal: they must never
        break the task the Agent is running.
        """
        with self._load_lock:
            self._live_load[agent_id] = max(0, self._live_load[agent_id] + delta)
            running = self._live_load[agent_id]
        try:
            profile = self.capabilities.get(agent_id)
        except (KeyError, AttributeError):
            return
        if profile.kind is not ActorKind.AGENT:
            return
        capacity = max(1, profile.capacity)
        try:
            self.capabilities.update_runtime(
                agent_id,
                current_load=min(1.0, running / capacity),
                metadata={
                    "running_task_count": running,
                    "max_concurrent_tasks": capacity,
                    "load_source": "orchestrator_live_execution",
                    "load_updated_at": time.time(),
                },
            )
        except (KeyError, ValueError):
            return

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
        # Per-phase wall clock so the console can explain *where* a slow run spent
        # its time (model call vs tool vs verification) instead of only a total.
        timings: dict[str, float] = {}
        task_started = time.perf_counter()
        self._set_agent_load(assignment.agent_id, +1)
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

            plan_started = time.perf_counter()
            calls = agent.plan(running, assignment, trace_id)
            timings["agent_plan_ms"] = (time.perf_counter() - plan_started) * 1000.0
            if not calls:
                raise RuntimeError("agent returned no ToolCall")

            tool_started = time.perf_counter()
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
            timings["tool_execution_ms"] = (time.perf_counter() - tool_started) * 1000.0

            combined = self._combine(results)
            verifying = self.events.transition(
                run_id, task_id, TaskState.VERIFYING, actor_id="orchestrator",
                trace_id=trace_id, reason="tool execution finished",
            )
            verify_started = time.perf_counter()
            verification = self.verifier.verify(verifying, combined, tuple(evidence))
            timings["verification_ms"] = (time.perf_counter() - verify_started) * 1000.0
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
        finally:
            self._set_agent_load(assignment.agent_id, -1)
            timings["total_ms"] = (time.perf_counter() - task_started) * 1000.0
            self.events.append_event(
                "TASK_PHASE_TIMING", run_id, actor_id="orchestrator", task_id=task_id,
                trace_id=trace_id, model_id=assignment.model_id,
                payload={
                    "agent_id": assignment.agent_id,
                    "timings_ms": {key: round(value, 3) for key, value in timings.items()},
                    "measurement": "wall clock measured inside Orchestrator._execute_assignment",
                },
            )

    def _publish_scheduling_round(self, run_id: str, assignments: list[Any]) -> None:
        """Append the explainable scheduling decision as an authoritative event.

        Candidate sets, elimination reasons and remaining capacity are what let a
        reviewer see same-role Agents competing, rather than a single opaque
        "assigned/unassigned" flag.
        """
        record = getattr(self.scheduler, "last_round", None)
        if record is None:
            return
        payload = record.to_dict()
        payload["round_index"] = self.round_index
        payload["assigned_task_ids"] = sorted(item.task_id for item in assignments)
        payload["selected_agents"] = sorted({item.agent_id for item in assignments})
        self.events.append_event(
            "SCHEDULING_ROUND", run_id, actor_id="scheduler", payload=payload
        )

    def run_once(self, run_id: str) -> list[str]:
        self.last_round_made_progress = False
        self.round_index += 1
        ready = self.events.tasks(run_id, TaskState.READY)
        assignments = self.scheduler.assign_tasks(ready)
        self._publish_scheduling_round(run_id, assignments)
        if not assignments:
            return []

        self.last_round_made_progress = True
        # The scheduler enforces both Agent concurrency and resource capacity and
        # only returns currently feasible assignments.  Execute that independent
        # READY batch in parallel; dependency-constrained tasks remain blocked
        # until the next round, which is the DAG barrier.  Worker count is capped
        # so a wide DAG layer cannot open an unbounded number of provider
        # connections at once.
        workers = min(self.max_concurrency, max(1, len(assignments)))
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
            metadata=_combined_metadata(results, first),
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
