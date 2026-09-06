"""Regressions for the dynamic heterogeneous group behaviour.

These cover the defects that made the system look like a static role pipeline:
one Agent per role, Agent concurrency ignored by the solver, every non-running
task collapsed into a single "unassigned" label, topology history discarded, and
one provider round trip per acceptance condition.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from mosaic_omega.execution_scheduler.adapters import deepseek_agent as deepseek_module
from mosaic_omega.execution_scheduler.adapters.postgres import MemoryDatabase
from mosaic_omega.execution_scheduler.capability import CapabilityRegistry
from mosaic_omega.execution_scheduler.config import Settings
from mosaic_omega.execution_scheduler.cost_model import CostModel
from mosaic_omega.execution_scheduler.models import (
    ActorKind,
    CapabilityProfile,
    ExecutionResult,
    TaskNodeView,
    TaskState,
)
from mosaic_omega.execution_scheduler.scheduler import Scheduler
from mosaic_omega.integration import MosaicMainChain
from mosaic_omega.observability.projections import build_dashboard_snapshot
from mosaic_omega.observability.snapshots import SnapshotStore
from mosaic_omega.verifier import semantic as semantic_module
from mosaic_omega.verifier.service import VerifierService


class _StubCompletions:
    def __init__(self) -> None:
        self.agent_calls = 0
        self.verifier_calls = 0
        self.verifier_batch_sizes: list[int] = []

    def create(self, **kwargs):
        messages = kwargs.get("messages") or []
        is_verifier = any(
            "independent acceptance verifier" in str(item.get("content", "")).casefold()
            for item in messages if isinstance(item, dict)
        )
        if is_verifier:
            self.verifier_calls += 1
            conditions = json.loads(messages[-1]["content"])["acceptance_conditions"]
            self.verifier_batch_sizes.append(len(conditions))
            content = json.dumps({"judgments": [
                {"id": item["id"], "passed": True, "rationale": "stub verified deliverable"}
                for item in conditions
            ]})
        else:
            self.agent_calls += 1
            content = "本节点成果：完成分析并给出结论。"
        return SimpleNamespace(
            id=f"stub-{self.agent_calls + self.verifier_calls}",
            model=kwargs["model"],
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )


class _StubClient:
    _mosaic_transport = "test_fixture"

    def __init__(self) -> None:
        self.completions = _StubCompletions()
        self.chat = SimpleNamespace(completions=self.completions)


@pytest.fixture()
def stub_provider(monkeypatch):
    client = _StubClient()
    monkeypatch.setenv("DEEPSEEK_API_KEY", "stub-key-not-a-secret")
    monkeypatch.setattr(deepseek_module, "create_openai_compatible_client", lambda **_: client)
    monkeypatch.setattr(semantic_module, "create_openai_compatible_client", lambda **_: client)
    return client


GOAL = (
    "完成跨域投研分析并交付报告。验收条件：报告包含三个数据源的分析结论；包含风险模型结果；"
    "包含合规审核结果；每个结论绑定证据引用；输出结构化报告文件。"
)


def _registry(agent_specs: list[tuple[str, int, str]], resources: list[tuple[str, int, str]]):
    registry = CapabilityRegistry(MemoryDatabase())
    common = dict(
        task_types=frozenset({"*"}), capabilities=frozenset({"*"}), permissions=frozenset({"*"})
    )
    for actor_id, capacity, tier in agent_specs:
        registry.register(CapabilityProfile(
            actor_id, ActorKind.AGENT, capacity=capacity, metadata={"tier": tier}, **common
        ))
    registry.register(CapabilityProfile("model", ActorKind.MODEL, **common))
    registry.register(CapabilityProfile("tool", ActorKind.TOOL, **common))
    for actor_id, capacity, tier in resources:
        registry.register(CapabilityProfile(
            actor_id, ActorKind.DEVICE, capacity=capacity, device_location=tier, **common
        ))
    return registry


def _ready(count: int, **kwargs) -> list[TaskNodeView]:
    return [
        TaskNodeView(
            run_id="run", task_id=f"task-{i:02d}", task_type="general",
            description="work", state=TaskState.READY, **kwargs,
        )
        for i in range(count)
    ]


# --------------------------------------------------------------- scheduler


def test_same_role_agents_share_a_ready_layer_instead_of_one_saturated_agent(tmp_path):
    """Three single-slot Agents must take one task each, not one Agent taking all."""
    pytest.importorskip("ortools")
    settings = Settings.from_env({"EXECUTION_WORKSPACE": str(tmp_path), "SCHEDULER_POLICY": "ortools"})
    registry = _registry(
        [("analyst-01", 1, "cloud"), ("analyst-02", 1, "cloud"), ("analyst-03", 1, "cloud")],
        [("pool-cloud", 8, "cloud")],
    )
    scheduler = Scheduler(registry, CostModel(settings), settings)

    assignments = scheduler.assign_tasks(_ready(3))

    assert sorted(item.agent_id for item in assignments) == ["analyst-01", "analyst-02", "analyst-03"]


def test_scheduler_reports_no_feasible_agent_separately_from_queued(tmp_path):
    pytest.importorskip("ortools")
    settings = Settings.from_env({"EXECUTION_WORKSPACE": str(tmp_path), "SCHEDULER_POLICY": "ortools"})
    registry = _registry([("cloud-01", 1, "cloud")], [("pool-cloud", 4, "cloud")])
    scheduler = Scheduler(registry, CostModel(settings), settings)

    # Two identical READY tasks, one slot: exactly one must be QUEUED with candidates.
    scheduler.assign_tasks(_ready(2))
    states = {item.task_id: item for item in scheduler.last_round.diagnostics}
    assert sorted(item.state for item in states.values()) == ["ASSIGNED", "QUEUED"]
    queued = next(item for item in states.values() if item.state == "QUEUED")
    assert queued.candidates, "a queued task must still expose the candidates it is waiting for"

    # A task pinned to a tier with no Agent is infeasible, not queued.
    pinned = _ready(1, resource_requirements={"allowed_tiers": ["device"]})
    scheduler.assign_tasks(pinned)
    diagnostic = scheduler.last_round.diagnostics[0]
    assert diagnostic.state == "NO_FEASIBLE_AGENT"
    assert diagnostic.rejected, "elimination reasons must be recorded, not just an empty candidate set"
    assert any("allowed_tiers" in reason for item in diagnostic.rejected for reason in item["reasons"])


def test_agent_tier_cannot_execute_on_a_foreign_resource_pool(tmp_path):
    settings = Settings.from_env({"EXECUTION_WORKSPACE": str(tmp_path)})
    registry = _registry(
        [("cloud-01", 1, "cloud"), ("device-01", 1, "device")],
        [("pool-cloud", 4, "cloud"), ("pool-device", 2, "device")],
    )
    scheduler = Scheduler(registry, CostModel(settings), settings)

    candidates, _rejected = scheduler.candidates_for(_ready(1)[0])

    pairs = {(item.agent_id, item.resource_id) for item in candidates.values()}
    assert pairs == {("cloud-01", "pool-cloud"), ("device-01", "pool-device")}


# ------------------------------------------------------------- projection


def _snapshot(tasks, events, **kwargs):
    return build_dashboard_snapshot(
        run_id="run", phase="test", tasks=tasks, events=events, capabilities=[],
        communication=[], communication_decisions=[], context_packs={}, memory_records=[],
        memory_metrics={}, topology_snapshot={}, topology_telemetry={}, metric_snapshot={},
        **kwargs,
    )


def test_blocked_queued_and_infeasible_are_distinct_projection_states():
    tasks = [
        {"task_id": "parent", "status": "RUNNING", "predecessors": []},
        {"task_id": "child", "status": "PLANNED", "predecessors": ["parent"]},
        {"task_id": "waiting", "status": "READY", "predecessors": []},
        {"task_id": "stuck", "status": "READY", "predecessors": []},
    ]
    rounds = [{
        "round_index": 1,
        "tasks": [
            {"task_id": "waiting", "state": "QUEUED", "reason": "no free slot", "candidate_count": 2},
            {"task_id": "stuck", "state": "NO_FEASIBLE_AGENT", "reason": "all eliminated", "candidate_count": 0},
        ],
    }]

    snapshot = _snapshot(tasks, [], scheduling_rounds=rounds)

    states = {node["id"]: node["scheduling_state"] for node in snapshot["task_graph"]["nodes"]}
    assert states == {
        "parent": "RUNNING", "child": "BLOCKED", "waiting": "QUEUED", "stuck": "NO_FEASIBLE_AGENT"
    }
    blocked = next(n for n in snapshot["task_graph"]["nodes"] if n["id"] == "child")
    assert blocked["scheduling"]["blocking_task_ids"] == ["parent"]
    assert snapshot["scheduling"]["infeasible_task_ids"] == ["stuck"]
    assert snapshot["scheduling"]["queued_task_ids"] == ["waiting"]


def test_topology_history_reaches_the_snapshot():
    history = [
        {"version": 1, "nodes": ["a", "b"], "edges": [], "connected": True},
        {"version": 2, "nodes": ["a", "b"], "edges": [{"source": "a", "target": "b"}], "connected": True},
    ]

    snapshot = _snapshot([], [], topology_history=history)

    assert snapshot["topology"]["version_count"] == 2
    assert [item["version"] for item in snapshot["topology"]["history"]] == [1, 2]


def test_intra_agent_handoff_is_recorded_but_not_counted_as_a_message():
    decisions = [
        {"policy_action": "SEND", "message_id": "m1"},
        {"policy_action": "INTERNAL", "message_id": None, "policy_reason": "same agent"},
    ]

    snapshot = build_dashboard_snapshot(
        run_id="run", phase="test", tasks=[], events=[], capabilities=[],
        communication=[], communication_decisions=decisions, context_packs={},
        memory_records=[], memory_metrics={}, topology_snapshot={},
        topology_telemetry={}, metric_snapshot={},
    )

    assert snapshot["communication"]["total"] == 1
    assert snapshot["communication"]["internal_handoff_count"] == 1
    assert snapshot["communication"]["decision_count"] == 2


# --------------------------------------------------------------- verifier


def _result(deliverable: str | None = None) -> ExecutionResult:
    metadata = {"deliverable_relative": deliverable} if deliverable else {}
    return ExecutionResult(call_id="c1", success=True, output="done", exit_code=0, metadata=metadata)


def _evidence(content: str = "done"):
    import hashlib

    from mosaic_omega.execution_scheduler.models import Evidence
    # Digest must match what the verifier recomputes, otherwise the integrity
    # predicate fails for reasons unrelated to the test.
    digest = hashlib.sha256(f"{content}\n".encode("utf-8")).hexdigest()
    return (Evidence(run_id="run", task_id="t", kind="tool_execution", digest=digest, content=content),)


def test_mechanically_checkable_conditions_never_reach_the_semantic_judge(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    (workspace / "out").mkdir(parents=True)
    (workspace / "out" / "report.md").write_text("content", encoding="utf-8")
    verifier = VerifierService(workspace)
    called = []
    monkeypatch.setattr(
        verifier.semantic, "judge_batch",
        lambda **kwargs: called.append(kwargs["predicates"]) or {},
    )
    task = TaskNodeView(
        run_id="run", task_id="t", task_type="report", description="d",
        acceptance_conditions=("必须输出报告文件", "必须绑定证据"),
        metadata={"acceptance_predicates": [
            {"condition": "必须输出报告文件", "check_type": "file_check"},
            {"condition": "必须绑定证据", "predicate": "evidence_present"},
        ]},
    )

    result = verifier.verify(task, _result("out/report.md"), _evidence())

    assert called == [], "file_check/evidence_present must be decided by rule"
    assert result.passed
    assert result.metadata["rule_checked_condition_count"] == 2
    assert result.metadata["semantic_check_count"] == 0


def test_semantic_conditions_are_judged_in_one_batched_request(tmp_path, monkeypatch):
    verifier = VerifierService(tmp_path)
    batches = []

    def fake_batch(*, task, predicates, result, evidence):
        batches.append(predicates)
        return {p: {"passed": True, "rationale": "ok", "request_id": "r1"} for p in predicates}

    monkeypatch.setattr(verifier.semantic, "judge_batch", fake_batch)
    task = TaskNodeView(
        run_id="run", task_id="t", task_type="report", description="d",
        acceptance_conditions=("结论一致", "论证充分", "风险说明完整"),
    )

    result = verifier.verify(task, _result(), _evidence())

    assert len(batches) == 1, "one provider round trip per task, not one per condition"
    assert len(batches[0]) == 3
    assert result.metadata["semantic_check_count"] == 3
    assert result.metadata["semantic_request_count"] == 1


def test_restricted_task_content_is_never_sent_to_the_cloud_verifier(tmp_path, monkeypatch):
    verifier = VerifierService(tmp_path)
    monkeypatch.setattr(
        verifier.semantic, "judge_batch",
        lambda **kwargs: pytest.fail("restricted deliverable must not leave the device"),
    )
    task = TaskNodeView(
        run_id="run", task_id="t", task_type="analysis", description="d",
        privacy_level="restricted", acceptance_conditions=("结论必须完整",),
    )

    result = verifier.verify(task, _result(), _evidence())

    assert result.metadata["assurance"] == "reduced_privacy_sealed"
    assert result.metadata["privacy_withheld_conditions"] == ["结论必须完整"]


# ------------------------------------------------------------- end to end


def test_run_builds_a_competing_agent_pool_and_publishes_its_decisions(tmp_path, stub_provider):
    workspace = tmp_path / "workspace"
    chain = MosaicMainChain(workspace=workspace, scheduler_policy="ortools")

    result = chain.run(GOAL, run_id="pool-e2e", goalspec_mode="rule", agent_mode="deepseek")
    assert result.all_succeeded

    agents = [
        item["actor_id"] for item in result.capability_profiles if item["kind"] == "agent"
    ]
    # Every automatically created role must have at least two instances so
    # same-role Agents can compete, run in parallel and cover for each other.
    roles: dict[str, int] = {}
    for actor_id in agents:
        if not actor_id.startswith("agent-deepseek-") or actor_id[-3:-2] != "-":
            continue
        role = actor_id[:-3]
        if role.endswith("generalist-standby"):
            continue  # the standby is intentionally a single instance
        roles[role] = roles.get(role, 0) + 1
    assert roles, "expected instance-suffixed role pool members"
    assert all(count >= 2 for count in roles.values()), roles

    snapshot = SnapshotStore(workspace / "observability").read_latest()
    assert snapshot["scheduling"]["round_count"] >= 2
    assert snapshot["topology"]["history"], "topology replay history must be published"
    assert snapshot["performance"]["sample_count"] == len(result.tasks)

    # More than one distinct Agent instance actually executed work.
    used = {item["agent_id"] for item in snapshot["scheduler"]["assignments"]}
    assert len(used) >= 3, used

    # The requirement baseline is compiled on-device and costs no provider call.
    assert "agent-local-requirement-compiler" in used
    assert snapshot["authenticity"]["local_execution_count"] >= 1

    # Every acceptance condition of a task is judged in a single batched request.
    assert all(size >= 1 for size in stub_provider.completions.verifier_batch_sizes)
    assert stub_provider.completions.verifier_calls <= len(result.tasks)


# ------------------------------------------------------- real tool execution


def _planner(monkeypatch, response_text: str):
    from mosaic_omega.execution_scheduler.adapters.tool_planning_agent import ToolPlanningAgent

    class _Client:
        _mosaic_transport = "test_fixture"

        def __init__(self):
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: SimpleNamespace(
                id="req-1", model=kw["model"],
                choices=[SimpleNamespace(message=SimpleNamespace(content=response_text))],
                usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            )))

    monkeypatch.setenv("DEEPSEEK_API_KEY", "stub")
    monkeypatch.setattr(deepseek_module, "create_openai_compatible_client", lambda **_: _Client())
    return ToolPlanningAgent("agent-x", role="code", delivery_kinds=("software",))


def _software_task() -> TaskNodeView:
    return TaskNodeView(
        run_id="run", task_id="t", task_type="code", description="fix the module",
        metadata={"delivery_kind": "software"},
        required_permissions=frozenset({"file.write", "shell.execute"}),
    )


def _assignment():
    from mosaic_omega.execution_scheduler.models import Assignment
    return Assignment(
        task_id="t", agent_id="agent-x", model_id="m", tool_id="task", resource_id="pool-cloud",
        total_cost=1.0, cost_breakdown={}, policy="ortools", reason="",
    )


def test_tool_plan_becomes_real_tool_calls(monkeypatch):
    agent = _planner(monkeypatch, json.dumps({
        "steps": [
            {"tool": "write_file", "arguments": {"path": "pkg/calc.py", "content": "x = 1\n"}},
            {"tool": "test", "arguments": {}},
        ],
        "deliverable_markdown": "# done",
    }))

    calls = agent.plan(_software_task(), _assignment(), "trace-1")

    assert [c.tool_name for c in calls] == ["write_file", "test", "write_file"]
    # Exactly one model request means exactly one api_provenance record.
    assert sum(1 for c in calls if "api_provenance" in c.arguments) == 1
    assert calls[0].arguments["execution_intent"] == "act_on_environment"
    assert calls[-1].arguments["execution_intent"] == "persist_planner_output"
    assert calls[-1].arguments["path"] == ".mosaic_deliverables/run/t.md"


def test_tool_plan_validator_rejects_unsafe_or_out_of_menu_steps(monkeypatch):
    agent = _planner(monkeypatch, json.dumps({
        "steps": [
            {"tool": "write_file", "arguments": {"path": "../../etc/passwd", "content": "x"}},
            {"tool": "write_file", "arguments": {"path": "/etc/passwd", "content": "x"}},
            {"tool": "shell", "arguments": {"command": "rm -rf /"}},
            {"tool": "definitely_not_a_tool", "arguments": {}},
            {"tool": "write_file", "arguments": {"path": "ok.txt"}},
        ],
        "deliverable_markdown": "# done",
    }))

    calls = agent.plan(_software_task(), _assignment(), "trace-1")

    # Only the deliverable survives; every unsafe or malformed step is dropped.
    assert [c.tool_name for c in calls] == ["write_file"]
    assert calls[0].arguments["path"] == ".mosaic_deliverables/run/t.md"
    rejected = calls[0].arguments["api_provenance"]["rejected_steps"]
    assert len(rejected) == 5
    reasons = " ".join(item["reason"] for item in rejected)
    assert "escapes the workspace" in reasons
    assert "list of strings" in reasons
    assert "not in the menu" in reasons


def test_reasoning_tasks_still_persist_output_without_planning_tools(monkeypatch):
    agent = _planner(monkeypatch, "plain reasoning output")
    task = TaskNodeView(
        run_id="run", task_id="t", task_type="plan", description="think",
        metadata={"delivery_kind": "reasoning"},
    )

    calls = agent.plan(task, _assignment(), "trace-1")

    assert [c.tool_name for c in calls] == ["task"]
    assert calls[0].arguments["execution_intent"] == "persist_planner_output"


def test_multi_step_plan_keeps_the_node_deliverable_visible_to_the_verifier():
    from mosaic_omega.execution_scheduler.orchestrator import _combined_metadata

    results = [
        ExecutionResult(call_id="a", success=True, metadata={"deliverable_relative": "work/script.py"}),
        ExecutionResult(call_id="b", success=True, metadata={}),
        ExecutionResult(call_id="c", success=True, metadata={"deliverable_relative": ".mosaic_deliverables/run/t.md"}),
    ]

    metadata = _combined_metadata(results, results[0])

    # The node's own artifact is the last one its plan produced.
    assert metadata["deliverable_relative"] == ".mosaic_deliverables/run/t.md"
    assert metadata["deliverable_relatives"] == ["work/script.py", ".mosaic_deliverables/run/t.md"]


def test_explicit_acceptance_enumeration_is_not_silently_dropped():
    from mosaic_omega.goalspec import compile_goal

    spec = compile_goal(
        "修复并实现计算模块的代码缺陷。验收条件：实现代码并通过 pytest 单元测试；完成构建编译；输出结构化报告文件。",
        mode="rule",
    )

    conditions = [item["condition"] for item in spec["acceptance_conditions"]]
    # Each enumerated clause becomes its own acceptance condition even though
    # only one of them happens to contain a keyword like "输出".
    assert "实现代码并通过 pytest 单元测试" in conditions
    assert "完成构建编译" in conditions
    assert "输出结构化报告文件" in conditions


def test_software_nodes_request_execution_permissions_and_document_nodes_do_not():
    from mosaic_omega.goalspec import compile_goal
    from mosaic_omega.todag import ToDAGEngine

    engine = ToDAGEngine()
    engine.build(compile_goal(
        "修复模块缺陷。验收条件：实现代码并通过 pytest 单元测试；输出结构化报告文件。", mode="rule",
    ))
    nodes = {n["task_id"]: n for n in engine.snapshot()["nodes"]}

    software = [n for n in nodes.values() if n["delivery_kind"] == "software"]
    documents = [n for n in nodes.values() if n["delivery_kind"] == "document"]
    assert software and documents
    assert all("test.execute" in n["required_permissions"] for n in software)
    assert all("shell.execute" not in n["required_permissions"] for n in documents)


# --------------------------------------------------- memory pipeline / DAG


def test_context_pack_records_its_own_selection_pipeline(tmp_path, stub_provider):
    chain = MosaicMainChain(workspace=tmp_path / "workspace", scheduler_policy="greedy")
    result = chain.run(GOAL, run_id="mem-trace", goalspec_mode="rule", agent_mode="deepseek")
    assert result.all_succeeded

    packs = list(result.context_packs.values())
    assert packs
    trace = packs[0]["selection_trace"]
    # Full history -> candidates -> filter -> ranking -> limit -> compression.
    assert trace["pipeline"][0] == "full_history"
    assert trace["full_history"]["record_count"] >= 1
    assert trace["raw_candidate_count"] >= trace["deduplicated_candidate_count"]
    assert {stage["stage"] for stage in trace["stages"]} >= {
        "taskgraph_neighbourhood", "semantic_core", "task_scoped_history"
    }
    assert trace["ranked"], "ranking must expose per-record scores, not just the winners"
    assert all("score" in row and "selected" in row for row in trace["ranked"])
    assert "compression" in trace

    snapshot = SnapshotStore(tmp_path / "workspace" / "observability").read_latest()
    pipeline = snapshot["memory"]["pipeline"]
    assert pipeline["sample_count"] == len(packs)
    assert pipeline["totals"]["selected"] >= 1
    assert pipeline["ranking_formula"]


def test_dropped_memories_carry_an_explicit_reason():
    from mosaic_omega.memory_recovery import MemoryService
    from mosaic_omega.memory_recovery.models import MemoryEvent, MemoryEventType

    service = MemoryService()
    for index in range(30):
        service.ingest_event(MemoryEvent(
            event_type=MemoryEventType.CUSTOM, run_id="r", task_id="t", node_id="t",
            content=f"fact {index}", source="test",
        ))
    pack = service.build_context_pack(run_id="r", node_id="t", task_id="t", query="fact")

    trace = pack.selection_trace
    dropped = trace["dropped_by_recall_limit"]
    assert dropped, "with more candidates than the recall limit, some must be reported as dropped"
    assert all("ranked below the recall limit" in row["drop_reason"] for row in dropped)


def test_task_graph_exposes_layers_and_a_measured_critical_path():
    tasks = [
        {"task_id": "a", "status": "SUCCEEDED", "predecessors": []},
        {"task_id": "b", "status": "SUCCEEDED", "predecessors": ["a"]},
        {"task_id": "c", "status": "SUCCEEDED", "predecessors": ["a"]},
        {"task_id": "d", "status": "SUCCEEDED", "predecessors": ["b", "c"]},
    ]
    events = [
        {"run_id": "r", "type": "TASK_PHASE_TIMING", "task_id": task, "timestamp": 1.0,
         "payload": {"timings_ms": {"total_ms": ms}}}
        for task, ms in (("a", 10.0), ("b", 5.0), ("c", 500.0), ("d", 10.0))
    ]

    snapshot = _snapshot(tasks, events)

    graph = snapshot["task_graph"]
    levels = {node["id"]: node["level"] for node in graph["nodes"]}
    # b and c are parallel: same layer, so the UI can show them side by side.
    assert levels == {"a": 0, "b": 1, "c": 1, "d": 2}
    assert graph["layer_count"] == 3
    # The critical path follows measured duration, so it goes through c, not b.
    assert graph["critical_path"] == ["a", "c", "d"]
    assert "measured" in graph["critical_path_source"]
    on_path = {node["id"] for node in graph["nodes"] if node["on_critical_path"]}
    assert on_path == {"a", "c", "d"}
