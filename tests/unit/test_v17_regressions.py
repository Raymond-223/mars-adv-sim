from __future__ import annotations

import hashlib
from pathlib import Path

from test_support.mock_agent import MockAgent
from mosaic_omega.execution_scheduler.adapters.local_tool_executor import LocalToolExecutor
from mosaic_omega.execution_scheduler.config import Settings
from mosaic_omega.execution_scheduler.models import (
    ActorKind,
    Assignment,
    CapabilityProfile,
    Evidence,
    TaskNodeView,
    TaskState,
    ToolCall,
)
from mosaic_omega.execution_scheduler.service import ExecutionSchedulerService
from mosaic_omega.verifier import VerifierService


def _settings(workspace: Path) -> Settings:
    return Settings.from_env({
        "EXECUTION_WORKSPACE": str(workspace),
        "SCHEDULER_POLICY": "greedy",
        "ALLOWED_COMMANDS": "python,python.exe",
        "TOOL_TIMEOUT_S": "5",
        "MAX_TASK_RETRIES": "0",
    })


def _profiles() -> list[CapabilityProfile]:
    common = dict(task_types=frozenset({"*"}), capabilities=frozenset({"*"}), permissions=frozenset({"*"}))
    return [
        CapabilityProfile("agent", ActorKind.AGENT, **common),
        CapabilityProfile("model", ActorKind.MODEL, context_limit=10000, **common),
        CapabilityProfile("task", ActorKind.TOOL, **common),
        CapabilityProfile("write_file", ActorKind.TOOL, **common),
        CapabilityProfile("edge", ActorKind.DEVICE, capacity=2, device_location="edge", **common),
    ]


def _register(service: ExecutionSchedulerService) -> None:
    for profile in _profiles():
        service.register_actor(profile, adapter=MockAgent("agent") if profile.actor_id == "agent" else None)


def test_generic_task_cannot_pass_by_echoing_its_own_acceptance(tmp_path: Path, monkeypatch) -> None:
    for key in ("MOSAIC_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    settings = _settings(tmp_path)
    executor = LocalToolExecutor(settings)
    call = ToolCall(
        run_id="run-self-echo",
        task_id="task-a",
        actor_id="agent",
        tool_name="task",
        arguments={"description": "生成最终报告"},
        idempotency_key="self-echo",
    )
    result = executor.execute(call, timeout_s=5)
    assert result.success is True
    assert result.metadata["acceptance_conditions_injected"] is False
    assert "acceptance:" not in result.output.casefold()
    deliverable = tmp_path / result.metadata["deliverable_relative"]
    assert deliverable.is_file()

    raw = f"{result.output}\n".encode("utf-8")
    evidence = Evidence(
        run_id="run-self-echo",
        task_id="task-a",
        kind="tool_execution",
        digest=hashlib.sha256(raw).hexdigest(),
        content=result.output,
    )
    task = TaskNodeView(
        run_id="run-self-echo",
        task_id="task-a",
        task_type="general",
        description="生成最终报告",
        state=TaskState.VERIFYING,
        acceptance_conditions=("生成最终报告",),
    )
    verification = VerifierService(tmp_path).verify(task, result, (evidence,))
    assert verification.passed is False
    assert verification.metadata["self_echo_acceptance_disabled"] is True
    assert verification.metadata["semantic_check_count"] == 1


def test_sqlite_run_can_resume_after_process_restart_at_ready_boundary(tmp_path: Path) -> None:
    db = tmp_path / "state" / "execution.sqlite3"
    settings = _settings(tmp_path / "workspace")
    first = ExecutionSchedulerService.sqlite(settings, path=str(db))
    _register(first)
    run_id = first.create_run([
        {"task_id": "a", "description": "persisted task", "acceptance_conditions": ["persisted task"]}
    ], run_id="durable-ready")
    assert first.events.require_task(run_id, "a").state is TaskState.READY

    second = ExecutionSchedulerService.sqlite(settings, path=str(db))
    _register(second)
    resumed = second.resume_run(run_id)
    assert resumed["safe_stopped_side_effecting"] == []
    assert second.events.require_task(run_id, "a").state is TaskState.SUCCEEDED
    assert any(e.event_type == "TASK_VERIFIED" for e in second.events.events(run_id=run_id))


def test_restart_does_not_blindly_repeat_unknown_side_effect(tmp_path: Path) -> None:
    db = tmp_path / "state" / "execution.sqlite3"
    settings = _settings(tmp_path / "workspace")
    first = ExecutionSchedulerService.sqlite(settings, path=str(db))
    _register(first)
    run_id = first.create_run([{"task_id": "a", "description": "write"}], run_id="durable-safe-stop")
    assignment = Assignment(
        task_id="a", agent_id="agent", model_id="model", tool_id="write_file", resource_id="edge",
        total_cost=0.0, cost_breakdown={}, policy="test", reason="crash-boundary",
        execution_tier="edge", recommended_tier="edge", actual_execution_tier="edge", run_id=run_id,
    )
    first.events.assign(run_id, "a", assignment, actor_id="test")
    first.events.transition(run_id, "a", TaskState.RUNNING, actor_id="test")

    second = ExecutionSchedulerService.sqlite(settings, path=str(db))
    _register(second)
    resumed = second.resume_run(run_id)
    assert resumed["safe_stopped_side_effecting"] == ["a"]
    assert second.events.require_task(run_id, "a").state is TaskState.PAUSED
    assert any(e.event_type == "RUN_RESUME_SAFE_STOP" for e in second.events.events(run_id=run_id))


def test_reference_split_runs_from_source_tree_without_editable_install(monkeypatch) -> None:
    from mosaic_omega.agent_runtime.split_inference import run_pipeline_split
    # A deliberately minimal PYTHONPATH proves the child receives the source tree
    # from split_inference.py itself instead of depending on test-runner sys.path.
    monkeypatch.setenv("PYTHONPATH", "")
    result = run_pipeline_split([0.15, -0.2, 0.45, 0.7, -0.55, 0.31, 0.08, -0.11])
    assert result["process_boundary_verified"] is True
    assert result["verified_equivalent"] is True
    assert result["claim_boundary"] == "REFERENCE_MLP_NOT_LLM_SPLIT"
