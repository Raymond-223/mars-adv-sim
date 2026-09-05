"""Out-of-band trace metadata for fixed-schema business messages."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
import threading
import time


_RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def validate_run_id(value: str) -> str:
    run_id = value.strip()
    if not _RUN_ID_PATTERN.fullmatch(run_id):
        raise ValueError("run_id must match [A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
    return run_id


def trace_id_for(run_id: str, task_id: str | None, message_id: str | None = None) -> str:
    """Return a stable trace ID without adding anything to the wire message."""
    run_id = validate_run_id(run_id)
    anchor = task_id or message_id or "control"
    return hashlib.sha256(f"{run_id}:{anchor}".encode("utf-8")).hexdigest()[:32]


def event_id_for(run_id: str, message_id: str | None) -> str:
    """Return the stable cross-process event ID for one logical message."""
    run_id = validate_run_id(run_id)
    anchor = message_id or "anonymous"
    return hashlib.sha256(f"{run_id}:event:{anchor}".encode("utf-8")).hexdigest()[:32]


def business_content_hash(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class TraceContext:
    message_id: str
    task_id: str | None = None
    parent_message_id: str | None = None
    parent_event_id: str | None = None
    model_id: str | None = None
    schema_version: str = "task-message-v1"
    created_at: float = 0.0
    queued_at: float | None = None
    sent_at: float | None = None
    content_hash: str | None = None
    token_budget: int | None = None


class TraceContextStore:
    """Thread-safe bounded registry joined by message_id in telemetry."""

    def __init__(self, capacity: int = 16_384) -> None:
        if capacity < 1:
            raise ValueError("trace context capacity must be positive")
        self._capacity = capacity
        self._items: dict[str, TraceContext] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()

    def register(
        self,
        message_id: str,
        *,
        task_id: str | None = None,
        parent_message_id: str | None = None,
        parent_event_id: str | None = None,
        model_id: str | None = None,
        schema_version: str = "task-message-v1",
        created_at: float | None = None,
        queued_at: float | None = None,
        content_hash: str | None = None,
        token_budget: int | None = None,
    ) -> TraceContext:
        if not message_id:
            raise ValueError("message_id is required")
        with self._lock:
            previous = self._items.get(message_id)
            context = TraceContext(
                message_id=message_id,
                task_id=task_id if task_id is not None else (previous.task_id if previous else None),
                parent_message_id=(
                    parent_message_id if parent_message_id is not None
                    else (previous.parent_message_id if previous else None)
                ),
                parent_event_id=(
                    parent_event_id if parent_event_id is not None
                    else (previous.parent_event_id if previous else None)
                ),
                model_id=model_id if model_id is not None else (previous.model_id if previous else None),
                schema_version=schema_version or (previous.schema_version if previous else "task-message-v1"),
                created_at=(
                    created_at if created_at is not None
                    else (previous.created_at if previous else time.time())
                ),
                queued_at=queued_at if queued_at is not None else (previous.queued_at if previous else None),
                sent_at=previous.sent_at if previous else None,
                content_hash=(
                    content_hash if content_hash is not None
                    else (previous.content_hash if previous else None)
                ),
                token_budget=(
                    token_budget if token_budget is not None
                    else (previous.token_budget if previous else None)
                ),
            )
            if message_id not in self._items:
                self._order.append(message_id)
            self._items[message_id] = context
            while len(self._order) > self._capacity:
                expired = self._order.pop(0)
                self._items.pop(expired, None)
        return context

    def mark_sent(self, message_id: str, *, sent_at: float | None = None) -> TraceContext | None:
        with self._lock:
            current = self._items.get(message_id)
            if current is None:
                return None
            updated = TraceContext(
                message_id=current.message_id,
                task_id=current.task_id,
                parent_message_id=current.parent_message_id,
                parent_event_id=current.parent_event_id,
                model_id=current.model_id,
                schema_version=current.schema_version,
                created_at=current.created_at,
                queued_at=current.queued_at,
                sent_at=time.time() if sent_at is None else sent_at,
                content_hash=current.content_hash,
                token_budget=current.token_budget,
            )
            self._items[message_id] = updated
            return updated

    def get(self, message_id: str | None) -> TraceContext | None:
        if not message_id:
            return None
        with self._lock:
            return self._items.get(message_id)


    def clear(self) -> None:
        with self._lock:
            self._items.clear()


TRACE_CONTEXTS = TraceContextStore()

