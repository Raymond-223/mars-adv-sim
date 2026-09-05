"""Distribution metrics used by acceptance tests and experiment summaries."""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Protocol

from ..agent_runtime.task_messages import TaskMessage


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * fraction) - 1))
    return round(ordered[index], 6)


@dataclass
class MetricSeries:
    values: list[float] = field(default_factory=list)

    def observe(self, value: float) -> None:
        if value < 0:
            raise ValueError("metric values must be non-negative")
        self.values.append(float(value))

    def summary(self) -> dict[str, float | int]:
        return {
            "count": len(self.values),
            "mean": round(sum(self.values) / len(self.values), 6) if self.values else 0.0,
            "p50": percentile(self.values, 0.50),
            "p95": percentile(self.values, 0.95),
            "p99": percentile(self.values, 0.99),
        }


@dataclass
class MessageTopologyTelemetry:
    topology_recovery_ms: MetricSeries = field(default_factory=MetricSeries)
    queue_wait_ms: MetricSeries = field(default_factory=MetricSeries)
    mqtt_rtt_ms: MetricSeries = field(default_factory=MetricSeries)
    model_input_tokens: MetricSeries = field(default_factory=MetricSeries)
    model_output_tokens: MetricSeries = field(default_factory=MetricSeries)

    def snapshot(self) -> dict[str, dict[str, float | int]]:
        return {
            "topology_recovery_ms": self.topology_recovery_ms.summary(),
            "queue_wait_ms": self.queue_wait_ms.summary(),
            "mqtt_rtt_ms": self.mqtt_rtt_ms.summary(),
            "model_input_tokens": self.model_input_tokens.summary(),
            "model_output_tokens": self.model_output_tokens.summary(),
        }


def fact_fidelity(expected: set[str], received: set[str]) -> float:
    if not expected:
        return 1.0
    return round(len(expected & received) / len(expected), 6)


def evaluate_fidelity_samples(samples: list[tuple[set[str], set[str]]]) -> dict[str, float | int]:
    """Aggregate a fixed golden-set evaluation without hiding empty samples."""
    scores = [fact_fidelity(expected, received) for expected, received in samples]
    return {
        "sample_count": len(scores),
        "mean": round(sum(scores) / len(scores), 6) if scores else 0.0,
        "minimum": min(scores, default=0.0),
        "p95": percentile(scores, 0.95),
    }


def reduction(baseline: float, optimized: float) -> float:
    return round(1 - optimized / baseline, 6) if baseline else 0.0


class TokenCounter(Protocol):
    """Adapter implemented by a real model tokenizer or usage reporter."""

    def count(self, text: str) -> int: ...


def measured_message_tokens(message: TaskMessage, counter: TokenCounter) -> int:
    """Count the exact serialized business message with an injected tokenizer."""
    import json

    return counter.count(json.dumps(
        message.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ))


@dataclass(frozen=True)
class AcceptanceThresholds:
    topology_recovery_p95_ms: float = 300.0
    critical_path_disconnect_rate: float = 0.01
    dynamic_topology_token_reduction: float = 0.35
    low_entropy_token_reduction: float = 0.50
    fact_fidelity: float = 0.99
    queue_wait_p95_ms: float = 500.0


def evaluate_acceptance(
    metrics: dict[str, float | None],
    thresholds: AcceptanceThresholds | None = None,
) -> dict[str, dict[str, float | bool | str | None]]:
    """Evaluate only supplied measurements; missing data is never a pass."""
    thresholds = thresholds or AcceptanceThresholds()
    checks = {
        "topology_recovery_p95_ms": (thresholds.topology_recovery_p95_ms, "max"),
        "critical_path_disconnect_rate": (thresholds.critical_path_disconnect_rate, "max"),
        "dynamic_topology_token_reduction": (thresholds.dynamic_topology_token_reduction, "min"),
        "low_entropy_token_reduction": (thresholds.low_entropy_token_reduction, "min"),
        "fact_fidelity": (thresholds.fact_fidelity, "min"),
        "queue_wait_p95_ms": (thresholds.queue_wait_p95_ms, "max"),
    }
    result: dict[str, dict[str, float | bool | str | None]] = {}
    for name, (threshold, direction) in checks.items():
        value = metrics.get(name)
        passed = None if value is None else (
            value <= threshold if direction == "max" else value >= threshold
        )
        result[name] = {
            "value": value,
            "threshold": threshold,
            "direction": direction,
            "passed": passed,
            "status": "insufficient_data" if passed is None else ("pass" if passed else "fail"),
        }
    return result
