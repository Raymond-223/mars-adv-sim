"""Explainable scheduling costs with hard privacy/permission/location filters."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..agent_runtime.edge_cloud import (
    EdgeCloudPlacementEngine,
    ExecutionTier,
    ModelPartitionDescriptor,
    ModelPartitionPolicy,
)
from .config import Settings
from .models import ActorKind, CapabilityProfile, TaskNodeView


@dataclass(frozen=True)
class CostEvaluation:
    eligible: bool
    total: float
    breakdown: dict[str, float] = field(default_factory=dict)
    reasons: tuple[str, ...] = ()
    tier: ExecutionTier = ExecutionTier.EDGE
    partition_policy: ModelPartitionPolicy = ModelPartitionPolicy.NONE
    partition_descriptor: ModelPartitionDescriptor = field(default_factory=ModelPartitionDescriptor)


class CostModel:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @staticmethod
    def _posterior_reliability(profile: CapabilityProfile, task_type: str) -> float:
        sample = profile.posterior.get(task_type)
        if sample:
            alpha = float(sample.get("alpha", 1.0))
            beta = float(sample.get("beta", 1.0))
            if alpha + beta > 0:
                return min(1.0, max(0.0, alpha / (alpha + beta)))
        return min(1.0, max(0.0, profile.reliability))

    @staticmethod
    def hard_filter(
        task: TaskNodeView,
        profile: CapabilityProfile,
        *,
        enforce_permissions: bool = True,
        enforce_location: bool = False,
    ) -> tuple[bool, str | None]:
        if not profile.supports(task):
            return False, f"{profile.actor_id} lacks task capability or is offline"
        if (
            enforce_permissions
            and "*" not in profile.permissions
            and not task.required_permissions.issubset(profile.permissions)
        ):
            return False, f"{profile.actor_id} lacks required permissions"
        if profile.context_limit and task.estimated_tokens > profile.context_limit:
            return False, f"{profile.actor_id} context limit is too small"
        if task.max_latency_ms is not None and profile.latency_ms > task.max_latency_ms:
            return False, f"{profile.actor_id} exceeds hard latency limit"

        allowed_privacy = (profile.metadata or {}).get("allowed_privacy_levels")
        if allowed_privacy and task.privacy_level not in set(allowed_privacy):
            return False, f"{profile.actor_id} rejects privacy level {task.privacy_level}"


        if enforce_location and task.data_location:
            if profile.device_location != task.data_location:
                return False, f"{profile.actor_id} violates required data location"
        return True, None

    def evaluate(
        self,
        task: TaskNodeView,
        agent: CapabilityProfile,
        model: CapabilityProfile,
        tool: CapabilityProfile,
        device: CapabilityProfile,
        *,
        previous_resource_id: str | None = None,
    ) -> CostEvaluation:
        reasons: list[str] = []
        checks = (
            (agent, True, False),
            (model, False, False),
            (tool, True, False),
            (device, False, bool(task.data_location)),
        )
        for profile, permissions, location in checks:
            eligible, reason = self.hard_filter(
                task,
                profile,
                enforce_permissions=permissions,
                enforce_location=location,
            )
            if not eligible:
                reasons.append(reason or "hard filter rejected candidate")

        # Evaluate recommended Edge-Cloud Tier and model partitioning.  An explicit
        # task execution_tier is a hard placement constraint; otherwise this is
        # an explainable recommendation that can be realized by the best
        # available heterogeneous Agent.
        recommended_tier, partition_desc = EdgeCloudPlacementEngine.select_placement(
            privacy_level=task.privacy_level,
            max_latency_ms=task.max_latency_ms,
            estimated_tokens=task.estimated_tokens,
            device_gpu=bool((device.metadata or {}).get("gpu_count", 0) > 0),
        )
        requested_tier_raw = (task.metadata or {}).get("execution_tier")
        if requested_tier_raw:
            try:
                requested_tier = ExecutionTier(str(requested_tier_raw).casefold())
            except ValueError:
                reasons.append(f"unknown execution_tier {requested_tier_raw}")
            else:
                recommended_tier = requested_tier
                agent_tier = str((agent.metadata or {}).get("tier", agent.device_location or "")).casefold()
                if agent_tier and agent_tier != requested_tier.value:
                    reasons.append(
                        f"{agent.actor_id} tier={agent_tier} violates required execution_tier={requested_tier.value}"
                    )

        # Sensitive tasks must run at the declared location. If no location was
        # provided, fail closed rather than guessing an execution tier.
        if task.privacy_level.casefold() in {"restricted", "secret"}:
            if not task.data_location:
                reasons.append("restricted/secret task requires data_location")
            elif device.device_location != task.data_location:
                reasons.append(f"{device.actor_id} violates privacy/data-location policy")

        if reasons:
            return CostEvaluation(False, float("inf"), reasons=tuple(dict.fromkeys(reasons)))


        profiles = (agent, model, tool, device)
        fixed = sum(item.fixed_cost for item in profiles)
        latency_seconds = max(item.latency_ms for item in profiles) / 1000.0
        latency = latency_seconds * self.settings.weight_latency
        token = (
            task.estimated_tokens
            * sum(item.cost_per_token for item in profiles)
            * self.settings.weight_token
        )
        energy = sum(item.energy_cost for item in profiles) * self.settings.weight_energy
        reliability = sum(
            self._posterior_reliability(item, task.task_type) for item in profiles
        ) / len(profiles)
        failure = (1.0 - reliability) * self.settings.weight_failure
        migration = (
            self.settings.weight_migration
            if previous_resource_id and previous_resource_id != device.actor_id
            else 0.0
        )
        load = device.current_load * 2.0
        breakdown = {
            "fixed": fixed,
            "latency": latency,
            "token": token,
            "energy": energy,
            "failure": failure,
            "migration": migration,
            "load": load,
        }
        # Prefer a matching heterogeneous Agent when the tier is inferred rather
        # than explicitly fixed.  Do not make it infeasible if only another tier
        # is currently available.
        agent_tier = str((agent.metadata or {}).get("tier", agent.device_location or "")).casefold()
        if not requested_tier_raw and agent_tier and agent_tier != recommended_tier.value:
            breakdown["placement_mismatch"] = 0.25

        return CostEvaluation(
            True,
            sum(breakdown.values()),
            breakdown,
            ("hard privacy/permission/location filters passed",),
            tier=recommended_tier,
            partition_policy=partition_desc.partition_policy,
            partition_descriptor=partition_desc,
        )
