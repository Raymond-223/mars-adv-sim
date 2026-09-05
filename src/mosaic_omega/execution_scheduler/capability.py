"""Persistent actor capability profiles and runtime status."""

from __future__ import annotations

from copy import deepcopy

from .models import ActorKind, CapabilityProfile


class CapabilityRegistry:
    def __init__(self, database: object) -> None:
        self.database = database

    @staticmethod
    def _normalize(profile: CapabilityProfile) -> CapabilityProfile:
        if not profile.actor_id or not profile.task_types:
            raise ValueError("actor_id and task_types are required")
        profile.reliability = min(1.0, max(0.0, float(profile.reliability)))
        profile.current_load = min(1.0, max(0.0, float(profile.current_load)))
        profile.fixed_cost = max(0.0, float(profile.fixed_cost))
        profile.cost_per_token = max(0.0, float(profile.cost_per_token))
        profile.latency_ms = max(0.0, float(profile.latency_ms))
        profile.energy_cost = max(0.0, float(profile.energy_cost))
        profile.context_limit = max(0, int(profile.context_limit))
        profile.capacity = max(1, int(profile.capacity))
        return profile

    def register(self, profile: CapabilityProfile) -> CapabilityProfile:
        profile = self._normalize(profile)
        self.database.save_profile(profile)
        return deepcopy(profile)

    def get(self, actor_id: str) -> CapabilityProfile:
        profile = self.database.get_profile(actor_id)
        if profile is None:
            raise KeyError(actor_id)
        return profile

    def list(self, kind: ActorKind | None = None) -> list[CapabilityProfile]:
        profiles = self.database.list_profiles()
        return [item for item in profiles if kind is None or item.kind is kind]

    def update_runtime(
        self,
        actor_id: str,
        *,
        online: bool | None = None,
        current_load: float | None = None,
        latency_ms: float | None = None,
        metadata: dict | None = None,
    ) -> CapabilityProfile:
        profile = self.get(actor_id)
        if online is not None:
            profile.online = bool(online)
        if current_load is not None:
            profile.current_load = min(1.0, max(0.0, float(current_load)))
        if latency_ms is not None:
            profile.latency_ms = max(0.0, float(latency_ms))
        if metadata:
            profile.metadata.update(deepcopy(metadata))
        return self.save(profile)

    def save(self, profile: CapabilityProfile) -> CapabilityProfile:
        profile = self._normalize(profile)
        self.database.save_profile(profile)
        return deepcopy(profile)
