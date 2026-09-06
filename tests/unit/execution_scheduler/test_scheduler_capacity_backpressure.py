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
    registry.register(CapabilityProfile("agent", ActorKind.AGENT, capacity=4, **common))
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
    queued = [item for item in scheduler.last_round.diagnostics if item.state == "QUEUED"]
    assert len(queued) == 6
    assert all(item.candidates for item in queued)


def test_agent_concurrency_is_a_hard_constraint_not_only_device_capacity(tmp_path) -> None:
    """An Agent with max_load=1 may not receive four concurrent tasks.

    Agent concurrency used to be ignored: only device capacity entered the
    optimization model, so a single Agent instance could be handed a whole DAG
    layer at once while extra same-role Agents were never scheduled at all.
    """
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
    registry.register(CapabilityProfile("agent-a", ActorKind.AGENT, capacity=1, **common))
    registry.register(CapabilityProfile("agent-b", ActorKind.AGENT, capacity=1, **common))
    registry.register(CapabilityProfile("agent-c", ActorKind.AGENT, capacity=1, **common))
    registry.register(CapabilityProfile("model", ActorKind.MODEL, **common))
    registry.register(CapabilityProfile("tool", ActorKind.TOOL, **common))
    registry.register(CapabilityProfile("device", ActorKind.DEVICE, capacity=8, **common))
    scheduler = Scheduler(registry, CostModel(settings), settings)
    tasks = [
        TaskNodeView(
            run_id="concurrency-run",
            task_id=f"task-{index:02d}",
            task_type="general",
            description="agent concurrency",
            state=TaskState.READY,
        )
        for index in range(6)
    ]

    assignments = scheduler.assign_tasks(tasks)

    # Three single-slot Agents can absorb exactly three tasks per round, even
    # though the shared resource pool still has eight free slots.
    assert len(assignments) == 3
    assert sorted(item.agent_id for item in assignments) == ["agent-a", "agent-b", "agent-c"]


def test_candidates_keep_every_same_role_agent_in_contention(tmp_path) -> None:
    """Same-role Agents must remain distinct candidates, not be pre-compressed."""
    settings = Settings.from_env({"EXECUTION_WORKSPACE": str(tmp_path)})
    registry = CapabilityRegistry(MemoryDatabase())
    common = dict(
        task_types=frozenset({"*"}),
        capabilities=frozenset({"*"}),
        permissions=frozenset({"*"}),
    )
    registry.register(CapabilityProfile("analyst-01", ActorKind.AGENT, **common))
    registry.register(CapabilityProfile("analyst-02", ActorKind.AGENT, fixed_cost=1.0, **common))
    registry.register(CapabilityProfile("model", ActorKind.MODEL, **common))
    registry.register(CapabilityProfile("tool", ActorKind.TOOL, **common))
    registry.register(CapabilityProfile("device", ActorKind.DEVICE, capacity=4, **common))
    scheduler = Scheduler(registry, CostModel(settings), settings)
    task = TaskNodeView("run", "a", "general", "x", state=TaskState.READY)

    candidates, rejected = scheduler.candidates_for(task)

    assert {item.agent_id for item in candidates.values()} == {"analyst-01", "analyst-02"}
    assert rejected == []
