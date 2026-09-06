"""Read-only data source for the web console."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class ConsoleDataSource:
    """Read dashboard projections without importing runtime mutation services.

    This class intentionally does not instantiate ``SnapshotStore`` because that
    class owns write-side atomic persistence.  The console can therefore mount
    the observability directory read-only in production.
    """

    SECTIONS = {
        "run",
        "task_graph",
        "tasks",
        "topology",
        "communication",
        "scheduler",
        "scheduling",
        "performance",
        "memory",
        "recovery",
        "evidence",
        "events",
        "traces",
        "metrics",
    }

    def __init__(self, snapshot_dir: str | Path) -> None:
        self.directory = Path(snapshot_dir)
        self.runs_dir = self.directory / "runs"

    @staticmethod
    def _safe_run_name(run_id: str) -> str:
        return "".join(ch for ch in run_id if ch.isalnum() or ch in "-_.") or "run"

    @staticmethod
    def _read(path: Path) -> dict[str, Any] | None:
        if not path.is_file():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return raw if isinstance(raw, dict) else None

    def _latest_pointer(self) -> dict[str, Any] | None:
        return self._read(self.directory / "latest.json")

    def snapshot(self, run_id: str | None = None) -> dict[str, Any] | None:
        if run_id:
            return self._read(self.runs_dir / f"{self._safe_run_name(run_id)}.json")
        pointer = self._latest_pointer()
        if pointer is None:
            return None
        # ``latest.json`` is a small pointer in the current store; older stores
        # wrote the whole snapshot there and must still be readable.
        if pointer.get("schema_version") != "mosaic-console-latest-v2":
            return pointer
        target = str(pointer.get("run_id") or "")
        return self._read(self.runs_dir / f"{self._safe_run_name(target)}.json") if target else None

    def freshness(self, run_id: str | None = None) -> dict[str, Any] | None:
        """Cheap change signal for streaming: never parses a full snapshot.

        The SSE loop only needs to know *that* runtime state changed. Reading the
        multi-megabyte run snapshot twice a second just to compare one timestamp
        was pure overhead on every active run.
        """
        if run_id:
            path = self.runs_dir / f"{self._safe_run_name(run_id)}.json"
            if not path.is_file():
                return None
            stat_result = path.stat()
            return {
                "run_id": run_id,
                "generated_at": stat_result.st_mtime,
                "size_bytes": stat_result.st_size,
            }
        pointer = self._latest_pointer()
        if pointer is None:
            return None
        run = pointer.get("run", {}) if isinstance(pointer.get("run"), dict) else {}
        return {
            "run_id": pointer.get("run_id") or run.get("run_id"),
            "generated_at": pointer.get("generated_at"),
            "phase": pointer.get("phase"),
            "status": run.get("status"),
        }

    def section(self, name: str, run_id: str | None = None) -> Any:
        if name not in self.SECTIONS:
            raise KeyError(name)
        snapshot = self.snapshot(run_id)
        if snapshot is None:
            return None
        return snapshot.get(name)

    def runs(self) -> list[dict[str, Any]]:
        # Prefer the compact index: listing runs used to deserialize every full
        # snapshot on the disk just to read four header fields, which grew with
        # both run count and run size.
        index = self._read(self.directory / "index.json")
        if isinstance(index, dict) and index:
            rows = [dict(value) for value in index.values() if isinstance(value, dict)]
            rows.sort(key=lambda item: float(item.get("generated_at") or 0.0), reverse=True)
            return [{k: v for k, v in row.items() if k != "file"} for row in rows]
        if not self.runs_dir.is_dir():
            return []
        items: list[dict[str, Any]] = []
        for path in sorted(self.runs_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            raw = self._read(path)
            if raw is None:
                continue
            run = raw.get("run", {}) if isinstance(raw.get("run"), dict) else {}
            items.append({
                "run_id": run.get("run_id", path.stem),
                "status": run.get("status", "UNKNOWN"),
                "generated_at": raw.get("generated_at"),
                "phase": raw.get("phase"),
            })
        return items

    def events(
        self,
        *,
        run_id: str | None = None,
        event_type: str | None = None,
        task_id: str | None = None,
        trace_id: str | None = None,
    ) -> list[dict[str, Any]]:
        items = self.section("events", run_id) or []
        result: list[dict[str, Any]] = []
        for event in items:
            if event_type and str(event.get("type", event.get("event_type", ""))) != event_type:
                continue
            node = str(event.get("node_id", event.get("task_id", "")))
            if task_id and node != task_id:
                continue
            if trace_id and str(event.get("trace_id", "")) != trace_id:
                continue
            result.append(dict(event))
        return result
