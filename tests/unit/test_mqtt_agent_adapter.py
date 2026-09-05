from __future__ import annotations

from mosaic_omega.execution_scheduler.adapters.mqtt_agent import MqttAgentAdapter
from mosaic_omega.execution_scheduler.models import Assignment, TaskNodeView


class FakeRpc:
    def __init__(self):
        self.request_seen = None

    def request(self, agent_id, message, timeout_s):
        self.request_seen = (agent_id, message, timeout_s)
        return {
            "correlation_id": message["correlation_id"],
            "tool_calls": [{
                "tool_name": "task",
                "arguments": {"description": "remote planned"},
            }],
        }

    def close(self):
        pass


def test_real_mqtt_adapter_contract_keeps_toolruntime_authoritative() -> None:
    rpc = FakeRpc()
    adapter = MqttAgentAdapter("agent-edge-1", rpc, timeout_s=3)
    task = TaskNodeView("run-1", "node-1", "general", "do work")
    assignment = Assignment(
        task_id="node-1",
        agent_id="agent-edge-1",
        model_id="model",
        tool_id="task",
        resource_id="edge",
        total_cost=1.0,
        cost_breakdown={},
        policy="ortools",
        reason="test",
        run_id="run-1",
    )
    calls = adapter.plan(task, assignment, "trace-1")
    assert len(calls) == 1
    call = calls[0]
    assert call.actor_id == "agent-edge-1"
    assert call.run_id == "run-1"
    assert call.task_id == "node-1"
    assert call.tool_name == "task"
    assert call.trace_id == "trace-1"
    _, request, _ = rpc.request_seen
    assert request["type"] == "PLAN_REQUEST"
    assert request["payload"]["assignment"]["agent_id"] == "agent-edge-1"
