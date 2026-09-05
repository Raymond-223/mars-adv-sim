from __future__ import annotations

from pathlib import Path

from apps.console.backend.control import CompetitionControlPlane


def test_control_plane_exposes_real_competition_operations(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[3]
    control = CompetitionControlPlane(root, tmp_path / "workspace")
    scenario_ids = {x["id"] for x in control.scenarios()}
    assert scenario_ids == {"ros_repair", "financial_research"}
    experiments = control.experiments()
    assert {x["id"] for x in experiments["faults"]} == {
        "agent_offline", "tool_failure", "requirement_change", "evidence_invalidation"
    }
    assert {x["id"] for x in experiments["benchmarks"]} == {"long_horizon", "topology_replay", "scheduler_ablation", "memory_ablation", "split_inference_reference"}


def test_control_start_command_is_strict_real_api_and_shared_observability(tmp_path: Path, monkeypatch) -> None:
    root = Path(__file__).resolve().parents[3]
    control = CompetitionControlPlane(root, tmp_path / "workspace")
    captured = {}

    def fake_start(**kwargs):
        captured.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(control, "_start", fake_start)
    monkeypatch.setattr(control, "_require_strict_env", lambda: None)
    result = control.start_custom("完成一个跨多步骤的软件工程任务并给出证据")
    assert result == {"ok": True}
    command = captured["command"]
    assert "--goalspec-mode" in command and command[command.index("--goalspec-mode") + 1] == "deepseek"
    assert "--agent-mode" in command and command[command.index("--agent-mode") + 1] == "deepseek"
    assert "--scheduler-policy" in command and command[command.index("--scheduler-policy") + 1] == "ortools"
    assert "--no-clean" in command
    assert "--live-control" in command
    assert str(control.workspace) in command


def test_frontend_has_real_control_buttons_and_no_demo_mode() -> None:
    root = Path(__file__).resolve().parents[3]
    html = (root / "apps/console/frontend/index.html").read_text(encoding="utf-8")
    js = (root / "apps/console/frontend/assets/app.js").read_text(encoding="utf-8")
    assert 'id="startCustomBtn"' in html
    assert 'id="stopJobBtn"' in html
    assert 'data-start-scenario' in js
    assert 'data-start-fault' in js
    assert 'data-start-bench' in js
    assert '/api/control/start-custom' in js
    assert '--demo' not in html


def test_scenario_command_enables_live_control(tmp_path: Path, monkeypatch) -> None:
    root = Path(__file__).resolve().parents[3]
    control = CompetitionControlPlane(root, tmp_path / "workspace")
    captured = {}
    monkeypatch.setattr(control, "_require_strict_env", lambda: None)
    monkeypatch.setattr(control, "_start", lambda **kwargs: captured.update(kwargs) or {"ok": True})
    control.start_scenario("ros_repair")
    command = captured["command"]
    assert "--agent-mode" in command and command[command.index("--agent-mode") + 1] == "deepseek"
    assert "--live-control" in command


def test_live_fault_request_is_queued_for_running_custom_task(tmp_path: Path, monkeypatch) -> None:
    import sys
    import time

    root = Path(__file__).resolve().parents[3]
    control = CompetitionControlPlane(root, tmp_path / "workspace")
    monkeypatch.setattr(control, "_require_strict_env", lambda: None)
    job = control._start(  # noqa: SLF001 - validating control lifecycle
        kind="custom_task",
        label="test-live",
        run_id="test-live-run",
        command=[sys.executable, "-c", "import time; time.sleep(10)"],
        output_path=None,
    )
    try:
        result = control.start_fault("tool_failure")
        assert result["mode"] == "LIVE_INJECTION_QUEUED"
        assert result["run_id"] == "test-live-run"
        rows = control.injections.status("test-live-run")
        assert rows and rows[0]["state"] == "PENDING"
    finally:
        control.stop(job["job_id"])
        for _ in range(100):
            row = next(x for x in control.jobs() if x["job_id"] == job["job_id"])
            if row["status"] not in {"QUEUED", "RUNNING", "STOPPING"}:
                break
            time.sleep(0.02)
