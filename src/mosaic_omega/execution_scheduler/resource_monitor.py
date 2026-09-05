"""Periodic dynamic resource updates for Scheduler decisions."""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from typing import Any

from .capability import CapabilityRegistry


class ResourceMonitor:
    def __init__(
        self,
        registry: CapabilityRegistry,
        provider: Callable[[], Mapping[str, Mapping[str, Any]]],
        *,
        refresh_s: float = 5.0,
    ) -> None:
        self.registry = registry
        self.provider = provider
        self.refresh_s = max(0.1, refresh_s)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def refresh_once(self) -> None:
        for actor_id, values in self.provider().items():
            try:
                self.registry.get(actor_id)
            except KeyError:
                continue
            metadata = {
                key: values[key]
                for key in ("cpu_percent", "gpu_percent", "memory_mb", "queue_length", "model_online")
                if key in values
            }
            online = values.get("online")
            if online is None and "model_online" in values:
                online = bool(values["model_online"])

            load = values.get("current_load")
            if load is None and "cpu_percent" in values:
                # Keep the scheduler's normalized load in [0, 1].  Raw CPU/GPU/
                # memory/queue values remain in metadata for explainability.
                load = max(0.0, min(1.0, float(values["cpu_percent"]) / 100.0))

            self.registry.update_runtime(
                actor_id,
                online=bool(online) if online is not None else None,
                current_load=float(load) if load is not None else None,
                latency_ms=float(values["latency_ms"]) if "latency_ms" in values else None,
                metadata=metadata,
            )

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()

        def loop() -> None:
            while not self._stop.wait(self.refresh_s):
                self.refresh_once()

        self._thread = threading.Thread(target=loop, name="resource-monitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=self.refresh_s + 1.0)
            self._thread = None
