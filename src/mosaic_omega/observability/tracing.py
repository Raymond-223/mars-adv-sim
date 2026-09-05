"""Trace helpers for read-only console projections."""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable, Mapping


def group_events_by_trace(events: Iterable[Mapping[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        trace_id = str(event.get("trace_id") or "untraced")
        grouped[trace_id].append(dict(event))
    for items in grouped.values():
        items.sort(key=lambda item: float(item.get("timestamp") or 0.0))
    return dict(grouped)


def trace_summary(events: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for trace_id, items in group_events_by_trace(events).items():
        timestamps = [float(item.get("timestamp") or 0.0) for item in items]
        tasks = sorted({str(item.get("node_id")) for item in items if item.get("node_id")})
        summaries.append({
            "trace_id": trace_id,
            "event_count": len(items),
            "task_ids": tasks,
            "started_at": min(timestamps) if timestamps else None,
            "finished_at": max(timestamps) if timestamps else None,
            "duration_ms": round((max(timestamps) - min(timestamps)) * 1000, 6) if len(timestamps) >= 2 else 0.0,
        })
    summaries.sort(key=lambda item: float(item.get("started_at") or 0.0))
    return summaries
