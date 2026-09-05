from __future__ import annotations

from pathlib import Path

from mosaic_omega.execution_scheduler.adapters.llm_agent import LLMAgentAdapter
from mosaic_omega.observability.projections import _execution_semantics


def _tool_event(event_id: str, tool_name: str, success: bool = True) -> dict:
    return {
        "event_id": event_id,
        "event_type": "TOOL_EXECUTED",
        "run_id": "run-v171",
        "task_id": f"task-{event_id}",
        "actor_id": "agent",
        "payload": {
            "tool_call": {"tool_name": tool_name, "arguments": {}},
            "result": {"success": success},
        },
    }


def test_reasoning_deliverable_is_not_counted_as_concrete_execution() -> None:
    summary = _execution_semantics([_tool_event("1", "task", True)])
    assert summary["verdict"] == "REASONING_DELIVERABLE_ONLY"
    assert summary["reasoning_deliverable_call_count"] == 1
    assert summary["concrete_tool_call_count"] == 0
    assert summary["successful_concrete_tool_call_count"] == 0


def test_real_tool_call_is_counted_as_concrete_execution() -> None:
    summary = _execution_semantics([
        _tool_event("1", "task", True),
        _tool_event("2", "shell", True),
        _tool_event("3", "write_file", False),
    ])
    assert summary["verdict"] == "CONCRETE_TOOL_EXECUTION_VERIFIED"
    assert summary["concrete_tool_call_count"] == 2
    assert summary["successful_concrete_tool_call_count"] == 1
    assert summary["concrete_tools"] == ["shell", "write_file"]


def test_openai_compatible_provider_cannot_inherit_deepseek_official_claim(monkeypatch) -> None:
    monkeypatch.setenv("MOSAIC_PROVIDER", "openai_compatible")
    monkeypatch.setenv("MOSAIC_API_KEY", "unit-test-key")
    adapter = LLMAgentAdapter(
        "provider-test-agent",
        api_key="unit-test-key",
        base_url="https://api.deepseek.com",
        model_name="compatible-model",
        allow_fallback=False,
    )
    assert adapter.provider_id == "openai_compatible"
    assert adapter.endpoint_host == "api.deepseek.com"
    assert adapter.official_endpoint_verified is False
    assert adapter.authenticity_mode == "real_api_unverified_provider"


def test_windows_launcher_regression_contract() -> None:
    root = Path(__file__).resolve().parents[2]
    start_bat_bytes = (root / "START_MOSAIC.bat").read_bytes()
    start_bat = start_bat_bytes.decode("ascii")
    launcher = (root / "scripts" / "windows_launcher.py").read_text(encoding="utf-8")

    # One user-facing BAT only; it must be native CRLF and PowerShell-free.
    assert b"\r\n" in start_bat_bytes
    assert "powershell" not in start_bat.casefold()
    assert "scripts\\windows_launcher.py" in start_bat
    assert not (root / "PREPARE_OFFLINE_WHEELS.bat").exists()

    # Normal startup is genuinely zero-install: no venv bootstrap and no pip/PyPI.
    assert ".venv\\Scripts\\python.exe" not in start_bat
    assert "No pip. No PyPI. No venv setup." in start_bat
    assert "pip install" not in launcher
    assert "subprocess" in launcher
    assert "ZERO_INSTALL_STDLIB" in launcher

    # Python launcher presence must not imply that one exact minor version is installed.
    for selector in ("-3.11", "-3.12", "-3.10", "-3.13"):
        assert selector in start_bat

    # Runtime still preserves fail-closed optional capabilities and app lifecycle.
    assert "127.0.0.1" in launcher and "free_loopback_port" in launcher
    assert "ortools_ready" in launcher
    assert "built-in stdlib HTTP" in launcher
    assert "webbrowser.open" in launcher
    assert "--app=" in launcher
    assert "server.terminate()" in launcher
    assert "--diagnose" in launcher
