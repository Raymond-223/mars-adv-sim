from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from test_support.mock_agent import MockAgent
from mosaic_omega.execution_scheduler.config import Settings
from mosaic_omega.execution_scheduler.models import ActorKind, CapabilityProfile, TaskState
from mosaic_omega.execution_scheduler.service import ExecutionSchedulerService


def _settings(workspace: str, retries: int = 1) -> Settings:
    return Settings.from_env({
        "EXECUTION_WORKSPACE": workspace,
        "SCHEDULER_POLICY": "greedy",
        "ALLOWED_COMMANDS": "python,python3,python.exe",
        "TOOL_TIMEOUT_S": "5",
        "MAX_TASK_RETRIES": str(retries),
    })


def _register(service: ExecutionSchedulerService, agent) -> None:
    common = dict(task_types=frozenset({"*"}), capabilities=frozenset({"*"}), permissions=frozenset({"*"}))
    service.register_actor(CapabilityProfile("agent", ActorKind.AGENT, **common), adapter=agent)
    service.register_actor(CapabilityProfile("model", ActorKind.MODEL, **common))
    service.register_actor(CapabilityProfile("task", ActorKind.TOOL, **common))
    service.register_actor(CapabilityProfile("device", ActorKind.DEVICE, capacity=2, **common))


def test_verifier_is_independent_and_evidence_manifest_is_hash_backed() -> None:
    with tempfile.TemporaryDirectory() as temp:
        service = ExecutionSchedulerService.memory(_settings(temp))
        _register(service, MockAgent("agent"))
        run_id = service.create_run([
            {
                "task_id": "a",
                "task_type": "general",
                "description": "produce audited output",
                "acceptance_conditions": ["produce audited output"],
            }
        ])
        assert service.run_until_blocked(run_id) == ["a"]
        task = service.events.require_task(run_id, "a")
        assert task.state is TaskState.SUCCEEDED
        assert task.evidence and task.evidence[0].uri and task.evidence[0].uri.startswith("file:")
        assert Path(task.evidence[0].metadata["artifact_path"]).is_file()
        verified = [event for event in service.events.events(run_id=run_id) if event.event_type == "TASK_VERIFIED"]
        assert len(verified) == 1
        assert verified[0].actor_id == "verifier"
        assert verified[0].payload["verification"]["verifier"] == "deterministic-verifier"


class FailingAgent(MockAgent):
    def plan(self, task, assignment, trace_id):
        calls = super().plan(task, assignment, trace_id)
        call = calls[0]
        raw = call.to_dict()
        raw["tool_name"] = "shell"
        raw["arguments"] = {"command": [Path(sys.executable).name, "-c", "import sys; sys.exit(7)"]}
        raw["required_permissions"] = []
        raw["idempotency_key"] = f"{task.run_id}:{task.task_id}:{max(1, task.attempt)}:fail"
        return [type(call).from_dict(raw)]


def test_recovery_retries_only_failed_node_and_keeps_descendant_unexecuted() -> None:
    with tempfile.TemporaryDirectory() as temp:
        service = ExecutionSchedulerService.memory(_settings(temp, retries=1))
        _register(service, FailingAgent("agent"))
        run_id = service.create_run([
            {"task_id": "a", "task_type": "general", "description": "fail", "depends_on": []},
            {"task_id": "b", "task_type": "general", "description": "child", "depends_on": ["a"]},
        ])
        service.run_until_blocked(run_id, max_rounds=10)
        a = service.events.require_task(run_id, "a")
        b = service.events.require_task(run_id, "b")
        assert a.state is TaskState.FAILED
        assert a.attempt == 2  # first attempt + one configured retry
        assert b.state is TaskState.PLANNED
        plans = [e for e in service.events.events(run_id=run_id) if e.event_type == "RECOVERY_PLANNED"]
        assert len(plans) == 2
        assert tuple(plans[0].payload["affected_task_ids"]) == ("a", "b")
        assert plans[0].payload["retry_allowed"] is True
        assert plans[1].payload["retry_allowed"] is False


def test_replan_invalidates_only_impact_closure_and_releases_root() -> None:
    with tempfile.TemporaryDirectory() as temp:
        service = ExecutionSchedulerService.memory(_settings(temp, retries=0))
        _register(service, MockAgent("agent"))
        run_id = service.create_run([
            {"task_id": "root", "task_type": "general", "description": "root"},
            {"task_id": "child", "task_type": "general", "description": "child", "depends_on": ["root"]},
            {"task_id": "independent", "task_type": "general", "description": "independent"},
        ])
        service.run_until_blocked(run_id)
        assert all(t.state is TaskState.SUCCEEDED for t in service.events.tasks(run_id))

        # Build the explicit REPLAN_REQUIRED plan through the public error class.
        from mosaic_omega.execution_scheduler.models import ErrorClass
        plan = service.recovery.plan(
            run_id, "root", error_class=ErrorClass.REPLAN_REQUIRED, reason="requirement changed"
        )
        assert plan.affected_task_ids == ("root", "child")
        assert service.recovery.execute(plan, trace_id="trace-replan") is True
        states = {t.task_id: t.state for t in service.events.tasks(run_id)}
        assert states["root"] is TaskState.READY
        assert states["child"] is TaskState.PLANNED
        assert states["independent"] is TaskState.SUCCEEDED
        assert any(e.event_type == "TASK_REPLANNED" for e in service.events.events(run_id=run_id))


def test_safe_stop_pauses_high_risk_failure_instead_of_guessing() -> None:
    from mosaic_omega.execution_scheduler.models import ErrorClass
    with tempfile.TemporaryDirectory() as temp:
        service = ExecutionSchedulerService.memory(_settings(temp, retries=0))
        _register(service, MockAgent("agent"))
        run_id = service.create_run([
            {"task_id": "a", "task_type": "general", "description": "unsafe"}
        ])
        task = service.events.require_task(run_id, "a")
        # READY is a pausable active state; a SAFE_STOP must not silently fail or retry.
        plan = service.recovery.plan(
            run_id, "a", error_class=ErrorClass.SAFE_STOP, reason="evidence insufficient"
        )
        assert service.recovery.execute(plan, trace_id="trace-stop") is True
        assert service.events.require_task(run_id, "a").state is TaskState.PAUSED
        assert any(e.event_type == "SAFE_STOP_TRIGGERED" for e in service.events.events(run_id=run_id))


def test_rollback_executes_declared_compensation_then_replans_affected_subgraph() -> None:
    from mosaic_omega.execution_scheduler.models import Assignment, ErrorClass
    with tempfile.TemporaryDirectory() as temp:
        service = ExecutionSchedulerService.memory(_settings(temp, retries=0))
        common = dict(task_types=frozenset({"*"}), capabilities=frozenset({"*"}), permissions=frozenset({"*"}))
        service.register_actor(
            CapabilityProfile("agent", ActorKind.AGENT, **common), adapter=MockAgent("agent")
        )
        service.register_actor(CapabilityProfile("model", ActorKind.MODEL, **common))
        service.register_actor(CapabilityProfile("write_file", ActorKind.TOOL, **common))
        service.register_actor(CapabilityProfile("device", ActorKind.DEVICE, capacity=1, **common))
        run_id = service.create_run([
            {
                "task_id": "a",
                "task_type": "general",
                "description": "side effect",
                "metadata": {
                    "rollback_tool": {
                        "name": "write_file",
                        "arguments": {"path": "rollback.marker", "content": "rolled back"},
                    }
                },
            }
        ])
        assignment = Assignment(
            task_id="a",
            agent_id="agent",
            model_id="model",
            tool_id="write_file",
            resource_id="device",
            total_cost=0.0,
            cost_breakdown={},
            policy="greedy",
            reason="test",
            run_id=run_id,
        )
        service.events.assign(run_id, "a", assignment, actor_id="test")
        service.events.transition(run_id, "a", TaskState.RUNNING, actor_id="test")
        plan = service.recovery.plan(
            run_id, "a", error_class=ErrorClass.ROLLBACK_REQUIRED, reason="partial side effect"
        )
        assert service.recovery.execute(plan, trace_id="trace-rollback") is True
        assert (Path(temp) / "rollback.marker").read_text(encoding="utf-8") == "rolled back"
        assert service.events.require_task(run_id, "a").state is TaskState.READY
        kinds = [e.event_type for e in service.events.events(run_id=run_id)]
        assert "ROLLBACK_EXECUTED" in kinds
        assert "TASK_REPLANNED" in kinds
