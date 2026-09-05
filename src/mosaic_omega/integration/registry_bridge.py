"""Bridge dynamic-registry AgentProfile/status into scheduler CapabilityProfile.

Every registered execution actor carries explicit adapter provenance.  A UI may
therefore distinguish real API / remote RPC / deterministic executor / mock /
unbound actors without guessing from an actor name.
"""
from __future__ import annotations

import time
from typing import Any, Sequence

from ..execution_scheduler.models import CapabilityProfile
from ..execution_scheduler.service import ExecutionSchedulerService
from ..agent_runtime.models import AgentProfile, AgentStatus
from .contracts import runtime_agent_to_capability


def adapter_provenance(adapter: Any | None) -> dict[str, Any]:
    if adapter is None:
        return {
            "adapter_bound": False,
            "adapter_class": None,
            "authenticity_mode": "unbound",
        }
    cls = adapter.__class__
    mode = getattr(adapter, "authenticity_mode", "unclassified")
    return {
        "adapter_bound": True,
        "adapter_class": f"{cls.__module__}.{cls.__qualname__}",
        "authenticity_mode": str(mode),
        "api_transport": getattr(adapter, "api_transport", getattr(adapter, "_transport", None)),
        "endpoint_host": getattr(adapter, "endpoint_host", None),
        "official_endpoint_verified": bool(getattr(adapter, "official_endpoint_verified", False)),
    }


class DynamicRegistrySchedulerBridge:
    def __init__(self, execution: ExecutionSchedulerService) -> None:
        self.execution = execution

    def register(
        self,
        profile: AgentProfile,
        *,
        status: AgentStatus | str = AgentStatus.ONLINE,
        current_load: int = 0,
        permissions: Sequence[str] = ("*",),
        latency_ms: float | None = None,
        adapter: Any | None = None,
        fixed_cost: float = 0.0,
    ) -> CapabilityProfile:
        now = time.time()
        capability = runtime_agent_to_capability(
            profile,
            status=status,
            current_load=current_load,
            permissions=permissions,
            latency_ms=latency_ms,
        )
        capability.fixed_cost = float(fixed_cost)
        capability.metadata.update(adapter_provenance(adapter))
        capability.metadata.update({
            "registered_at": now,
            "status_updated_at": now,
            "status_source": "registry.register",
            "reliability_source": "configured_prior",
            # A value supplied at registration is configuration, not a measurement.
            # Only heartbeat/runtime telemetry is allowed to increment measurement_count.
            "latency_source": "configured_value" if latency_ms is not None else "unmeasured",
            "latency_measurement_count": 0,
        })
        return self.execution.register_actor(capability, adapter=adapter)

    def heartbeat(
        self,
        actor_id: str,
        *,
        status: AgentStatus | str = AgentStatus.ONLINE,
        current_load: int | None = None,
        latency_ms: float | None = None,
    ) -> CapabilityProfile:
        state = AgentStatus(status)
        profile = self.execution.capabilities.get(actor_id)
        capacity = max(1, profile.capacity)
        normalized_load = None
        if current_load is not None:
            normalized_load = min(1.0, max(0.0, float(current_load) / capacity))
        metadata: dict[str, Any] = {
            "status_updated_at": time.time(),
            "status_source": "registry.heartbeat",
        }
        if latency_ms is not None:
            metadata["latency_source"] = "heartbeat_measurement"
            metadata["latency_measurement_count"] = int(
                profile.metadata.get("latency_measurement_count", 0) or 0
            ) + 1
        return self.execution.capabilities.update_runtime(
            actor_id,
            online=state is AgentStatus.ONLINE,
            current_load=normalized_load,
            latency_ms=latency_ms,
            metadata=metadata,
        )

    def offline(self, actor_id: str) -> CapabilityProfile:
        return self.execution.capabilities.update_runtime(
            actor_id,
            online=False,
            metadata={
                "status_updated_at": time.time(),
                "status_source": "registry.offline",
            },
        )
