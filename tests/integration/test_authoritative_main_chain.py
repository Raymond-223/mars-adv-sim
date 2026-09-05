from __future__ import annotations

from mosaic_omega.integration import MosaicMainChain
from mosaic_omega.agent_runtime.models import AgentProfile, AgentStatus
from mosaic_omega.agent_runtime.edge_cloud import ExecutionTier
from test_support.mock_mainchain import run_test_mock


GOAL_FIELDS = {
    "goal_id", "objective", "hard_constraints", "soft_preferences",
    "acceptance_predicates", "budgets", "privacy_level", "source_spans",
}
TASK_FIELDS = {
    "node_id", "type", "inputs", "outputs", "predecessors",
    "evidence_dependencies", "resource_requirements", "risk", "status", "acceptance",
}
EVENT_FIELDS = {
    "event_id", "run_id", "node_id", "type", "payload", "timestamp", "trace_id",
    "parent_event_id", "actor_id", "model_id", "schema_version",
}
EVIDENCE_FIELDS = {
    "evidence_id", "uri", "hash", "producer", "node_id", "mime_type", "scope",
    "created_at", "verification_status",
}
CAPABILITY_FIELDS = {
    "actor_id", "capabilities", "reliability", "cost", "latency", "context_limit",
    "permissions", "device_location", "posterior",
}
MESSAGE_FIELDS = {
    "message_id", "sender", "receiver", "topic", "priority", "ttl", "budget", "summary",
    "evidence_refs", "content_hash", "causal_parent",
}
TOPOLOGY_FIELDS = {
    "version", "nodes", "edges", "edge_scores", "lambda2_or_connectivity",
    "effective_from", "min_hold_time",
}
VERIFICATION_FIELDS = {
    "target_id", "passed", "predicate_results", "confidence", "evidence_refs", "risk_level", "action",
}


def test_handbook_main_chain_and_contracts(tmp_path):
    chain = MosaicMainChain(workspace=tmp_path / "workspace")
    result = run_test_mock(
        chain,
        "修复 ROS 仓库，必须通过测试，不得修改公共接口。",
        run_id="contract-e2e",
    )

    assert result.all_succeeded
    assert result.tasks
    assert len(result.verification_results) == len(result.tasks)
    assert len(result.evidence_manifest) == len(result.tasks)
    assert result.communication
    assert result.context_packs

    assert GOAL_FIELDS <= result.canonical_goalspec.keys()
    assert all(TASK_FIELDS <= item.keys() for item in result.tasks)
    assert all(EVENT_FIELDS <= item.keys() for item in result.events)
    assert all(EVIDENCE_FIELDS <= item.keys() for item in result.evidence_manifest)
    assert all(CAPABILITY_FIELDS <= item.keys() for item in result.capability_profiles)
    assert all(MESSAGE_FIELDS <= item.keys() for item in result.communication)
    assert TOPOLOGY_FIELDS <= result.topology_snapshot.keys()
    assert all(VERIFICATION_FIELDS <= item.keys() for item in result.verification_results)

    # Handbook unified trace fields must not disappear at an adapter boundary.
    for event in result.events:
        for field in ("run_id", "trace_id", "actor_id", "model_id", "timestamp", "schema_version"):
            assert event[field] not in (None, "")

    # A multi-capability final milestone must remain schedulable without weakening
    # its TaskNode requirements; the bridge supplies a multi-skill dynamic agent.
    final = next(item for item in result.tasks if item["node_id"] == "task_final_review")
    assert final["status"] == "SUCCEEDED"
    assert final["assignment"] is not None


def test_dynamic_registry_bridge_updates_scheduler_liveness(tmp_path):
    chain = MosaicMainChain(workspace=tmp_path / "workspace")
    profile = AgentProfile(
        agent_id="external-reviewer",
        name="External Reviewer",
        skills=("review",),
        endpoint="mqtt://external-reviewer",
        max_load=2,
        reliability=0.97,
        tier=ExecutionTier.EDGE,
    )
    saved = chain.registry_bridge.register(profile, permissions=("read",))
    assert saved.actor_id == profile.agent_id
    assert saved.capabilities == frozenset({"review"})
    assert saved.online

    chain.registry_bridge.heartbeat(
        profile.agent_id,
        status=AgentStatus.ONLINE,
        current_load=1,
        latency_ms=12.0,
    )
    current = chain.execution.capabilities.get(profile.agent_id)
    assert current.online
    assert current.current_load == 0.5
    assert current.latency_ms == 12.0

    chain.registry_bridge.offline(profile.agent_id)
    assert chain.execution.capabilities.get(profile.agent_id).online is False


def test_evidence_invalidation_replans_only_execution_impact_closure(tmp_path):
    chain = MosaicMainChain(workspace=tmp_path / "workspace", scheduler_policy="greedy")
    result = run_test_mock(chain, "修复 ROS 仓库，必须通过测试，不得修改公共接口。", run_id="invalidate-e2e")
    assert result.all_succeeded
    first_task = result.completed_task_ids[0]
    first_evidence = next(item for item in result.evidence_manifest if item["node_id"] == first_task)

    plan = chain.invalidate_evidence(result.run_id, first_evidence["evidence_id"], reason="test invalidation")
    assert first_task in plan["affected_task_ids"]
    states = {task.task_id: task.state.value for task in chain.execution.events.tasks(result.run_id)}
    assert states[first_task] == "READY"
    # Unaffected completed nodes, if any, remain completed; affected descendants
    # are reset to PLANNED and are not re-executed before the root succeeds again.
    for task_id in plan["affected_task_ids"]:
        if task_id != first_task:
            assert states[task_id] == "PLANNED"
    assert any(
        event.event_type == "TASK_REPLANNED"
        for event in chain.execution.events.events(run_id=result.run_id)
    )
