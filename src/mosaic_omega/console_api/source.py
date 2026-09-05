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

    def snapshot(self, run_id: str | None = None) -> dict[str, Any] | None:
        if run_id:
            return self._read(self.runs_dir / f"{self._safe_run_name(run_id)}.json")
        return self._read(self.directory / "latest.json")

    def section(self, name: str, run_id: str | None = None) -> Any:
        if name not in self.SECTIONS:
            raise KeyError(name)
        snapshot = self.snapshot(run_id)
        if snapshot is None:
            return None
        return snapshot.get(name)

    def runs(self) -> list[dict[str, Any]]:
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
