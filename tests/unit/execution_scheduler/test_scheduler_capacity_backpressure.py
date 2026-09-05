from __future__ import annotations

import pytest

from mosaic_omega.execution_scheduler.adapters.postgres import MemoryDatabase
from mosaic_omega.execution_scheduler.capability import CapabilityRegistry
from mosaic_omega.execution_scheduler.config import Settings
from mosaic_omega.execution_scheduler.cost_model import CostModel
from mosaic_omega.execution_scheduler.models import (
    ActorKind,
    CapabilityProfile,
    TaskNodeView,
    TaskState,
)
from mosaic_omega.execution_scheduler.scheduler import Scheduler


def test_ortools_uses_capacity_bounded_subsets_without_greedy_fallback(tmp_path) -> None:
    pytest.importorskip("ortools", reason="optional scheduler dependency; strict scheduler remains fail-closed when absent")
    settings = Settings.from_env({
        "EXECUTION_WORKSPACE": str(tmp_path),
        "SCHEDULER_POLICY": "ortools",
        "SCHEDULER_ALLOW_FALLBACK": "false",
    })
    registry = CapabilityRegistry(MemoryDatabase())
    common = dict(
        task_types=frozenset({"*"}),
        capabilities=frozenset({"*"}),
        permissions=frozenset({"*"}),
    )
    registry.register(CapabilityProfile("agent", ActorKind.AGENT, **common))
    registry.register(CapabilityProfile("model", ActorKind.MODEL, **common))
    registry.register(CapabilityProfile("tool", ActorKind.TOOL, **common))
    registry.register(CapabilityProfile("device", ActorKind.DEVICE, capacity=4, **common))
    scheduler = Scheduler(registry, CostModel(settings), settings)
    tasks = [
        TaskNodeView(
            run_id="capacity-run",
            task_id=f"task-{index:02d}",
            task_type="general",
            description="capacity back-pressure",
            state=TaskState.READY,
        )
        for index in range(10)
    ]

    assignments = scheduler.assign_tasks(tasks)

    assert len(assignments) == 4
    assert all(item.policy == "ortools" for item in assignments)
    assert len({item.task_id for item in assignments}) == 4
