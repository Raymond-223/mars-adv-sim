"""Append-only EventStore with event-first projections, outbox, tracing and replay."""

from __future__ import annotations

import hashlib
import json
import uuid
from copy import deepcopy
from typing import Any, Mapping

from .models import Assignment, Event, Evidence, ExecutionResult, TaskNodeView, TaskState
from .state_machine import StateMachine


class EventStore:
    def __init__(
        self,
        database: Any,
        *,
        outbox_topic: str = "execution.events",
        schema_version: str = "0.1",
        snapshot_interval: int = 100,
    ) -> None:
        self.database = database
        self.outbox_topic = outbox_topic
        self.schema_version = schema_version
        self.snapshot_interval = max(0, int(snapshot_interval))
        self.database.initialize()

    def _parent_event_id(self, run_id: str, task_id: str | None) -> str | None:
        # Adapters expose an O(1)/indexed last-event lookup so append latency does
        # not grow linearly with long-running event histories.
        if hasattr(self.database, "last_event_id"):
            return self.database.last_event_id(run_id, task_id)
        if task_id is not None:
            task_events = self.events(run_id=run_id, task_id=task_id)
            if task_events:
                return task_events[-1].event_id
            run_level = [event for event in self.events(run_id=run_id) if event.task_id is None]
            return run_level[-1].event_id if run_level else None
        run_events = self.events(run_id=run_id)
        return run_events[-1].event_id if run_events else None

    def append_event(
        self,
        event_type: str,
        run_id: str,
        *,
        actor_id: str,
        task_id: str | None = None,
        node_id: str | None = None,
        trace_id: str | None = None,
        parent_event_id: str | None = None,
        model_id: str | None = None,
        schema_version: str | None = None,
        payload: Mapping[str, Any] | None = None,
        projection: TaskNodeView | None = None,
        publish: bool = True,
    ) -> Event:
        if not event_type or not run_id or not actor_id:
            raise ValueError("event_type, run_id, and actor_id are required")
        task_id = task_id or node_id
        trace_id = trace_id or f"trace_{uuid.uuid4().hex}"
        # Handbook trace contract requires every event to carry model_id.
        # System/planner/scheduler events are explicitly marked as system rather
        # than leaving the field absent/None.
        model_id = model_id or "system"
        if parent_event_id is None:
            parent_event_id = self._parent_event_id(run_id, task_id)
        event = Event(
            run_id=run_id,
            event_type=event_type,
            actor_id=actor_id,
            payload=deepcopy(dict(payload or {})),
            task_id=task_id,
            trace_id=trace_id,
            parent_event_id=parent_event_id,
            model_id=model_id,
            schema_version=schema_version or self.schema_version,
        )
        stored = self.database.append_bundle(
            event, projection, self.outbox_topic if publish else None
        )
        if (
            projection is not None
            and self.snapshot_interval > 0
            and stored.sequence is not None
            and stored.sequence % self.snapshot_interval == 0
        ):
            self.save_snapshot(projection, sequence=stored.sequence)
        return stored

    def create_run(
        self,
        run_id: str,
        *,
        actor_id: str,
        metadata: Mapping[str, Any] | None = None,
        trace_id: str | None = None,
    ) -> Event:
        if self.events(run_id=run_id):
            raise ValueError(f"run already exists: {run_id}")
        return self.append_event(
            "RUN_CREATED",
            run_id,
            actor_id=actor_id,
            trace_id=trace_id,
            payload={"metadata": dict(metadata or {})},
        )

    def create_task(
        self,
        task: TaskNodeView,
        *,
        actor_id: str,
        trace_id: str | None = None,
    ) -> Event:
        if self.get_task(task.run_id, task.task_id) is not None:
            raise ValueError(f"task already exists: {task.task_id}")
        task.state = TaskState.CREATED
        task.version = 1
        return self.append_event(
            "TASK_CREATED",
            task.run_id,
            actor_id=actor_id,
            task_id=task.task_id,
            trace_id=trace_id,
            payload={"projection": task.to_dict()},
            projection=task,
        )

    def transition(
        self,
        run_id: str,
        task_id: str,
        target: TaskState,
        *,
        actor_id: str,
        trace_id: str | None = None,
        parent_event_id: str | None = None,
        reason: str = "",
    ) -> TaskNodeView:
        current = self.require_task(run_id, task_id)
        next_state, paused_from = StateMachine.transition(
            current.state, target, paused_from=current.paused_from
        )
        updated = TaskNodeView.from_dict(current.to_dict())
        previous = updated.state
        updated.state = next_state
        updated.paused_from = paused_from
        updated.version += 1
        if target is TaskState.RUNNING and previous is not TaskState.PAUSED:
            updated.attempt += 1
        self.append_event(
            "TASK_STATE_CHANGED",
            run_id,
            actor_id=actor_id,
            task_id=task_id,
            trace_id=trace_id,
            parent_event_id=parent_event_id,
            payload={
                "from": previous.value,
                "to": target.value,
                "reason": reason,
                "projection": updated.to_dict(),
            },
            projection=updated,
        )
        return updated

    def assign(
        self,
        run_id: str,
        task_id: str,
        assignment: Assignment,
        *,
        actor_id: str,
        trace_id: str | None = None,
    ) -> TaskNodeView:
        current = self.require_task(run_id, task_id)
        if current.state is not TaskState.READY:
            raise ValueError(f"only READY tasks can be assigned: {task_id}")
        updated = TaskNodeView.from_dict(current.to_dict())
        if assignment.run_id is None or assignment.trace_id is None:
            raw = assignment.to_dict()
            raw["run_id"] = run_id
            raw["trace_id"] = trace_id
            assignment = Assignment.from_dict(raw)
        updated.assignment = assignment
        updated.version += 1
        self.append_event(
            "TASK_ASSIGNED",
            run_id,
            actor_id=actor_id,
            task_id=task_id,
            trace_id=trace_id,
            model_id=assignment.model_id,
            payload={"assignment": assignment.to_dict(), "projection": updated.to_dict()},
            projection=updated,
        )
        return updated

    def record_result(
        self,
        run_id: str,
        task_id: str,
        result: ExecutionResult,
        evidence: tuple[Evidence, ...],
        *,
        actor_id: str,
        trace_id: str | None = None,
    ) -> TaskNodeView:
        current = self.require_task(run_id, task_id)
        updated = TaskNodeView.from_dict(current.to_dict())
        updated.result = result
        updated.evidence = tuple(updated.evidence) + tuple(evidence)
        updated.version += 1
        self.append_event(
            "TASK_RESULT_UPDATED",
            run_id,
            actor_id=actor_id,
            task_id=task_id,
            trace_id=trace_id,
            model_id=(updated.assignment.model_id if updated.assignment else None),
            payload={
                "result": result.to_dict(),
                "evidence": [item.to_dict() for item in evidence],
                "projection": updated.to_dict(),
            },
            projection=updated,
        )
        return updated

    def recover_task(
        self,
        run_id: str,
        task_id: str,
        *,
        actor_id: str,
        trace_id: str | None = None,
        reason: str = "",
    ) -> TaskNodeView:
        """Event-first recovery reset used only by RecoveryEngine.

        Normal lifecycle transitions remain strict. Recovery is explicit in the
        append-only log and resets assignment/result/evidence for the failed
        attempt before returning the task to READY.
        """
        current = self.require_task(run_id, task_id)
        if current.state not in {TaskState.RUNNING, TaskState.VERIFYING, TaskState.FAILED}:
            raise ValueError(f"task is not recoverable from {current.state.value}: {task_id}")
        updated = TaskNodeView.from_dict(current.to_dict())
        previous = updated.state
        updated.state = TaskState.READY
        updated.paused_from = None
        updated.assignment = None
        updated.result = None
        updated.evidence = ()
        updated.version += 1
        self.append_event(
            "TASK_RECOVERED",
            run_id,
            actor_id=actor_id,
            task_id=task_id,
            trace_id=trace_id,
            payload={
                "from": previous.value,
                "to": TaskState.READY.value,
                "reason": reason,
                "projection": updated.to_dict(),
            },
            projection=updated,
        )
        return updated

    def replan_tasks(
        self,
        run_id: str,
        task_ids: list[str] | tuple[str, ...] | set[str],
        *,
        actor_id: str,
        trace_id: str | None = None,
        reason: str = "",
    ) -> list[TaskNodeView]:
        """Invalidate only an affected subgraph and return its roots to READY.

        This is the event-sourced equivalent of marking TaskGraph nodes STALE.
        There is no second task-state store: each reset is persisted as an
        append-only ``TASK_REPLANNED`` event before the projection changes.
        """
        affected = {str(task_id) for task_id in task_ids}
        if not affected:
            return []
        all_tasks = {task.task_id: task for task in self.tasks(run_id)}
        unknown = affected - set(all_tasks)
        if unknown:
            raise KeyError(f"unknown affected tasks: {sorted(unknown)}")

        # Reset every affected projection first, so descendants can never become
        # READY while an affected predecessor still carries an old SUCCEEDED view.
        replanned: list[TaskNodeView] = []
        for task_id in sorted(affected):
            current = self.require_task(run_id, task_id)
            updated = TaskNodeView.from_dict(current.to_dict())
            previous = updated.state
            updated.state = TaskState.PLANNED
            updated.paused_from = None
            updated.assignment = None
            updated.result = None
            updated.evidence = ()
            updated.version += 1
            self.append_event(
                "TASK_REPLANNED",
                run_id,
                actor_id=actor_id,
                task_id=task_id,
                trace_id=trace_id,
                payload={
                    "from": previous.value,
                    "to": TaskState.PLANNED.value,
                    "reason": reason,
                    "affected_task_ids": sorted(affected),
                    "projection": updated.to_dict(),
                },
                projection=updated,
            )
            replanned.append(updated)

        # Release only roots whose predecessors are outside the affected closure
        # and are still valid SUCCEEDED projections. Normal dependency release
        # handles the rest after those roots succeed again.
        refreshed = {task.task_id: task for task in self.tasks(run_id)}
        for task_id in sorted(affected):
            task = refreshed[task_id]
            if any(parent in affected for parent in task.depends_on):
                continue
            if all(refreshed[parent].state is TaskState.SUCCEEDED for parent in task.depends_on):
                self.transition(
                    run_id,
                    task_id,
                    TaskState.READY,
                    actor_id=actor_id,
                    trace_id=trace_id,
                    reason="affected-subgraph root is ready after local replan",
                )
        return [self.require_task(run_id, task_id) for task_id in sorted(affected)]

    def get_task(self, run_id: str, task_id: str) -> TaskNodeView | None:
        return self.database.get_task(run_id, task_id)

    def require_task(self, run_id: str, task_id: str) -> TaskNodeView:
        task = self.get_task(run_id, task_id)
        if task is None:
            raise KeyError(f"unknown task: {run_id}/{task_id}")
        return task

    def tasks(self, run_id: str, state: TaskState | None = None) -> list[TaskNodeView]:
        return self.database.list_tasks(run_id, state)

    def events(
        self,
        *,
        run_id: str | None = None,
        task_id: str | None = None,
        node_id: str | None = None,
        trace_id: str | None = None,
    ) -> list[Event]:
        return self.database.list_events(
            run_id=run_id, task_id=task_id or node_id, trace_id=trace_id
        )

    @staticmethod
    def _snapshot_checksum(document: Mapping[str, Any]) -> str:
        raw = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def save_snapshot(self, task: TaskNodeView, *, sequence: int | None = None) -> dict[str, Any]:
        if not hasattr(self.database, "save_snapshot"):
            return {}
        if sequence is None:
            task_events = self.events(run_id=task.run_id, task_id=task.task_id)
            sequence = task_events[-1].sequence if task_events else 0
        document = task.to_dict()
        snapshot = {
            "snapshot_id": f"snap_{uuid.uuid4().hex}",
            "run_id": task.run_id,
            "task_id": task.task_id,
            "sequence": int(sequence or 0),
            "document": document,
            "checksum": self._snapshot_checksum(document),
            "schema_version": self.schema_version,
        }
        self.database.save_snapshot(snapshot)
        return snapshot

    def replay(self, run_id: str, task_id: str) -> TaskNodeView:
        projection: TaskNodeView | None = None
        start_sequence = 0
        if hasattr(self.database, "latest_snapshot"):
            snapshot = self.database.latest_snapshot(run_id, task_id)
            if snapshot:
                document = snapshot.get("document", {})
                if snapshot.get("checksum") == self._snapshot_checksum(document):
                    projection = TaskNodeView.from_dict(document)
                    start_sequence = int(snapshot.get("sequence", 0))
        for event in self.events(run_id=run_id, task_id=task_id):
            if event.sequence is not None and event.sequence <= start_sequence:
                continue
            raw = event.payload.get("projection")
            if raw is not None:
                projection = TaskNodeView.from_dict(raw)
        if projection is None:
            raise KeyError(f"no task events: {run_id}/{task_id}")
        return projection

    def replay_run(self, run_id: str) -> list[TaskNodeView]:
        return [self.replay(run_id, task.task_id) for task in self.tasks(run_id)]

    def pending_outbox(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.database.pending_outbox(limit)

    def mark_outbox_published(self, outbox_id: str) -> None:
        self.database.mark_outbox_published(outbox_id)
