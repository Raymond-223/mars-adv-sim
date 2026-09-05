"""Observability facade attached to the authoritative main chain."""
from __future__ import annotations

from pathlib import Path
import time
from typing import Any, Mapping, Sequence

from .logging import JsonEventLogger
from .metrics import MetricRegistry
from .projections import build_dashboard_snapshot
from .snapshots import SnapshotStore


class ObservabilityRuntime:
    """Read-only projection runtime.

    It never changes a Task, Event, Assignment, MemoryRecord or VerificationResult.
    All snapshots can be regenerated from the core services.
    """

    def __init__(self, directory: str | Path, *, min_snapshot_interval_s: float = 0.15) -> None:
        self.directory = Path(directory)
        self.min_snapshot_interval_s = max(0.0, float(min_snapshot_interval_s))
        self._last_snapshot_at = 0.0
        self.snapshots = SnapshotStore(self.directory)
        self.metrics = MetricRegistry()
        self.logger = JsonEventLogger(self.directory / "logs" / "events.jsonl")
        self._seen_event_ids: set[str] = set()
        self._active_run_id: str | None = None

    def should_capture(self, *, force: bool = False) -> bool:
        if force:
            return True
        return time.monotonic() - self._last_snapshot_at >= self.min_snapshot_interval_s

    @staticmethod
    def _event_dict(event: Any) -> dict[str, Any]:
        if hasattr(event, "to_dict"):
            return dict(event.to_dict())
        return dict(event)

    def _observe_new_events(self, events: Sequence[Any], *, phase: str) -> None:
        for raw in events:
            event = self._event_dict(raw)
            event_id = str(event.get("event_id", ""))
            if event_id and event_id in self._seen_event_ids:
                continue
            if event_id:
                self._seen_event_ids.add(event_id)
            event_type = str(event.get("type", event.get("event_type", "UNKNOWN")))
            self.metrics.inc(f"events.{event_type}")
            payload = event.get("payload")
            if event_type == "TOOL_EXECUTED" and isinstance(payload, Mapping):
                result = payload.get("result")
                if isinstance(result, Mapping):
                    start = result.get("started_at")
                    end = result.get("finished_at")
                    if isinstance(start, (int, float)) and isinstance(end, (int, float)):
                        self.metrics.observe("tool_execution_latency_ms", max(0.0, float(end) - float(start)) * 1000)
            self.logger.write_event(event, phase=phase)

    def capture(
        self,
        *,
        run_id: str,
        phase: str,
        tasks: Sequence[Any],
        events: Sequence[Any],
        capabilities: Sequence[Any],
        communication: Sequence[Mapping[str, Any]],
        communication_decisions: Sequence[Mapping[str, Any]],
        context_packs: Mapping[str, Mapping[str, Any]],
        memory_records: Sequence[Any],
        memory_metrics: Mapping[str, Any],
        topology_snapshot: Mapping[str, Any],
        topology_telemetry: Mapping[str, Any],
    ) -> dict[str, Any]:
        if self._active_run_id != run_id:
            self._active_run_id = run_id
            self.metrics = MetricRegistry()
            self._seen_event_ids.clear()
        self._observe_new_events(events, phase=phase)
        self.metrics.set_label("run_id", run_id)
        self.metrics.set_label("phase", phase)
        self.metrics.set_gauge("tasks.total", len(tasks))
        self.metrics.set_gauge("messages.total", len(communication))
        self.metrics.set_gauge("memory.context_packs", len(context_packs))
        self.metrics.merge_gauges("memory.", memory_metrics)
        snapshot = build_dashboard_snapshot(
            run_id=run_id,
            phase=phase,
            tasks=tasks,
            events=events,
            capabilities=capabilities,
            communication=communication,
            communication_decisions=communication_decisions,
            context_packs=context_packs,
            memory_records=memory_records,
            memory_metrics=memory_metrics,
            topology_snapshot=topology_snapshot,
            topology_telemetry=topology_telemetry,
            metric_snapshot=self.metrics.snapshot(),
        )
        self.snapshots.write(run_id, snapshot)
        self._last_snapshot_at = time.monotonic()
        return snapshot
