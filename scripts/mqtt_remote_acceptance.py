"""Authoritative in-memory-main-chain acceptance for a configured remote MQTT Agent.

This verifies a real cross-process/network PLAN_REQUEST/PLAN_RESPONSE, then commits
that remote Agent into the same OR-Tools assignment -> ToolRuntime -> Evidence ->
Verifier chain.  It does NOT claim LLM split inference or a physical tier merely
from configuration; the result only proves the configured remote RPC endpoint was
actually used by this run.
"""
from __future__ import annotations

import argparse
import json
import os
import time
import uuid
from pathlib import Path

from mosaic_omega.agent_runtime.edge_cloud import ExecutionTier
from mosaic_omega.agent_runtime.models import AgentProfile
from mosaic_omega.execution_scheduler.adapters.mqtt_agent import PahoMqttRpcClient
from mosaic_omega.execution_scheduler.models import ActorKind, CapabilityProfile
from mosaic_omega.goalspec import compile_goal
from mosaic_omega.integration.main_chain import MosaicMainChain


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--run-id", default=f"mqtt-remote-{uuid.uuid4().hex[:8]}")
    args = parser.parse_args()

    host = os.getenv("MQTT_HOST", "").strip()
    port = int(os.getenv("MQTT_PORT", "1883"))
    topic_prefix = os.getenv("MQTT_TOPIC_PREFIX", "mosaic/v3").strip() or "mosaic/v3"
    agent_id = os.getenv("MOSAIC_REMOTE_AGENT_ID", "").strip()
    endpoint_id = os.getenv("MOSAIC_REMOTE_ENDPOINT_ID", "").strip()
    tier_raw = os.getenv("MOSAIC_REMOTE_TIER", "edge").strip().casefold()
    if not host or not agent_id or not endpoint_id:
        raise RuntimeError("Remote endpoint environment is incomplete")
    tier = ExecutionTier(tier_raw)

    started = time.time()
    chain = MosaicMainChain(workspace=args.workspace, scheduler_policy="ortools")
    rpc = PahoMqttRpcClient(host=host, port=port, topic_prefix=topic_prefix, username=os.getenv("MQTT_USERNAME") or None, password=os.getenv("MQTT_PASSWORD") or None, tls=os.getenv("MQTT_TLS", "false").casefold() in {"1","true","yes","on"})
    try:
        profile = AgentProfile(
            agent_id=agent_id,
            name=f"remote {tier.value} agent",
            skills=("mqtt_task",),
            endpoint=f"mqtt://{endpoint_id}",
            max_load=1,
            reliability=0.95,
            tier=tier,
            labels=("mqtt", "remote-rpc", "acceptance"),
        )
        chain.register_mqtt_agent(profile, rpc=rpc, permissions=("*",), timeout_s=12.0)
        chain.execution.register_actor(CapabilityProfile(
            actor_id="remote-acceptance-model-contract",
            kind=ActorKind.MODEL,
            task_types=frozenset({"*"}),
            capabilities=frozenset({"*"}),
            permissions=frozenset({"*"}),
            reliability=0.98,
            context_limit=100_000,
            metadata={"truth": "capability contract only; remote Agent is the actual planner"},
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
            actor_id=f"{tier.value}-execution-resource",
            kind=ActorKind.DEVICE,
            task_types=frozenset({"*"}),
            capabilities=frozenset({"*"}),
            permissions=frozenset({"*"}),
            reliability=0.99,
            capacity=1,
            device_location=tier.value,
            metadata={"allowed_privacy_levels": ["normal", "public", "internal"]},
        ))

        goal = "通过已配置的远程 MQTT Agent 完成一个可验证的跨节点任务。"
        goalspec = compile_goal(goal, mode="rule")
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
            "resource_requirements": {"device_location": tier.value},
            "metadata": {"execution_tier": tier.value},
        }]
        graph = {"revision": 1, "scenario": "mqtt_remote_acceptance", "nodes": plan, "edges": []}
        result = chain.run_plan(
            goalspec=goalspec,
            taskgraph=graph,
            plan=plan,
            run_id=args.run_id,
            max_rounds=5,
        )
        task = result.tasks[0] if result.tasks else {}
        assignment = dict(task.get("assignment") or {})
        remote_events = [
            event for event in result.events
            if str(event.get("actor_id") or "") == agent_id
            or str((event.get("payload") or {}).get("agent_id") or "") == agent_id
        ]
        verified = bool(
            result.all_succeeded
            and assignment.get("agent_id") == agent_id
            and str(assignment.get("execution_tier") or tier.value).casefold() == tier.value
            and set(result.scheduler_policies) == {"ortools"}
        )
        report = {
            "schema": "mosaic.remote_endpoint_acceptance.v1",
            "run_id": result.run_id,
            "endpoint_id": endpoint_id,
            "configured_tier": tier.value,
            "transport": "mqtt_request_reply",
            "remote_agent_id": agent_id,
            "assignment_agent_id": assignment.get("agent_id"),
            "assignment_execution_tier": assignment.get("execution_tier"),
            "scheduler_policies": sorted(set(result.scheduler_policies)),
            "all_succeeded": result.all_succeeded,
            "verification_count": len(result.verification_results),
            "evidence_count": len(result.evidence_manifest),
            "remote_related_event_count": len(remote_events),
            "elapsed_ms": round((time.time() - started) * 1000.0, 1),
            "verdict": "REMOTE_RPC_SCHEDULER_VERIFIED" if verified else "NOT_VERIFIED",
            "verified": verified,
            "claim_boundary": "Proves this run used the configured remote MQTT Agent through authoritative OR-Tools assignment and local ToolRuntime/Verifier. Does not prove physical model-layer split inference.",
        }
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if verified else 2
    finally:
        rpc.close()


if __name__ == "__main__":
    raise SystemExit(main())
