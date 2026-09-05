from __future__ import annotations

import json
import os
import uuid

from mosaic_omega.execution_scheduler.adapters.mqtt_agent import PahoMqttRpcClient
from mosaic_omega.execution_scheduler.models import ActorKind, CapabilityProfile
from mosaic_omega.goalspec import compile_goal
from mosaic_omega.integration.production import build_production_chain, production_health
from mosaic_omega.agent_runtime.edge_cloud import ExecutionTier
from mosaic_omega.agent_runtime.models import AgentProfile


def main() -> int:
    chain = build_production_chain()
    rpc = PahoMqttRpcClient(
        host=os.getenv("MQTT_HOST", "127.0.0.1"),
        port=int(os.getenv("MQTT_PORT", "1883")),
        topic_prefix=os.getenv("MQTT_TOPIC_PREFIX", "mosaic/v3"),
        username=os.getenv("MQTT_USERNAME") or None,
        password=os.getenv("MQTT_PASSWORD") or None,
        tls=os.getenv("MQTT_TLS", "false").casefold() in {"1","true","yes","on"},
    )
    try:
        profile = AgentProfile(
            agent_id="mqtt-agent-1",
            name="MQTT edge planner",
            skills=("mqtt_task",),
            endpoint="mqtt://mqtt-agent-1",
            max_load=1,
            reliability=0.95,
            tier=ExecutionTier.EDGE,
            labels=("mqtt", "smoke"),
        )
        chain.register_mqtt_agent(profile, rpc=rpc, permissions=("*",), timeout_s=10)
        chain.execution.register_actor(CapabilityProfile(
            actor_id="mqtt-model",
            kind=ActorKind.MODEL,
            task_types=frozenset({"*"}),
            capabilities=frozenset({"*"}),
            permissions=frozenset({"*"}),
            reliability=0.98,
            context_limit=100_000,
        ))
        chain.execution.register_actor(CapabilityProfile(
            actor_id="task",
            kind=ActorKind.TOOL,
            task_types=frozenset({"*"}),
            capabilities=frozenset({"*"}),
            permissions=frozenset({"*"}),
            reliability=0.99,
        ))
        chain.execution.register_actor(CapabilityProfile(
            actor_id="edge-device",
            kind=ActorKind.DEVICE,
            task_types=frozenset({"*"}),
            capabilities=frozenset({"*"}),
            permissions=frozenset({"*"}),
            reliability=0.99,
            capacity=1,
            device_location="edge",
            metadata={"allowed_privacy_levels": ["normal", "public", "internal"]},
        ))

        goalspec = compile_goal("通过远程 MQTT Agent 完成一个可验证任务。", mode="rule")
        plan = [{
            "task_id": "mqtt_task",
            "node_id": "mqtt_task",
            "type": "mqtt_task",
            "description": "MQTT remote task",
            "predecessors": [],
            "required_capabilities": ["mqtt_task"],
            "required_permissions": [],
            "acceptance": ["contains:MQTT remote task"],
            "risk": "normal",
            "inputs": {},
            "outputs": {},
            "evidence_dependencies": [],
            "resource_requirements": {"device_location": "edge"},
            "metadata": {},
        }]
        graph = {"revision": 1, "scenario": "mqtt_smoke", "nodes": plan, "edges": []}
        result = chain.run_plan(
            goalspec=goalspec,
            taskgraph=graph,
            plan=plan,
            run_id=f"mqtt-smoke-{uuid.uuid4().hex[:8]}",
            max_rounds=5,
        )
        health = production_health(chain)
        assigned_agent = result.tasks[0].get("assignment", {}).get("agent_id") if result.tasks else None
        ready = bool(
            health.ready
            and result.all_succeeded
            and assigned_agent == "mqtt-agent-1"
            and set(result.scheduler_policies) == {"ortools"}
        )
        report = {
            "health": health.to_dict(),
            "run_id": result.run_id,
            "all_succeeded": result.all_succeeded,
            "assigned_agent": assigned_agent,
            "scheduler_policies": sorted(set(result.scheduler_policies)),
            "evidence_count": len(result.evidence_manifest),
            "verification_count": len(result.verification_results),
            "ready": ready,
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if ready else 2
    finally:
        rpc.close()


if __name__ == "__main__":
    raise SystemExit(main())
