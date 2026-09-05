"""Device/edge/cloud resource and model partitioning contracts used by the scheduler."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ExecutionTier(str, Enum):
    DEVICE = "device"
    EDGE = "edge"
    CLOUD = "cloud"


class ModelPartitionPolicy(str, Enum):
    NONE = "none"
    PIPELINE_SPLIT = "pipeline_split"
    TENSOR_SPLIT = "tensor_split"
    FEATURE_OFFLOAD = "feature_offload"


@dataclass(frozen=True)
class ModelPartitionDescriptor:
    partition_policy: ModelPartitionPolicy = ModelPartitionPolicy.NONE
    split_layer_index: int = 0
    device_stage_ratio: float = 1.0
    cloud_stage_ratio: float = 0.0
    bandwidth_min_mbps: float = 10.0

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "ModelPartitionDescriptor":
        raw = raw or {}
        return cls(
            partition_policy=ModelPartitionPolicy(
                raw.get("partition_policy", ModelPartitionPolicy.NONE.value)
            ),
            split_layer_index=max(0, int(raw.get("split_layer_index", 0))),
            device_stage_ratio=float(raw.get("device_stage_ratio", 1.0)),
            cloud_stage_ratio=float(raw.get("cloud_stage_ratio", 0.0)),
            bandwidth_min_mbps=float(raw.get("bandwidth_min_mbps", 10.0)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "partition_policy": self.partition_policy.value,
            "split_layer_index": self.split_layer_index,
            "device_stage_ratio": self.device_stage_ratio,
            "cloud_stage_ratio": self.cloud_stage_ratio,
            "bandwidth_min_mbps": self.bandwidth_min_mbps,
        }


@dataclass(frozen=True)
class ResourceDescriptor:
    cpu_cores: int = 0
    memory_mb: int = 0
    gpu_count: int = 0
    accelerator_tags: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "ResourceDescriptor":
        raw = raw or {}
        return cls(
            cpu_cores=max(0, int(raw.get("cpu_cores", 0))),
            memory_mb=max(0, int(raw.get("memory_mb", 0))),
            gpu_count=max(0, int(raw.get("gpu_count", 0))),
            accelerator_tags=tuple(str(item) for item in raw.get("accelerator_tags", ())),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "cpu_cores": self.cpu_cores,
            "memory_mb": self.memory_mb,
            "gpu_count": self.gpu_count,
            "accelerator_tags": list(self.accelerator_tags),
        }


class EdgeCloudPlacementEngine:
    """Evaluates task requirements (privacy level, latency constraint, estimated tokens)

    to decide optimal ExecutionTier and ModelPartitionDescriptor.
    """

    @staticmethod
    def select_placement(
        privacy_level: str,
        max_latency_ms: float | None = None,
        estimated_tokens: int = 0,
        device_gpu: bool = False,
    ) -> tuple[ExecutionTier, ModelPartitionDescriptor]:
        level = (privacy_level or "public").casefold()

        # Strict privacy constraint: secret or restricted MUST run locally on DEVICE or EDGE
        if level in {"secret", "restricted"}:
            tier = ExecutionTier.DEVICE if level == "secret" else ExecutionTier.EDGE
            partition = ModelPartitionDescriptor(
                partition_policy=ModelPartitionPolicy.NONE,
                device_stage_ratio=1.0,
                cloud_stage_ratio=0.0,
            )
            return tier, partition

        # High latency strictness (< 200ms) prefers EDGE/DEVICE
        if max_latency_ms is not None and max_latency_ms <= 200:
            tier = ExecutionTier.DEVICE if device_gpu else ExecutionTier.EDGE
            if max_latency_ms < 100:
                partition = ModelPartitionDescriptor(
                    partition_policy=ModelPartitionPolicy.FEATURE_OFFLOAD,
                    split_layer_index=12,
                    device_stage_ratio=0.4,
                    cloud_stage_ratio=0.6,
                )
            else:
                partition = ModelPartitionDescriptor(
                    partition_policy=ModelPartitionPolicy.NONE,
                    split_layer_index=0,
                    device_stage_ratio=1.0,
                    cloud_stage_ratio=0.0,
                )
            return tier, partition

        # High complexity / token volume (> 4000 tokens) offloads to CLOUD with pipeline partition
        if estimated_tokens > 4000:
            return ExecutionTier.CLOUD, ModelPartitionDescriptor(
                partition_policy=ModelPartitionPolicy.PIPELINE_SPLIT,
                split_layer_index=24,
                device_stage_ratio=0.2,
                cloud_stage_ratio=0.8,
            )

        # Default public/internal task balance
        return ExecutionTier.EDGE, ModelPartitionDescriptor(
            partition_policy=ModelPartitionPolicy.NONE,
            split_layer_index=0,
            device_stage_ratio=1.0,
            cloud_stage_ratio=0.0,
        )
