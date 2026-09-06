from __future__ import annotations

import json
from pathlib import Path

from mosaic_omega.console_api import ConsoleDataSource
from mosaic_omega.integration import MosaicMainChain
from test_support.mock_mainchain import run_test_mock


def test_main_chain_writes_read_only_dashboard_snapshot(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    chain = MosaicMainChain(workspace=workspace, scheduler_policy="greedy")
    result = run_test_mock(
        chain,
        "修复 ROS 仓库，必须通过测试，不得修改公共接口。",
        run_id="observability-e2e",
    )
    assert result.all_succeeded

    latest = workspace / "observability" / "latest.json"
    run_snapshot = workspace / "observability" / "runs" / "observability-e2e.json"
    log_file = workspace / "observability" / "logs" / "events.jsonl"
    assert latest.is_file()
    assert run_snapshot.is_file()
    assert log_file.is_file()

    # latest.json is a compact pointer; the body lives in the run file.
    pointer = json.loads(latest.read_text(encoding="utf-8"))
    assert pointer["run_id"] == result.run_id
    snapshot = json.loads(run_snapshot.read_text(encoding="utf-8"))
    assert snapshot["schema_version"] == "mosaic-console-v1"
    assert snapshot["run"]["run_id"] == result.run_id
    assert snapshot["run"]["status"] == "SUCCEEDED"
    for section in {
        "task_graph",
        "topology",
        "communication",
        "scheduler",
        "memory",
        "recovery",
        "evidence",
        "events",
        "traces",
        "metrics",
    }:
        assert section in snapshot

    # Console telemetry must preserve the low-entropy policy decisions even when
    # a message is suppressed or merged and therefore has no MessageEnvelope.
    assert snapshot["communication"]["total"] >= len(result.communication)
    assert "action_counts" in snapshot["communication"]


def test_structured_jsonl_log_has_handbook_fields(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    chain = MosaicMainChain(workspace=workspace, scheduler_policy="greedy")
    run_test_mock(chain, "生成一个报告，必须通过验收。", run_id="log-e2e")
    lines = (workspace / "observability" / "logs" / "events.jsonl").read_text(encoding="utf-8").splitlines()
    assert lines
    first = json.loads(lines[0])
    for key in {
        "timestamp",
        "level",
        "service",
        "trace_id",
        "run_id",
        "event_type",
        "latency_ms",
        "error_code",
        "schema_version",
    }:
        assert key in first


def test_console_data_source_is_read_only_and_filters_events(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    chain = MosaicMainChain(workspace=workspace, scheduler_policy="greedy")
    run_test_mock(chain, "生成一个报告，必须通过验收。", run_id="console-source")

    source = ConsoleDataSource(workspace / "observability")
    snapshot = source.snapshot("console-source")
    assert snapshot is not None
    assert source.runs()[0]["run_id"] == "console-source"
    verified = source.events(run_id="console-source", event_type="TASK_VERIFIED")
    assert verified
    assert all((item.get("type") or item.get("event_type")) == "TASK_VERIFIED" for item in verified)


def test_console_frontend_is_self_contained_and_has_no_cdn_dependency() -> None:
    root = Path("apps/console/frontend")
    html = (root / "index.html").read_text(encoding="utf-8")
    js = (root / "assets/app.js").read_text(encoding="utf-8")
    css = (root / "assets/style.css").read_text(encoding="utf-8")
    assert "http://" not in html and "https://" not in html
    assert "http://" not in js and "https://" not in js
    assert "MOSAIC-Ω" in html
    assert "INTERACTIVE" in html
    assert "startCustomBtn" in html
    assert "faultCards" in html
    assert css
