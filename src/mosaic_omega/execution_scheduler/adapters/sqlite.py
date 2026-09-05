"""Stdlib SQLite durable adapter for the desktop/product runtime.

The competition desktop build should not require a PostgreSQL service merely to
obtain crash-resilient EventStore/task/idempotency state. SQLite is therefore the
product default. PostgreSQL remains available for server deployment.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Mapping

from ..models import CapabilityProfile, Event, TaskNodeView, TaskState


class SQLiteDatabase:
    def __init__(self, path: str | Path, *, synchronous: str = "NORMAL") -> None:
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        mode = str(synchronous or "NORMAL").strip().upper()
        if mode not in {"OFF", "NORMAL", "FULL", "EXTRA"}:
            raise ValueError("SQLite synchronous must be OFF, NORMAL, FULL, or EXTRA")
        self.synchronous = mode
        # Keep one process-local connection instead of reopening SQLite and
        # renegotiating WAL pragmas for every EventStore append/read.  Access is
        # serialized by ``_lock`` and check_same_thread=False is required because
        # independent READY DAG nodes may execute on worker threads.
        self._conn: sqlite3.Connection | None = None

    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            conn = sqlite3.connect(self.path, timeout=30.0, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            # WAL + NORMAL keeps committed database pages consistent while avoiding
            # a full fsync on every individual append.  FULL/EXTRA remain available
            # for deployments that explicitly prefer maximum power-loss durability.
            conn.execute(f"PRAGMA synchronous={self.synchronous}")
            conn.execute("PRAGMA foreign_keys=ON")
            self._conn = conn
        return self._conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def initialize(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS execution_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    run_id TEXT NOT NULL,
                    node_id TEXT,
                    trace_id TEXT,
                    parent_event_id TEXT,
                    type TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    model_id TEXT,
                    timestamp REAL NOT NULL,
                    schema_version TEXT NOT NULL,
                    document TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_events_run ON execution_events(run_id, sequence);
                CREATE INDEX IF NOT EXISTS idx_events_task ON execution_events(run_id, node_id, sequence);
                CREATE INDEX IF NOT EXISTS idx_events_trace ON execution_events(trace_id, sequence);

                CREATE TABLE IF NOT EXISTS task_node_views (
                    run_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    document TEXT NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(run_id, task_id)
                );
                CREATE TABLE IF NOT EXISTS execution_outbox (
                    outbox_id TEXT PRIMARY KEY,
                    event_id TEXT NOT NULL UNIQUE,
                    topic TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    published_at REAL
                );
                CREATE TABLE IF NOT EXISTS execution_idempotency (
                    key TEXT PRIMARY KEY,
                    fingerprint TEXT,
                    status TEXT NOT NULL,
                    result TEXT,
                    error TEXT,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS capability_profiles (
                    actor_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    document TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS execution_snapshots (
                    snapshot_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    document TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_snapshots_task ON execution_snapshots(run_id, task_id, sequence);
                """
            )

    @staticmethod
    def _dump(value: Mapping[str, Any] | dict[str, Any]) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)

    @staticmethod
    def _load(value: str | None) -> dict[str, Any] | None:
        return json.loads(value) if value else None

    def append_bundle(self, event: Event, view: TaskNodeView | None, outbox_topic: str | None) -> Event:
        with self._lock, self._connect() as conn:
            stored = event.to_dict()
            cursor = conn.execute(
                """INSERT INTO execution_events
                (event_id,run_id,node_id,trace_id,parent_event_id,type,actor_id,model_id,timestamp,schema_version,document)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    event.event_id, event.run_id, event.task_id, event.trace_id, event.parent_event_id,
                    event.event_type, event.actor_id, event.model_id, event.occurred_at, event.schema_version,
                    self._dump(stored),
                ),
            )
            sequence = int(cursor.lastrowid)
            stored["sequence"] = sequence
            # Store the assigned sequence in the immutable event document too.
            conn.execute("UPDATE execution_events SET document=? WHERE sequence=?", (self._dump(stored), sequence))
            if view is not None:
                doc = view.to_dict()
                conn.execute(
                    """INSERT INTO task_node_views(run_id,task_id,state,version,document,updated_at)
                    VALUES(?,?,?,?,?,?)
                    ON CONFLICT(run_id,task_id) DO UPDATE SET
                    state=excluded.state,version=excluded.version,document=excluded.document,updated_at=excluded.updated_at""",
                    (view.run_id, view.task_id, view.state.value, view.version, self._dump(doc), time.time()),
                )
            if outbox_topic:
                outbox_id = f"out_{uuid.uuid4().hex}"
                conn.execute(
                    "INSERT INTO execution_outbox(outbox_id,event_id,topic,payload,created_at,published_at) VALUES(?,?,?,?,?,NULL)",
                    (outbox_id, event.event_id, outbox_topic, self._dump(stored), time.time()),
                )
            conn.commit()
        return Event.from_dict(stored)

    def last_event_id(self, run_id: str, task_id: str | None = None) -> str | None:
        with self._lock, self._connect() as conn:
            if task_id is None:
                row = conn.execute(
                    "SELECT event_id FROM execution_events WHERE run_id=? ORDER BY sequence DESC LIMIT 1", (run_id,)
                ).fetchone()
            else:
                row = conn.execute(
                    """SELECT event_id FROM execution_events
                    WHERE run_id=? AND (node_id=? OR node_id IS NULL)
                    ORDER BY CASE WHEN node_id=? THEN 0 ELSE 1 END, sequence DESC LIMIT 1""",
                    (run_id, task_id, task_id),
                ).fetchone()
            return str(row[0]) if row else None

    def list_events(self, *, run_id: str | None = None, task_id: str | None = None, trace_id: str | None = None) -> list[Event]:
        clauses: list[str] = []
        args: list[Any] = []
        if run_id is not None:
            clauses.append("run_id=?"); args.append(run_id)
        if task_id is not None:
            clauses.append("node_id=?"); args.append(task_id)
        if trace_id is not None:
            clauses.append("trace_id=?"); args.append(trace_id)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT document FROM execution_events" + where + " ORDER BY sequence", args).fetchall()
        return [Event.from_dict(json.loads(row[0])) for row in rows]

    def get_task(self, run_id: str, task_id: str) -> TaskNodeView | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT document FROM task_node_views WHERE run_id=? AND task_id=?", (run_id, task_id)).fetchone()
        return TaskNodeView.from_dict(json.loads(row[0])) if row else None

    def list_tasks(self, run_id: str, state: TaskState | None = None) -> list[TaskNodeView]:
        sql = "SELECT document FROM task_node_views WHERE run_id=?"
        args: list[Any] = [run_id]
        if state is not None:
            sql += " AND state=?"; args.append(state.value)
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, args).fetchall()
        tasks = [TaskNodeView.from_dict(json.loads(row[0])) for row in rows]
        return sorted(tasks, key=lambda task: (-task.priority, task.task_id))

    def pending_outbox(self, limit: int) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT outbox_id,event_id,topic,payload,created_at,published_at FROM execution_outbox WHERE published_at IS NULL ORDER BY created_at LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [
            {"outbox_id": r[0], "event_id": r[1], "topic": r[2], "payload": json.loads(r[3]), "created_at": r[4], "published_at": r[5]}
            for r in rows
        ]

    def mark_outbox_published(self, outbox_id: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("UPDATE execution_outbox SET published_at=? WHERE outbox_id=?", (time.time(), outbox_id)); conn.commit()

    def begin_idempotency(self, key: str, *, fingerprint: str | None = None) -> tuple[bool, dict[str, Any] | None]:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT key,fingerprint,status,result,error,updated_at FROM execution_idempotency WHERE key=?", (key,)).fetchone()
            if row:
                return False, {
                    "key": row[0], "fingerprint": row[1], "status": row[2],
                    "result": self._load(row[3]), "error": row[4], "updated_at": row[5],
                }
            conn.execute(
                "INSERT INTO execution_idempotency(key,fingerprint,status,result,error,updated_at) VALUES(?,?, 'RUNNING', NULL, NULL, ?)",
                (key, fingerprint, time.time()),
            ); conn.commit()
            return True, None

    def finish_idempotency(self, key: str, *, result: Mapping[str, Any] | None, error: str | None) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE execution_idempotency SET status=?,result=?,error=?,updated_at=? WHERE key=?",
                ("FAILED" if error else "SUCCEEDED", self._dump(dict(result)) if result is not None else None, error, time.time(), key),
            ); conn.commit()

    def get_idempotency(self, key: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT key,fingerprint,status,result,error,updated_at FROM execution_idempotency WHERE key=?", (key,)).fetchone()
        if not row:
            return None
        return {"key": row[0], "fingerprint": row[1], "status": row[2], "result": self._load(row[3]), "error": row[4], "updated_at": row[5]}

    def save_profile(self, profile: CapabilityProfile) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """INSERT INTO capability_profiles(actor_id,kind,document,updated_at) VALUES(?,?,?,?)
                ON CONFLICT(actor_id) DO UPDATE SET kind=excluded.kind,document=excluded.document,updated_at=excluded.updated_at""",
                (profile.actor_id, profile.kind.value, self._dump(profile.to_dict()), time.time()),
            ); conn.commit()

    def get_profile(self, actor_id: str) -> CapabilityProfile | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT document FROM capability_profiles WHERE actor_id=?", (actor_id,)).fetchone()
        return CapabilityProfile.from_dict(json.loads(row[0])) if row else None

    def list_profiles(self) -> list[CapabilityProfile]:
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT document FROM capability_profiles ORDER BY actor_id").fetchall()
        return [CapabilityProfile.from_dict(json.loads(row[0])) for row in rows]

    def save_snapshot(self, snapshot: Mapping[str, Any]) -> None:
        raw = dict(snapshot)
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO execution_snapshots(snapshot_id,run_id,task_id,sequence,document,created_at) VALUES(?,?,?,?,?,?)",
                (
                    str(raw.get("snapshot_id") or f"snp_{uuid.uuid4().hex}"), str(raw["run_id"]), str(raw["task_id"]),
                    int(raw.get("sequence", 0)), self._dump(raw), time.time(),
                ),
            ); conn.commit()

    def latest_snapshot(self, run_id: str, task_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT document FROM execution_snapshots WHERE run_id=? AND task_id=? ORDER BY sequence DESC, created_at DESC LIMIT 1",
                (run_id, task_id),
            ).fetchone()
        return json.loads(row[0]) if row else None
