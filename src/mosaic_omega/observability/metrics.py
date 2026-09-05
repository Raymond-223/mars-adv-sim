"""Thread-safe runtime metric projection for MOSAIC-Ω observability.

The registry is deliberately *not* a second source of truth.  Counters, gauges and
histograms are derived from authoritative events and module telemetry and may be
recreated at any time.
"""
from __future__ import annotations

import math
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Mapping


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * fraction) - 1))
    return round(ordered[index], 6)


@dataclass
class MetricRegistry:
    """Small dependency-free metrics registry used by the console and reports."""

    counters: dict[str, float] = field(default_factory=lambda: defaultdict(float))
    gauges: dict[str, float] = field(default_factory=dict)
    histograms: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))
    labels: dict[str, str] = field(default_factory=dict)
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def inc(self, name: str, value: float = 1.0) -> None:
        with self._lock:
            self.counters[name] += float(value)

    def set_gauge(self, name: str, value: float) -> None:
        with self._lock:
            self.gauges[name] = float(value)

    def observe(self, name: str, value: float) -> None:
        if value < 0:
            return
        with self._lock:
            self.histograms[name].append(float(value))

    def set_label(self, name: str, value: str) -> None:
        with self._lock:
            self.labels[name] = str(value)

    def merge_gauges(self, prefix: str, values: Mapping[str, Any]) -> None:
        for name, value in values.items():
            if isinstance(value, bool):
                self.set_gauge(f"{prefix}{name}", float(value))
            elif isinstance(value, (int, float)):
                self.set_gauge(f"{prefix}{name}", float(value))

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            histograms = {
                name: {
                    "count": len(values),
                    "mean": round(sum(values) / len(values), 6) if values else 0.0,
                    "p50": _percentile(values, 0.50),
                    "p95": _percentile(values, 0.95),
                    "p99": _percentile(values, 0.99),
                }
                for name, values in self.histograms.items()
            }
            return {
                "counters": dict(self.counters),
                "gauges": dict(self.gauges),
                "histograms": histograms,
                "labels": dict(self.labels),
            }
