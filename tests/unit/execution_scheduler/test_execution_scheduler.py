from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_support.mock_agent import MockAgent
from mosaic_omega.execution_scheduler.adapters.postgres import MemoryDatabase
from mosaic_omega.execution_scheduler.capability import CapabilityRegistry
from mosaic_omega.execution_scheduler.config import Settings
from mosaic_omega.execution_scheduler.cost_model import CostModel
from mosaic_omega.execution_scheduler.event_store import EventStore
from mosaic_omega.execution_scheduler.idempotency import IdempotencyManager
from mosaic_omega.execution_scheduler.resource_monitor import ResourceMonitor
from mosaic_omega.execution_scheduler.models import (
    ActorKind,
    CapabilityProfile,
    ExecutionResult,
    TaskNodeView,
    TaskState,
    ToolCall,
)
from mosaic_omega.execution_scheduler.scheduler import Scheduler
from mosaic_omega.execution_scheduler.service import ExecutionSchedulerService
from mosaic_omega.execution_scheduler.state_machine import IllegalTransition
from mosaic_omega.execution_scheduler.tool_runtime import ToolRuntime


def settings(workspace: str, policy: str = "greedy") -> Settings:
    return Settings.from_env({
        "EXECUTION_WORKSPACE": workspace,
        "SCHEDULER_POLICY": policy,
        "ALLOWED_COMMANDS": "python,python.exe",
        "TOOL_TIMEOUT_S": "5",
    })


def profiles() -> list[CapabilityProfile]:
    common = {"task_types": frozenset({"*"}), "capabilities": frozenset({"*"}),
              "permissions": frozenset({"*"})}
    return [
        CapabilityProfile("agent", ActorKind.AGENT, **common),
        CapabilityProfile("model", ActorKind.MODEL, context_limit=10000, **common),
        CapabilityProfile("task", ActorKind.TOOL, **common),
        CapabilityProfile("edge", ActorKind.DEVICE, device_location="edge-a", capacity=2, **common),
    ]


class CountingExecutor:
    def __init__(self) -> None:
        self.calls = 0

    def execute(self, call: ToolCall, timeout_s: float) -> ExecutionResult:
        self.calls += 1
        return ExecutionResult(call.call_id, True, "done")


class ExecutionSchedulerTests(unittest.TestCase):
    def test_event_first_projection_replay_trace_and_illegal_transition(self) -> None:
        database = MemoryDatabase()
        store = EventStore(database)
        store.create_run("run", actor_id="test")
        task = TaskNodeView("run", "a", "code", "compile")
        store.create_task(task, actor_id="test", trace_id="trace-a")
        store.transition("run", "a", TaskState.PLANNED, actor_id="test", trace_id="trace-a")
        store.transition("run", "a", TaskState.READY, actor_id="test", trace_id="trace-a")
        with self.assertRaises(IllegalTransition):
            store.transition("run", "a", TaskState.SUCCEEDED, actor_id="test")
        replayed = store.replay("run", "a")
        self.assertEqual(replayed.state, TaskState.READY)
        self.assertEqual([event.sequence for event in store.events(run_id="run")], [1, 2, 3, 4])
        self.assertEqual(len(store.events(trace_id="trace-a")), 3)
        self.assertEqual(len(store.pending_outbox()), 4)

    def test_tool_runtime_checks_permission_and_executes_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            database = MemoryDatabase()
            registry = CapabilityRegistry(database)
            registry.register(CapabilityProfile(
                "agent", ActorKind.AGENT, frozenset({"*"}),
                permissions=frozenset({"file.write"}), capabilities=frozenset({"*"}),
            ))
            executor = CountingExecutor()
            runtime = ToolRuntime(executor, registry, IdempotencyManager(database), settings(temp))
            call = ToolCall(
                "run", "task", "agent", "write_file", {"path": "x", "content": "v1"},
                "same-key", required_permissions=frozenset({"file.write"})
            )
            first, _ = runtime.execute(call)
            second, evidence = runtime.execute(call)
            self.assertTrue(first.success and second.success)
            self.assertEqual(executor.calls, 1)
            self.assertTrue(second.metadata["reused"])
            self.assertTrue(evidence.digest)

            denied = ToolCall("run", "task", "agent", "shell", {"command": ["python", "-V"]}, "denied")
            result, _ = runtime.execute(denied)
            self.assertFalse(result.success)
            self.assertIn("PermissionError", result.error)

    def test_scheduler_hard_filters_privacy_and_falls_back_without_ortools(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            database = MemoryDatabase()
            registry = CapabilityRegistry(database)
            for profile in profiles():
                registry.register(profile)
            config = settings(temp, "ortools")
            scheduler = Scheduler(registry, CostModel(config), config)
            task = TaskNodeView(
                "run", "private", "code", "private code", state=TaskState.READY,
                privacy_level="restricted", data_location="edge-a",
            )
            # Simulate the optional dependency being unavailable so this test
            # remains deterministic even when OR-Tools is installed locally.
            with patch.object(scheduler, "_ortools", side_effect=ImportError("simulated missing ortools")):
                assignments = scheduler.assign_tasks([task])
                self.assertEqual(len(assignments), 1)
                self.assertEqual(assignments[0].resource_id, "edge")
                self.assertEqual(assignments[0].policy, "greedy_fallback")

                task.data_location = "other"
                self.assertEqual(scheduler.assign_tasks([task]), [])

    def test_service_runs_dependency_dag_and_updates_posteriors(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            service = ExecutionSchedulerService.memory(settings(temp))
            for profile in profiles():
                adapter = MockAgent("agent") if profile.actor_id == "agent" else None
                service.register_actor(profile, adapter=adapter)
            run_id = service.create_run([
                {"task_id": "a", "description": "prepare A", "required_skill": "code",
                 "depends_on": [], "acceptance_conditions": ["prepare A"]},
                {"task_id": "b", "description": "finish B", "required_skill": "code",
                 "depends_on": ["a"], "acceptance_conditions": ["finish B"]},
            ], run_id="run-e2e")
            completed = service.run_until_blocked(run_id)
            self.assertEqual(completed, ["a", "b"])
            tasks = service.events.tasks(run_id)
            self.assertTrue(all(task.state is TaskState.SUCCEEDED for task in tasks))
            self.assertGreater(len(service.events.events(run_id=run_id)), 10)
            self.assertIn("code", service.capabilities.get("agent").posterior)

    def test_canonical_event_evidence_fields_snapshot_and_parent_trace(self) -> None:
        database = MemoryDatabase()
        store = EventStore(database, snapshot_interval=2, schema_version="0.1")
        run_event = store.create_run("run-schema", actor_id="test", trace_id="trace-schema")
        task = TaskNodeView("run-schema", "node-a", "code", "compile")
        store.create_task(task, actor_id="test", trace_id="trace-schema")
        event = store.events(run_id="run-schema", task_id="node-a")[0]
        raw = event.to_dict()
        for field in (
            "event_id", "run_id", "node_id", "type", "payload", "timestamp",
            "trace_id", "parent_event_id", "actor_id", "schema_version",
        ):
            self.assertIn(field, raw)
        self.assertEqual(raw["parent_event_id"], run_event.event_id)
        snapshot = database.latest_snapshot("run-schema", "node-a")
        self.assertIsNotNone(snapshot)
        self.assertEqual(store.replay("run-schema", "node-a").task_id, "node-a")

    def test_idempotency_key_reuse_with_different_side_effect_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            database = MemoryDatabase()
            registry = CapabilityRegistry(database)
            registry.register(CapabilityProfile(
                "agent", ActorKind.AGENT, frozenset({"*"}),
                permissions=frozenset({"file.write"}), capabilities=frozenset({"*"}),
            ))
            executor = CountingExecutor()
            runtime = ToolRuntime(executor, registry, IdempotencyManager(database), settings(temp))
            first = ToolCall(
                "run", "task", "agent", "write_file", {"path": "x", "content": "v1"}, "key"
            )
            second = ToolCall(
                "run", "task", "agent", "write_file", {"path": "x", "content": "v2"}, "key"
            )
            self.assertTrue(runtime.execute(first)[0].success)
            result, _ = runtime.execute(second)
            self.assertFalse(result.success)
            self.assertIn("IdempotencyConflict", result.error or "")
            self.assertEqual(executor.calls, 1)

    def test_resource_monitor_updates_required_runtime_signals(self) -> None:
        database = MemoryDatabase()
        registry = CapabilityRegistry(database)
        registry.register(CapabilityProfile(
            "edge", ActorKind.DEVICE, frozenset({"*"}), capabilities=frozenset({"*"})
        ))
        monitor = ResourceMonitor(
            registry,
            lambda: {
                "edge": {
                    "cpu_percent": 60, "gpu_percent": 20, "memory_mb": 1024,
                    "queue_length": 3, "latency_ms": 42, "model_online": True,
                }
            },
            refresh_s=1,
        )
        monitor.refresh_once()
        profile = registry.get("edge")
        self.assertAlmostEqual(profile.current_load, 0.60)
        self.assertEqual(profile.latency_ms, 42)
        self.assertTrue(profile.online)
        self.assertEqual(profile.metadata["queue_length"], 3)

    def test_local_executor_rejects_windows_style_workspace_escape(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            service = ExecutionSchedulerService.memory(settings(temp))
            service.register_actor(CapabilityProfile(
                "agent", ActorKind.AGENT, frozenset({"*"}),
                capabilities=frozenset({"*"}), permissions=frozenset({"file.write"}),
            ))
            result, _ = service.execute_tool(ToolCall(
                "run", "task", "agent", "write_file",
                {"path": "..\\outside.txt", "content": "x"}, "escape-key"
            ))
            self.assertFalse(result.success)
            self.assertIn("escapes workspace", result.error or "")

    def test_create_run_rejects_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            service = ExecutionSchedulerService.memory(settings(temp))
            with self.assertRaisesRegex(ValueError, "cycle"):
                service.create_run([
                    {"task_id": "a", "required_skill": "code", "depends_on": ["b"]},
                    {"task_id": "b", "required_skill": "code", "depends_on": ["a"]},
                ])


if __name__ == "__main__":
    unittest.main()
