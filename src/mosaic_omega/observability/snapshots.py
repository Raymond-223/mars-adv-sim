"""Atomic read-only dashboard snapshot persistence."""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Mapping


class SnapshotStore:
    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)
        self.runs_dir = self.directory / "runs"
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    @property
    def latest_path(self) -> Path:
        return self.directory / "latest.json"

    def run_path(self, run_id: str) -> Path:
        safe = "".join(ch for ch in run_id if ch.isalnum() or ch in "-_.") or "run"
        return self.runs_dir / f"{safe}.json"

    @staticmethod
    def _atomic_write(path: Path, data: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        # On Windows, os.replace() can fail with PermissionError when the
        # target file is being read by another thread (e.g. the HTTP server).
        # Retry a few times with a short backoff.
        for attempt in range(5):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == 4:
                    # Last resort: just overwrite directly
                    try:
                        path.write_text(tmp.read_text(encoding="utf-8"), encoding="utf-8")
                        tmp.unlink(missing_ok=True)
                    except OSError:
                        pass
                    return
                import time
                time.sleep(0.05 * (attempt + 1))

    def write(self, run_id: str, snapshot: Mapping[str, Any]) -> Path:
        with self._lock:
            path = self.run_path(run_id)
            self._atomic_write(path, snapshot)
            self._atomic_write(self.latest_path, snapshot)
            return path

    def read_latest(self) -> dict[str, Any] | None:
        with self._lock:
            if not self.latest_path.exists():
                return None
            return json.loads(self.latest_path.read_text(encoding="utf-8"))

    def read_run(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            path = self.run_path(run_id)
            if not path.exists():
                return None
            return json.loads(path.read_text(encoding="utf-8"))

    def list_runs(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for path in sorted(self.runs_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            run = raw.get("run", {}) if isinstance(raw, dict) else {}
            items.append({
                "run_id": run.get("run_id", path.stem),
                "status": run.get("status", "UNKNOWN"),
                "generated_at": raw.get("generated_at"),
                "phase": raw.get("phase"),
                "path": str(path),
            })
        return items
