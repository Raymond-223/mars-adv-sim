from __future__ import annotations

import tempfile

import pytest

from mosaic_omega.execution_scheduler.adapters.postgres import MemoryDatabase
from mosaic_omega.execution_scheduler.capability import CapabilityRegistry
from mosaic_omega.execution_scheduler.config import Settings
from mosaic_omega.execution_scheduler.cost_model import CostModel
from mosaic_omega.execution_scheduler.models import ActorKind, CapabilityProfile, TaskNodeView, TaskState
from mosaic_omega.execution_scheduler.scheduler import Scheduler


def _settings(workspace: str, allow_fallback: bool) -> Settings:
    return Settings.from_env({
        "EXECUTION_WORKSPACE": workspace,
        "SCHEDULER_POLICY": "ortools",
        "SCHEDULER_ALLOW_FALLBACK": "true" if allow_fallback else "false",
    })


def _scheduler(settings: Settings) -> Scheduler:
    registry = CapabilityRegistry(MemoryDatabase())
    common = dict(task_types=frozenset({"*"}), capabilities=frozenset({"*"}), permissions=frozenset({"*"}))
    registry.register(CapabilityProfile("agent", ActorKind.AGENT, **common))
    registry.register(CapabilityProfile("model", ActorKind.MODEL, **common))
    registry.register(CapabilityProfile("tool", ActorKind.TOOL, **common))
    registry.register(CapabilityProfile("device", ActorKind.DEVICE, capacity=2, **common))
    return Scheduler(registry, CostModel(settings), settings)


def test_production_ortools_mode_never_silently_falls_back(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as temp:
        scheduler = _scheduler(_settings(temp, False))
        monkeypatch.setattr(scheduler, "_ortools", lambda tasks: (_ for _ in ()).throw(ImportError("missing")))
        task = TaskNodeView("run", "a", "general", "x", state=TaskState.READY)
        with pytest.raises(ImportError):
            scheduler.assign_tasks([task])


def test_beta_posterior_changes_failure_cost() -> None:
    with tempfile.TemporaryDirectory() as temp:
        settings = _settings(temp, True)
        scheduler = _scheduler(settings)
        registry = scheduler.registry
        agent = registry.get("agent")
        agent.posterior["general"] = {"alpha": 99.0, "beta": 1.0}
        registry.save(agent)
        task = TaskNodeView("run", "a", "general", "x", state=TaskState.READY)
        candidates, _rejected = scheduler.candidates_for(task)
        candidate = next(iter(candidates.values()))
        assert candidate.cost_breakdown["failure"] < 2.0
