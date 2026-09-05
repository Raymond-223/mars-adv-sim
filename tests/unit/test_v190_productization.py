from __future__ import annotations

from pathlib import Path

from apps.console.backend.control import CompetitionControlPlane
from mosaic_omega.integration import MosaicMainChain


def test_templates_prefill_in_frontend_and_do_not_directly_launch_scenario() -> None:
    root = Path(__file__).resolve().parents[2]
    js = (root / "apps/console/frontend/assets/app.js").read_text(encoding="utf-8")
    html = (root / "apps/console/frontend/index.html").read_text(encoding="utf-8")
    assert "data-use-template" in js
    assert "applyTaskTemplate" in js
    assert "template_goal" in js
    assert "使用此模板" in js
    assert 'id="goalInput"' in html
    # Preset scenarios remain available only in the validation lab, but the
    # workspace template click must not call start-scenario directly.
    workspace_slice = js[js.index("function renderWorkspace"):js.index("function renderWorkspacePipeline")]
    assert "/api/control/start-scenario" not in workspace_slice


def test_artifacts_can_be_strictly_scoped_to_current_run(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    workspace = tmp_path / "workspace"
    control = CompetitionControlPlane(root, workspace)
    a = workspace / ".mosaic_deliverables" / "run-a" / "report.md"
    b = workspace / ".mosaic_deliverables" / "run-b" / "report.md"
    a.parent.mkdir(parents=True, exist_ok=True)
    b.parent.mkdir(parents=True, exist_ok=True)
    a.write_text("A", encoding="utf-8")
    b.write_text("B", encoding="utf-8")
    rows = control.artifacts(run_id="run-a")
    assert rows
    assert all(item["logical_path"].split("/")[0] == "run-a" for item in rows)
    assert not any("run-b" in item["logical_path"] for item in rows)


def test_status_separates_task_runs_from_experiments(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    control = CompetitionControlPlane(root, tmp_path / "workspace")
    # No jobs yet, but the DTO contract must expose independent collections so
    # benchmark/fault job IDs can never be passed into the authoritative run selector.
    status = control.status()
    assert "task_jobs" in status
    assert "experiment_jobs" in status


def test_persisted_capability_never_suppresses_new_process_adapter_binding(tmp_path: Path, monkeypatch) -> None:
    # DeepSeekAgent construction itself does not perform a network call. We only
    # verify process-local adapter binding against a persistent SQLite registry.
    monkeypatch.setenv("MOSAIC_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fixture-not-used")
    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-chat")
    workspace = tmp_path / "workspace"

    first = MosaicMainChain(workspace=workspace, scheduler_policy="greedy")
    first.register_default_deepseek_resources(["analysis"])
    assert "agent-deepseek-analysis" in first.execution.agents

    second = MosaicMainChain(workspace=workspace, scheduler_policy="greedy")
    # The capability now already exists in SQLite. v1.9 must still bind the
    # adapter in the fresh process-local execution.agents map.
    assert second.execution.capabilities.get("agent-deepseek-analysis") is not None
    assert "agent-deepseek-analysis" not in second.execution.agents
    second.register_default_deepseek_resources(["analysis"])
    assert "agent-deepseek-analysis" in second.execution.agents
