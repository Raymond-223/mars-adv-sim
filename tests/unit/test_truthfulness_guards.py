from __future__ import annotations

from pathlib import Path

import pytest

from mosaic_omega.execution_scheduler.adapters.llm_agent import LLMAgentAdapter
from mosaic_omega.execution_scheduler.models import Assignment, TaskNodeView
from mosaic_omega.integration import MosaicMainChain
from mosaic_omega.observability.snapshots import SnapshotStore
from test_support.mock_mainchain import run_test_mock


def _task() -> TaskNodeView:
    return TaskNodeView(
        run_id="truth-run",
        task_id="task-1",
        task_type="general",
        description="truth test",
        acceptance_conditions=("ok",),
        metadata={"tool": {"name": "task", "arguments": {"description": "ok"}}},
    )


def _assignment() -> Assignment:
    return Assignment(
        task_id="task-1",
        agent_id="agent-1",
        model_id="model-1",
        tool_id="task",
        resource_id="local-device",
        total_cost=0.0,
        cost_breakdown={},
        policy="test",
        reason="test",
    )


def test_llm_adapter_strict_mode_never_silently_falls_back(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    adapter = LLMAgentAdapter("agent-1", allow_fallback=False)
    with pytest.raises(RuntimeError, match="禁止自动降级"):
        adapter.plan(_task(), _assignment(), "trace-1")


def test_explicit_developer_fallback_is_marked_not_real(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    adapter = LLMAgentAdapter("agent-1", allow_fallback=True)
    call = adapter.plan(_task(), _assignment(), "trace-1")[0]
    assert adapter.authenticity_mode == "api_with_explicit_fallback"
    assert call.arguments["execution_provenance"]["mode"] == "explicit_deterministic_fallback"
    assert "api_provenance" not in call.arguments


def test_mock_run_snapshot_is_explicitly_rejected_as_real_agent(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    chain = MosaicMainChain(workspace=workspace, scheduler_policy="greedy")
    run_test_mock(chain, "生成报告，必须验收。", run_id="mock-truth-test")
    snap = SnapshotStore(workspace / "observability").read_latest()
    assert snap["authenticity"]["verdict"] == "MOCK_EXECUTION"
    assert snap["authenticity"]["competition_strict_real_agent"] is False
    assert snap["authenticity"]["mock_agents"]


def test_task_status_has_authoritative_event_provenance(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    chain = MosaicMainChain(workspace=workspace, scheduler_policy="greedy")
    run_test_mock(chain, "生成报告，必须验收。", run_id="state-truth-test")
    snap = SnapshotStore(workspace / "observability").read_latest()
    assert snap["task_graph"]["nodes"]
    for node in snap["task_graph"]["nodes"]:
        provenance = node["status_provenance"]
        assert provenance["event_id"]
        assert provenance["event_type"] in {"TASK_STATE_CHANGED", "TASK_RECOVERED", "TASK_REPLANNED", "TASK_CREATED"}
        assert provenance["entered_at"] is not None


def test_frontend_has_no_synthetic_live_or_edge_score_defaults() -> None:
    js = Path("apps/console/frontend/assets/app.js").read_text(encoding="utf-8")
    html = Path("apps/console/frontend/index.html").read_text(encoding="utf-8")
    assert "<span></span>LIVE" not in js
    assert "??.5" not in js and "?? .5" not in js
    assert "Unchanged Work" not in js
    assert "data-view=\"lineage\"" in html
    assert "等待动画只表示前端正在等待运行快照" in html
