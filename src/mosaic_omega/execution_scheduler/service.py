"""Unified application API for the execution scheduler."""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from .adapters.local_tool_executor import LocalToolExecutor
from .adapters.postgres import MemoryDatabase, PostgresDatabase
from .adapters.sqlite import SQLiteDatabase
from .capability import CapabilityRegistry
from .config import Settings, get_settings
from .cost_model import CostModel
from .event_store import EventStore
from .idempotency import IdempotencyManager
from .models import CapabilityProfile, Evidence, ExecutionResult, TaskNodeView, TaskState, ToolCall
from .orchestrator import Orchestrator
from .posterior import BetaPosteriorUpdater
from .scheduler import Scheduler
from .tool_runtime import ToolRuntime
from ..verifier import VerifierService
from ..recovery import RecoveryEngine


class ExecutionSchedulerService:
    """Facade required by the module contract.

    Public operations: ``create_run``, ``append_event``, ``execute_tool``,
    ``register_actor``, ``assign_tasks`` and ``update_result``.
    """

    def __init__(self, settings: Settings | None = None, *, database: Any | None = None) -> None:
        self.settings = settings or get_settings()
        self.database = database or PostgresDatabase(self.settings.database_url)
        self.events = EventStore(
            self.database,
            schema_version=self.settings.schema_version,
            snapshot_interval=self.settings.snapshot_interval,
        )
        self.capabilities = CapabilityRegistry(self.database)
        self.idempotency = IdempotencyManager(self.database)
        self.executor = LocalToolExecutor(self.settings)
        self.tools = ToolRuntime(self.executor, self.capabilities, self.idempotency, self.settings)
        self.cost_model = CostModel(self.settings)
        self.scheduler = Scheduler(self.capabilities, self.cost_model, self.settings)
        self.posterior = BetaPosteriorUpdater(
            self.capabilities, decay_per_day=self.settings.posterior_decay_per_day
        )
        self.agents: dict[str, Any] = {}
        self.verifier = VerifierService(self.settings.workspace)
        self.recovery = RecoveryEngine(
            self.events,
            self.capabilities,
            tool_runtime=self.tools,
            max_task_retries=self.settings.max_task_retries,
        )
        self.orchestrator = Orchestrator(
            self.events,
            self.scheduler,
            self.tools,
            self.posterior,
            self.agents,
            verifier=self.verifier,
            recovery=self.recovery,
            max_task_retries=self.settings.max_task_retries,
        )

    @classmethod
    def memory(cls, settings: Settings | None = None) -> "ExecutionSchedulerService":
        return cls(settings or get_settings(), database=MemoryDatabase())

    @classmethod
    def sqlite(cls, settings: Settings | None = None, *, path: str | None = None) -> "ExecutionSchedulerService":
        cfg = settings or get_settings()
        db_path = path or str(cfg.workspace / ".mosaic_state" / "execution.sqlite3")
        return cls(cfg, database=SQLiteDatabase(db_path))

    def create_run(
        self,
        tasks: Sequence[TaskNodeView | Mapping[str, Any]],
        *,
        run_id: str | None = None,
        actor_id: str = "service",
        metadata: Mapping[str, Any] | None = None,
    ) -> str:
        run_id = run_id or f"run_{uuid.uuid4().hex[:12]}"
        trace_id = f"trace_{uuid.uuid4().hex}"
        normalized = [self._task(run_id, item) for item in tasks]
        self._validate_graph(normalized)
        self.events.create_run(run_id, actor_id=actor_id, metadata=metadata, trace_id=trace_id)
        for task in normalized:
            self.events.create_task(task, actor_id=actor_id, trace_id=trace_id)
            self.events.transition(
                run_id,
                task.task_id,
                TaskState.PLANNED,
                actor_id=actor_id,
                trace_id=trace_id,
                reason="task accepted into execution plan",
            )
        for task in normalized:
            if not task.depends_on:
                self.events.transition(
                    run_id,
                    task.task_id,
                    TaskState.READY,
                    actor_id=actor_id,
                    trace_id=trace_id,
                    reason="entry task has no dependencies",
                )
        return run_id

    @staticmethod
    def _task(run_id: str, item: TaskNodeView | Mapping[str, Any]) -> TaskNodeView:
        if isinstance(item, TaskNodeView):
            raw = item.to_dict()
            raw["run_id"] = run_id
            raw["state"] = TaskState.CREATED.value
            raw["version"] = 0
            return TaskNodeView.from_dict(raw)
        raw = dict(item)
        metadata = dict(raw.get("metadata", {}))
        placement = dict(raw.get("placement", metadata.get("placement", {})) or {})
        task_id = raw.get("task_id", raw.get("node_id"))
        if task_id is None:
            raise KeyError("task_id/node_id")
        depends_on = raw.get("depends_on", raw.get("dependencies", raw.get("predecessors", ())))
        acceptance = raw.get(
            "acceptance_conditions",
            raw.get("acceptance", metadata.get("acceptance_conditions", ())),
        )

        # ToDAG keeps planner-only detail in ``metadata``. Promote the fields
        # required by execution at this single ownership boundary so evidence
        # dependencies, I/O, risk and resource requirements cannot be lost.
        required_capabilities = raw.get("required_capabilities")
        if required_capabilities is None:
            required_capabilities = raw.get("required_skills", metadata.get("required_skills"))
        if required_capabilities is None:
            required_capabilities = (raw.get("required_skill"),) if raw.get("required_skill") else ()
        if isinstance(required_capabilities, str):
            required_capabilities = (required_capabilities,)

        required_permissions = raw.get("required_permissions", metadata.get("required_permissions", ()))
        if isinstance(required_permissions, str):
            required_permissions = (required_permissions,)

        inputs_raw = raw.get("inputs", metadata.get("inputs", {}))
        outputs_raw = raw.get("outputs", metadata.get("outputs", {}))
        inputs = dict(inputs_raw) if isinstance(inputs_raw, Mapping) else {"items": list(inputs_raw or ())}
        outputs = dict(outputs_raw) if isinstance(outputs_raw, Mapping) else {"items": list(outputs_raw or ())}

        evidence_dependencies = raw.get(
            "evidence_dependencies", metadata.get("evidence_dependencies", ())
        )
        resource_requirements = raw.get(
            "resource_requirements", metadata.get("resource_requirements", placement)
        )
        risk_raw = raw.get("risk", metadata.get("risk", "normal"))
        risk = str(risk_raw.get("level", "normal")) if isinstance(risk_raw, Mapping) else str(risk_raw)
        metadata.setdefault("risk_detail", risk_raw if isinstance(risk_raw, Mapping) else {"level": risk})
        metadata.setdefault("inputs", inputs_raw)
        metadata.setdefault("outputs", outputs_raw)

        privacy_level = raw.get(
            "privacy_level",
            metadata.get("privacy_level", placement.get("data_sensitivity", "normal")),
        )
        data_location = raw.get(
            "data_location",
            metadata.get("data_location", placement.get("data_location")),
        )
        max_latency = raw.get("max_latency_ms", placement.get("max_latency_ms"))

        return TaskNodeView(
            run_id=run_id,
            task_id=str(task_id),
            task_type=str(raw.get("task_type", raw.get("type", raw.get("required_skill", "general")))),
            description=str(raw.get("description", raw.get("title", task_id))),
            depends_on=tuple(str(value) for value in depends_on),
            priority=int(raw.get("priority", 5)),
            required_capabilities=frozenset(str(value) for value in required_capabilities if value),
            required_permissions=frozenset(str(value) for value in required_permissions),
            acceptance_conditions=tuple(str(value) for value in acceptance),
            privacy_level=str(privacy_level),
            data_location=str(data_location) if data_location else None,
            estimated_tokens=int(raw.get("estimated_tokens", metadata.get("estimated_tokens", 0))),
            max_latency_ms=float(max_latency) if max_latency is not None else None,
            inputs=inputs,
            outputs=outputs,
            evidence_dependencies=tuple(str(value) for value in evidence_dependencies),
            resource_requirements=dict(resource_requirements or {}),
            risk=risk,
            metadata=metadata,
        )

    @staticmethod
    def _validate_graph(tasks: list[TaskNodeView]) -> None:
        ids = {task.task_id for task in tasks}
        if len(ids) != len(tasks):
            raise ValueError("task IDs must be unique")
        for task in tasks:
            missing = set(task.depends_on) - ids
            if missing:
                raise ValueError(
                    f"task {task.task_id} has missing dependencies: {sorted(missing)}"
                )
        visiting: set[str] = set()
        visited: set[str] = set()
        by_id = {task.task_id: task for task in tasks}

        def visit(task_id: str) -> None:
            if task_id in visiting:
                raise ValueError("task graph contains a cycle")
            if task_id in visited:
                return
            visiting.add(task_id)
            for parent in by_id[task_id].depends_on:
                visit(parent)
            visiting.remove(task_id)
            visited.add(task_id)

        for task_id in sorted(ids):
            visit(task_id)

    def append_event(self, *args: Any, **kwargs: Any):
        return self.events.append_event(*args, **kwargs)

    def execute_tool(self, call: ToolCall) -> tuple[ExecutionResult, Evidence]:
        return self.tools.execute(call)

    def register_actor(
        self, profile: CapabilityProfile, *, adapter: Any | None = None
    ) -> CapabilityProfile:
        saved = self.capabilities.register(profile)
        if adapter is not None:
            self.agents[profile.actor_id] = adapter
        return saved

    def assign_tasks(self, run_id: str):
        assignments = self.scheduler.assign_tasks(
            self.events.tasks(run_id, TaskState.READY)
        )
        for assignment in assignments:
            self.events.assign(
                run_id, assignment.task_id, assignment, actor_id="scheduler"
            )
        return assignments

    def update_result(
        self,
        run_id: str,
        task_id: str,
        result: ExecutionResult,
        evidence: tuple[Evidence, ...],
        *,
        actor_id: str = "service",
        trace_id: str | None = None,
    ) -> TaskNodeView:
        return self.events.record_result(
            run_id,
            task_id,
            result,
            evidence,
            actor_id=actor_id,
            trace_id=trace_id,
        )

    def run_once(self, run_id: str) -> list[str]:
        return self.orchestrator.run_once(run_id)

    def run_until_blocked(self, run_id: str, *, max_rounds: int = 100) -> list[str]:
        return self.orchestrator.run_until_blocked(run_id, max_rounds=max_rounds)

    def resume_run(self, run_id: str, *, max_rounds: int = 100) -> dict[str, Any]:
        """Resume a durable run after a process restart at a safe boundary.

        SQLite/PostgreSQL persist the authoritative task projections.  READY and
        PLANNED work can therefore continue immediately after the caller
        re-registers the execution adapters.  A task that was interrupted while
        RUNNING/VERIFYING is handled conservatively:

        * non-side-effecting tools are explicitly recovered to READY and may be
          executed again;
        * side-effecting tools are PAUSED rather than blindly repeated, because a
          crash may have happened after the external side effect but before its
          completion event was committed.  This prevents duplicate writes/builds
          from being disguised as automatic recovery.

        The returned structure is suitable for observability and tests; it does
        not claim that an unsafe side effect was resumed automatically.
        """
        if not self.events.events(run_id=run_id):
            raise KeyError(f"unknown run: {run_id}")

        recovered: list[str] = []
        safe_stops: list[str] = []
        for task in self.events.tasks(run_id):
            if task.state not in {TaskState.RUNNING, TaskState.VERIFYING}:
                continue
            assignment = task.assignment
            tool_name = assignment.tool_id if assignment is not None else None
            spec = self.tools._tools.get(tool_name) if tool_name else None
            side_effecting = True if spec is None else bool(spec.side_effecting)
            if side_effecting:
                self.events.transition(
                    run_id,
                    task.task_id,
                    TaskState.PAUSED,
                    actor_id="resume-manager",
                    reason=(
                        "process restart detected during a side-effecting operation; "
                        "safe-stop prevents an unverified duplicate side effect"
                    ),
                )
                self.events.append_event(
                    "RUN_RESUME_SAFE_STOP",
                    run_id,
                    actor_id="resume-manager",
                    task_id=task.task_id,
                    payload={
                        "tool_name": tool_name,
                        "previous_state": task.state.value,
                        "automatic_reexecution": False,
                        "reason": "side_effect_completion_unknown_after_process_restart",
                    },
                )
                safe_stops.append(task.task_id)
            else:
                self.events.recover_task(
                    run_id,
                    task.task_id,
                    actor_id="resume-manager",
                    reason="process restart recovery for non-side-effecting operation",
                )
                recovered.append(task.task_id)

        completed = self.run_until_blocked(run_id, max_rounds=max_rounds)
        return {
            "run_id": run_id,
            "recovered_non_side_effecting": recovered,
            "safe_stopped_side_effecting": safe_stops,
            "completed_after_resume": completed,
            "task_states": {task.task_id: task.state.value for task in self.events.tasks(run_id)},
            "durability": self.database.__class__.__name__,
        }

    def replay(self, run_id: str, task_id: str) -> TaskNodeView:
        return self.events.replay(run_id, task_id)

    def replay_run(self, run_id: str) -> list[TaskNodeView]:
        return self.events.replay_run(run_id)

    def trace(self, trace_id: str):
        return self.events.events(trace_id=trace_id)
