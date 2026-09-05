"""Repeatable local acceptance benchmarks for the execution scheduler.

These are engineering checks, not production guarantees.  PostgreSQL and the target
machine must be benchmarked separately before claiming the handbook thresholds.
"""

from __future__ import annotations

import argparse
import math
import statistics
import tempfile
import time

from .adapters.postgres import MemoryDatabase
from .capability import CapabilityRegistry
from .config import Settings
from .cost_model import CostModel
from .event_store import EventStore
from .models import ActorKind, CapabilityProfile, TaskNodeView, TaskState
from .scheduler import Scheduler


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, math.ceil(len(ordered) * 0.95) - 1)]


def event_benchmark(count: int) -> dict[str, float]:
    database = MemoryDatabase()
    store = EventStore(database, snapshot_interval=0)
    run_id = "bench-events"
    trace_id = "trace-bench-events"
    store.create_run(run_id, actor_id="benchmark", trace_id=trace_id)
    task = TaskNodeView(run_id, "node", "benchmark", "event replay benchmark")
    store.create_task(task, actor_id="benchmark", trace_id=trace_id)
    projection = task.to_dict()
    latencies_ms: list[float] = []

    started = time.perf_counter()
    for _ in range(max(0, count - 2)):
        tick = time.perf_counter()
        store.append_event(
            "BENCH_EVENT",
            run_id,
            actor_id="benchmark",
            task_id=task.task_id,
            trace_id=trace_id,
            payload={"projection": projection},
            publish=False,
        )
        latencies_ms.append((time.perf_counter() - tick) * 1000.0)
    append_total_s = time.perf_counter() - started

    started = time.perf_counter()
    store.replay(run_id, task.task_id)
    replay_s = time.perf_counter() - started
    return {
        "event_count": float(count),
        "append_total_s": append_total_s,
        "append_p95_ms": _p95(latencies_ms),
        "replay_s": replay_s,
    }


def scheduler_benchmark(task_count: int, resource_count: int, repeats: int, policy: str) -> dict[str, float | str]:
    with tempfile.TemporaryDirectory() as workspace:
        settings = Settings.from_env(
            {
                "EXECUTION_WORKSPACE": workspace,
                "SCHEDULER_POLICY": policy,
                "ALLOWED_COMMANDS": "python,python.exe",
                "TOOL_TIMEOUT_S": "5",
                "SCHEDULER_ALLOW_FALLBACK": "false" if policy == "ortools" else "true",
            }
        )
        database = MemoryDatabase()
        registry = CapabilityRegistry(database)
        common = {
            "task_types": frozenset({"*"}),
            "capabilities": frozenset({"*"}),
            "permissions": frozenset({"*"}),
        }
        registry.register(CapabilityProfile("agent", ActorKind.AGENT, **common))
        registry.register(
            CapabilityProfile("model", ActorKind.MODEL, context_limit=1_000_000, **common)
        )
        registry.register(CapabilityProfile("tool", ActorKind.TOOL, **common))
        per_resource = max(1, math.ceil(task_count / max(1, resource_count)))
        for index in range(resource_count):
            registry.register(
                CapabilityProfile(
                    f"resource-{index:02d}",
                    ActorKind.DEVICE,
                    capacity=per_resource,
                    latency_ms=float(index + 1),
                    device_location="edge",
                    **common,
                )
            )
        tasks = [
            TaskNodeView(
                "bench-scheduler",
                f"task-{index:03d}",
                "benchmark",
                "scheduler benchmark",
                state=TaskState.READY,
                priority=index % 10,
            )
            for index in range(task_count)
        ]
        scheduler = Scheduler(registry, CostModel(settings), settings)
        latencies_ms: list[float] = []
        last_assignments = []
        for _ in range(max(1, repeats)):
            tick = time.perf_counter()
            last_assignments = scheduler.assign_tasks(tasks)
            latencies_ms.append((time.perf_counter() - tick) * 1000.0)
        actual_policy = last_assignments[0].policy if last_assignments else "none"
        return {
            "task_count": float(task_count),
            "resource_count": float(resource_count),
            "assignment_count": float(len(last_assignments)),
            "requested_policy": policy,
            "actual_policy": actual_policy,
            "median_ms": statistics.median(latencies_ms),
            "p95_ms": _p95(latencies_ms),
            "total_objective_cost": sum(item.total_cost for item in last_assignments),
        }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--events", type=int, default=100_000)
    parser.add_argument("--tasks", type=int, default=100)
    parser.add_argument("--resources", type=int, default=20)
    parser.add_argument("--repeats", type=int, default=20)
    parser.add_argument("--policy", choices=("ortools", "greedy", "round_robin"), default="ortools")
    args = parser.parse_args()

    event_result = event_benchmark(args.events)
    scheduler_result = scheduler_benchmark(args.tasks, args.resources, args.repeats, args.policy)
    print("event_benchmark", event_result)
    print("scheduler_benchmark", scheduler_result)
    print(
        "targets",
        {
            "event_append_p95_lt_20ms": event_result["append_p95_ms"] < 20.0,
            "event_replay_lt_30s": event_result["replay_s"] < 30.0,
            "scheduler_p95_lt_200ms": float(scheduler_result["p95_ms"]) < 200.0,
            "requested_policy_really_used": scheduler_result["actual_policy"] == args.policy,
        },
    )


if __name__ == "__main__":
    main()
