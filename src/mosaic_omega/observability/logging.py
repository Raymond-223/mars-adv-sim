"""Structured JSONL logging projected from authoritative execution events."""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Mapping


class JsonEventLogger:
    """Append-only JSONL sink.

    The event store remains authoritative.  This file is only an operator/debugging
    projection and can be deleted/rebuilt without changing a run.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    @staticmethod
    def _latency_ms(event: Mapping[str, Any]) -> float | None:
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            return None
        result = payload.get("result")
        if isinstance(result, Mapping):
            started = result.get("started_at")
            finished = result.get("finished_at")
            if isinstance(started, (int, float)) and isinstance(finished, (int, float)):
                return round(max(0.0, float(finished) - float(started)) * 1000, 6)
        return None

    @staticmethod
    def _error_code(event: Mapping[str, Any]) -> str | None:
        payload = event.get("payload")
        if isinstance(payload, Mapping):
            value = payload.get("error_class")
            if value:
                return str(value)
            result = payload.get("result")
            if isinstance(result, Mapping) and result.get("error_class"):
                return str(result["error_class"])
        return None

    @staticmethod
    def _service(event: Mapping[str, Any]) -> str:
        actor = str(event.get("actor_id") or "system")
        mapping = {
            "goal-planner": "goal-planner",
            "scheduler": "scheduler",
            "orchestrator": "orchestrator",
            "verifier": "verifier",
            "recovery": "memory-recovery",
            "capability": "scheduler",
        }
        return mapping.get(actor, actor)

    def write_event(self, event: Mapping[str, Any], *, phase: str = "") -> None:
        record = {
            "timestamp": event.get("timestamp"),
            "level": "ERROR" if self._error_code(event) else "INFO",
            "service": self._service(event),
            "trace_id": event.get("trace_id"),
            "run_id": event.get("run_id"),
            "task_id": event.get("task_id") or event.get("node_id"),
            "node_id": event.get("node_id") or event.get("task_id"),
            "actor_id": event.get("actor_id"),
            "model_id": event.get("model_id"),
            "event_type": event.get("type") or event.get("event_type"),
            "latency_ms": self._latency_ms(event),
            "error_code": self._error_code(event),
            "phase": phase,
            "event_id": event.get("event_id"),
            "parent_event_id": event.get("parent_event_id"),
            "schema_version": event.get("schema_version"),
            "payload": event.get("payload", {}),
        }
        line = json.dumps(record, ensure_ascii=False, sort_keys=True, default=str)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
