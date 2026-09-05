"""PostgreSQL/SQLAlchemy persistence plus an in-memory test adapter.

Event, task projection and outbox rows are committed in one transaction. Event rows
are insert-only. PostgreSQL is the production source of truth; ``MemoryDatabase`` is
only for tests/demos.
"""

from __future__ import annotations

import threading
import time
import uuid
from copy import deepcopy
from typing import Any, Mapping

from ..models import CapabilityProfile, Event, TaskNodeView, TaskState


class MemoryDatabase:
    """Transaction-shaped in-memory adapter used only by tests and demos."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._events: list[dict[str, Any]] = []
        self._last_run_event: dict[str, str] = {}
        self._last_run_level_event: dict[str, str] = {}
        self._last_task_event: dict[tuple[str, str], str] = {}
        self._tasks: dict[tuple[str, str], dict[str, Any]] = {}
        self._outbox: dict[str, dict[str, Any]] = {}
        self._idempotency: dict[str, dict[str, Any]] = {}
        self._profiles: dict[str, dict[str, Any]] = {}
        self._snapshots: list[dict[str, Any]] = []

    def initialize(self) -> None:
        return None

    def append_bundle(
        self, event: Event, view: TaskNodeView | None, outbox_topic: str | None
    ) -> Event:
        with self._lock:
            stored = event.to_dict()
            stored["sequence"] = len(self._events) + 1
            self._events.append(deepcopy(stored))
            self._last_run_event[event.run_id] = event.event_id
            if event.task_id is not None:
                self._last_task_event[(event.run_id, event.task_id)] = event.event_id
            else:
                self._last_run_level_event[event.run_id] = event.event_id
            if view is not None:
                self._tasks[(view.run_id, view.task_id)] = view.to_dict()
            if outbox_topic:
                outbox_id = f"out_{uuid.uuid4().hex}"
                self._outbox[outbox_id] = {
                    "outbox_id": outbox_id,
                    "event_id": event.event_id,
                    "topic": outbox_topic,
                    "payload": deepcopy(stored),
                    "created_at": time.time(),
                    "published_at": None,
                }
            return Event.from_dict(stored)

    def last_event_id(self, run_id: str, task_id: str | None = None) -> str | None:
        with self._lock:
            if task_id is not None:
                return self._last_task_event.get((run_id, task_id)) or self._last_run_level_event.get(run_id)
            return self._last_run_event.get(run_id)

    def list_events(
        self,
        *,
        run_id: str | None = None,
        task_id: str | None = None,
        trace_id: str | None = None,
    ) -> list[Event]:
        with self._lock:
            rows = [deepcopy(item) for item in self._events]
        if run_id is not None:
            rows = [item for item in rows if item["run_id"] == run_id]
        if task_id is not None:
            rows = [
                item
                for item in rows
                if item.get("task_id", item.get("node_id")) == task_id
            ]
        if trace_id is not None:
            rows = [item for item in rows if item.get("trace_id") == trace_id]
        return [Event.from_dict(item) for item in rows]

    def get_task(self, run_id: str, task_id: str) -> TaskNodeView | None:
        with self._lock:
            raw = deepcopy(self._tasks.get((run_id, task_id)))
        return TaskNodeView.from_dict(raw) if raw else None

    def list_tasks(self, run_id: str, state: TaskState | None = None) -> list[TaskNodeView]:
        with self._lock:
            rows = [
                deepcopy(raw)
                for (current_run, _), raw in self._tasks.items()
                if current_run == run_id
            ]
        tasks = [TaskNodeView.from_dict(raw) for raw in rows]
        if state is not None:
            tasks = [task for task in tasks if task.state is state]
        return sorted(tasks, key=lambda task: (-task.priority, task.task_id))

    def pending_outbox(self, limit: int) -> list[dict[str, Any]]:
        with self._lock:
            rows = [
                deepcopy(item)
                for item in self._outbox.values()
                if item["published_at"] is None
            ]
        return sorted(rows, key=lambda item: item["created_at"])[:limit]

    def mark_outbox_published(self, outbox_id: str) -> None:
        with self._lock:
            self._outbox[outbox_id]["published_at"] = time.time()

    def begin_idempotency(
        self, key: str, *, fingerprint: str | None = None
    ) -> tuple[bool, dict[str, Any] | None]:
        with self._lock:
            existing = self._idempotency.get(key)
            if existing is not None:
                return False, deepcopy(existing)
            self._idempotency[key] = {
                "key": key,
                "fingerprint": fingerprint,
                "status": "RUNNING",
                "result": None,
                "error": None,
                "updated_at": time.time(),
            }
            return True, None

    def finish_idempotency(
        self,
        key: str,
        *,
        result: Mapping[str, Any] | None,
        error: str | None,
    ) -> None:
        with self._lock:
            row = self._idempotency[key]
            row.update(
                {
                    "status": "FAILED" if error else "SUCCEEDED",
                    "result": deepcopy(result),
                    "error": error,
                    "updated_at": time.time(),
                }
            )

    def get_idempotency(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            return deepcopy(self._idempotency.get(key))

    def save_profile(self, profile: CapabilityProfile) -> None:
        with self._lock:
            self._profiles[profile.actor_id] = profile.to_dict()

    def get_profile(self, actor_id: str) -> CapabilityProfile | None:
        with self._lock:
            raw = deepcopy(self._profiles.get(actor_id))
        return CapabilityProfile.from_dict(raw) if raw else None

    def list_profiles(self) -> list[CapabilityProfile]:
        with self._lock:
            rows = [deepcopy(self._profiles[key]) for key in sorted(self._profiles)]
        return [CapabilityProfile.from_dict(raw) for raw in rows]

    def save_snapshot(self, snapshot: Mapping[str, Any]) -> None:
        with self._lock:
            self._snapshots.append(deepcopy(dict(snapshot)))

    def latest_snapshot(self, run_id: str, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            matches = [
                deepcopy(item)
                for item in self._snapshots
                if item["run_id"] == run_id and item["task_id"] == task_id
            ]
        if not matches:
            return None
        return max(matches, key=lambda item: int(item.get("sequence", 0)))


class PostgresDatabase:
    """SQLAlchemy Core adapter; ``execution_events`` is append-only by API design."""

    def __init__(self, database_url: str) -> None:
        try:
            import sqlalchemy as sa
        except ImportError as exc:
            raise RuntimeError(
                "PostgreSQL persistence requires SQLAlchemy and psycopg; install requirements.txt"
            ) from exc
        self.sa = sa
        self.engine = sa.create_engine(database_url, pool_pre_ping=True, future=True)
        metadata = sa.MetaData()

        self.events = sa.Table(
            "execution_events",
            metadata,
            sa.Column("sequence", sa.BigInteger, primary_key=True, autoincrement=True),
            sa.Column("event_id", sa.String(64), nullable=False, unique=True),
            sa.Column("run_id", sa.String(128), nullable=False, index=True),
            sa.Column("node_id", sa.String(128), nullable=True, index=True),
            sa.Column("trace_id", sa.String(128), nullable=False, index=True),
            sa.Column("parent_event_id", sa.String(64), nullable=True, index=True),
            sa.Column("type", sa.String(96), nullable=False, index=True),
            sa.Column("actor_id", sa.String(128), nullable=False),
            sa.Column("model_id", sa.String(128), nullable=True),
            sa.Column("timestamp", sa.Float, nullable=False),
            sa.Column("schema_version", sa.String(32), nullable=False),
            sa.Column("payload", sa.JSON, nullable=False),
        )
        self.task_views = sa.Table(
            "task_node_views",
            metadata,
            sa.Column("run_id", sa.String(128), primary_key=True),
            sa.Column("task_id", sa.String(128), primary_key=True),
            sa.Column("state", sa.String(32), nullable=False, index=True),
            sa.Column("version", sa.Integer, nullable=False),
            sa.Column("document", sa.JSON, nullable=False),
            sa.Column("updated_at", sa.Float, nullable=False),
        )
        self.outbox = sa.Table(
            "execution_outbox",
            metadata,
            sa.Column("outbox_id", sa.String(64), primary_key=True),
            sa.Column("event_id", sa.String(64), nullable=False, unique=True),
            sa.Column("topic", sa.String(160), nullable=False),
            sa.Column("payload", sa.JSON, nullable=False),
            sa.Column("created_at", sa.Float, nullable=False),
            sa.Column("published_at", sa.Float, nullable=True, index=True),
        )
        self.idempotency = sa.Table(
            "execution_idempotency",
            metadata,
            sa.Column("key", sa.String(256), primary_key=True),
            sa.Column("fingerprint", sa.String(64), nullable=True),
            sa.Column("status", sa.String(24), nullable=False),
            sa.Column("result", sa.JSON, nullable=True),
            sa.Column("error", sa.Text, nullable=True),
            sa.Column("updated_at", sa.Float, nullable=False),
        )
        self.profiles = sa.Table(
            "capability_profiles",
            metadata,
            sa.Column("actor_id", sa.String(128), primary_key=True),
            sa.Column("kind", sa.String(32), nullable=False, index=True),
            sa.Column("document", sa.JSON, nullable=False),
            sa.Column("updated_at", sa.Float, nullable=False),
        )
        self.snapshots = sa.Table(
            "execution_snapshots",
            metadata,
            sa.Column("snapshot_id", sa.String(64), primary_key=True),
            sa.Column("run_id", sa.String(128), nullable=False, index=True),
            sa.Column("task_id", sa.String(128), nullable=False, index=True),
            sa.Column("sequence", sa.BigInteger, nullable=False, index=True),
            sa.Column("document", sa.JSON, nullable=False),
            sa.Column("checksum", sa.String(64), nullable=False),
            sa.Column("schema_version", sa.String(32), nullable=False),
            sa.Column("created_at", sa.Float, nullable=False),
        )
        self.metadata = metadata

    def initialize(self) -> None:
        self.metadata.create_all(self.engine)

    @staticmethod
    def _upsert(conn: Any, table: Any, keys: dict[str, Any], values: dict[str, Any]) -> None:
        condition = None
        for name, value in keys.items():
            clause = table.c[name] == value
            condition = clause if condition is None else condition & clause
        result = conn.execute(table.update().where(condition).values(**values))
        if result.rowcount == 0:
            conn.execute(table.insert().values(**keys, **values))

    def append_bundle(
        self, event: Event, view: TaskNodeView | None, outbox_topic: str | None
    ) -> Event:
        raw = event.to_storage_dict()
        with self.engine.begin() as conn:
            result = conn.execute(self.events.insert().values(**raw))
            sequence = int(result.inserted_primary_key[0])
            stored = Event.from_dict({**raw, "sequence": sequence})
            if view is not None:
                self._upsert(
                    conn,
                    self.task_views,
                    {"run_id": view.run_id, "task_id": view.task_id},
                    {
                        "state": view.state.value,
                        "version": view.version,
                        "document": view.to_dict(),
                        "updated_at": time.time(),
                    },
                )
            if outbox_topic:
                conn.execute(
                    self.outbox.insert().values(
                        outbox_id=f"out_{uuid.uuid4().hex}",
                        event_id=event.event_id,
                        topic=outbox_topic,
                        payload=stored.to_dict(),
                        created_at=time.time(),
                        published_at=None,
                    )
                )
        return stored

    def last_event_id(self, run_id: str, task_id: str | None = None) -> str | None:
        # Prefer the task chain.  If the task has no event yet, fall back to the
        # latest run-level event so the first task event still has a parent.
        query = self.sa.select(self.events.c.event_id).where(self.events.c.run_id == run_id)
        if task_id is not None:
            task_query = (
                query.where(self.events.c.node_id == task_id)
                .order_by(self.events.c.sequence.desc())
                .limit(1)
            )
            with self.engine.connect() as conn:
                event_id = conn.execute(task_query).scalar_one_or_none()
            if event_id is not None:
                return str(event_id)
            query = query.where(self.events.c.node_id.is_(None))
        with self.engine.connect() as conn:
            event_id = conn.execute(
                query.order_by(self.events.c.sequence.desc()).limit(1)
            ).scalar_one_or_none()
        return str(event_id) if event_id is not None else None

    def list_events(
        self,
        *,
        run_id: str | None = None,
        task_id: str | None = None,
        trace_id: str | None = None,
    ) -> list[Event]:
        query = self.sa.select(self.events)
        if run_id is not None:
            query = query.where(self.events.c.run_id == run_id)
        if task_id is not None:
            query = query.where(self.events.c.node_id == task_id)
        if trace_id is not None:
            query = query.where(self.events.c.trace_id == trace_id)
        with self.engine.connect() as conn:
            rows = conn.execute(query.order_by(self.events.c.sequence)).mappings().all()
        return [Event.from_dict(row) for row in rows]

    def get_task(self, run_id: str, task_id: str) -> TaskNodeView | None:
        query = self.sa.select(self.task_views.c.document).where(
            (self.task_views.c.run_id == run_id)
            & (self.task_views.c.task_id == task_id)
        )
        with self.engine.connect() as conn:
            raw = conn.execute(query).scalar_one_or_none()
        return TaskNodeView.from_dict(raw) if raw else None

    def list_tasks(self, run_id: str, state: TaskState | None = None) -> list[TaskNodeView]:
        query = self.sa.select(self.task_views.c.document).where(
            self.task_views.c.run_id == run_id
        )
        if state is not None:
            query = query.where(self.task_views.c.state == state.value)
        with self.engine.connect() as conn:
            rows = conn.execute(query).scalars().all()
        return sorted(
            (TaskNodeView.from_dict(raw) for raw in rows),
            key=lambda task: (-task.priority, task.task_id),
        )

    def pending_outbox(self, limit: int) -> list[dict[str, Any]]:
        query = (
            self.sa.select(self.outbox)
            .where(self.outbox.c.published_at.is_(None))
            .order_by(self.outbox.c.created_at)
            .limit(limit)
        )
        with self.engine.connect() as conn:
            return [dict(row) for row in conn.execute(query).mappings().all()]

    def mark_outbox_published(self, outbox_id: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                self.outbox.update()
                .where(self.outbox.c.outbox_id == outbox_id)
                .values(published_at=time.time())
            )

    def begin_idempotency(
        self, key: str, *, fingerprint: str | None = None
    ) -> tuple[bool, dict[str, Any] | None]:
        try:
            with self.engine.begin() as conn:
                conn.execute(
                    self.idempotency.insert().values(
                        key=key,
                        fingerprint=fingerprint,
                        status="RUNNING",
                        result=None,
                        error=None,
                        updated_at=time.time(),
                    )
                )
            return True, None
        except self.sa.exc.IntegrityError:
            return False, self.get_idempotency(key)

    def finish_idempotency(
        self,
        key: str,
        *,
        result: Mapping[str, Any] | None,
        error: str | None,
    ) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                self.idempotency.update()
                .where(self.idempotency.c.key == key)
                .values(
                    status="FAILED" if error else "SUCCEEDED",
                    result=deepcopy(result),
                    error=error,
                    updated_at=time.time(),
                )
            )

    def get_idempotency(self, key: str) -> dict[str, Any] | None:
        query = self.sa.select(self.idempotency).where(self.idempotency.c.key == key)
        with self.engine.connect() as conn:
            row = conn.execute(query).mappings().one_or_none()
        return dict(row) if row else None

    def save_profile(self, profile: CapabilityProfile) -> None:
        with self.engine.begin() as conn:
            self._upsert(
                conn,
                self.profiles,
                {"actor_id": profile.actor_id},
                {
                    "kind": profile.kind.value,
                    "document": profile.to_dict(),
                    "updated_at": time.time(),
                },
            )

    def get_profile(self, actor_id: str) -> CapabilityProfile | None:
        query = self.sa.select(self.profiles.c.document).where(
            self.profiles.c.actor_id == actor_id
        )
        with self.engine.connect() as conn:
            raw = conn.execute(query).scalar_one_or_none()
        return CapabilityProfile.from_dict(raw) if raw else None

    def list_profiles(self) -> list[CapabilityProfile]:
        with self.engine.connect() as conn:
            rows = conn.execute(
                self.sa.select(self.profiles.c.document).order_by(self.profiles.c.actor_id)
            ).scalars().all()
        return [CapabilityProfile.from_dict(raw) for raw in rows]

    def save_snapshot(self, snapshot: Mapping[str, Any]) -> None:
        payload = dict(snapshot)
        payload["created_at"] = time.time()
        with self.engine.begin() as conn:
            conn.execute(self.snapshots.insert().values(**payload))

    def latest_snapshot(self, run_id: str, task_id: str) -> dict[str, Any] | None:
        query = (
            self.sa.select(self.snapshots)
            .where(
                (self.snapshots.c.run_id == run_id)
                & (self.snapshots.c.task_id == task_id)
            )
            .order_by(self.snapshots.c.sequence.desc())
            .limit(1)
        )
        with self.engine.connect() as conn:
            row = conn.execute(query).mappings().one_or_none()
        return dict(row) if row else None
