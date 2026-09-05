from __future__ import annotations

from pathlib import Path

from apps.console.backend.control import CompetitionControlPlane
from apps.console.backend.settings import ProviderSettingsStore


def test_product_first_frontend_has_application_before_observability() -> None:
    root = Path(__file__).resolve().parents[3]
    html = (root / "apps/console/frontend/index.html").read_text(encoding="utf-8")
    workbench = html.index('data-view="workspace"')
    observatory = html.index('data-view="observatory"')
    assert workbench < observatory
    assert 'data-view="settings"' in html
    assert 'id="providerKey"' in html
    assert 'id="testProviderBtn"' in html
    assert 'id="judgeModeBtn"' in html


def test_provider_settings_never_return_secret(tmp_path: Path) -> None:
    store = ProviderSettingsStore(tmp_path)
    saved = store.save({
        "provider_id": "deepseek",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat",
        "api_key": "secret-value-must-not-leak",
    })
    text = repr(saved)
    assert "secret-value-must-not-leak" not in text
    assert saved["active"]["api_key_present"] is True
    assert saved["active"]["api_key_mask"]
    runtime = store.runtime_environment()
    assert runtime["MOSAIC_API_KEY"] == "secret-value-must-not-leak"
    assert runtime["DEEPSEEK_API_KEY"] == "secret-value-must-not-leak"
    assert "OPENAI_API_KEY" not in runtime


def test_public_control_dto_hides_machine_paths_and_pid(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[3]
    control = CompetitionControlPlane(root, tmp_path / "workspace")
    redacted = control.redact_public_payload({
        "uri": f"file://{tmp_path}/private/evidence.txt",
        "log": f"failed at {tmp_path}/private/code.py",
        "url": "http://127.0.0.1:9000/private",
    })
    serialized = repr(redacted)
    assert str(tmp_path) not in serialized
    assert "127.0.0.1" not in serialized
    assert "127.0.0.1" not in redacted["url"] and redacted["url"].endswith("/private")
    public_job = control._public_job({
        "job_id": "j", "kind": "x", "label": "x", "run_id": "r", "status": "RUNNING",
        "created_at": 1.0, "started_at": 2.0, "pid": 1234, "log_path": "/private/log",
        "output_path": "/private/out", "command": ["python", "secret"],
    })
    assert public_job is not None
    assert "pid" not in public_job and "command" not in public_job and "log_path" not in public_job


def test_remote_endpoint_registry_is_settings_only_and_summary_hides_address(tmp_path: Path) -> None:
    from apps.console.backend.settings import ExecutionEndpointStore

    store = ExecutionEndpointStore(tmp_path)
    saved = store.save({
        "endpoint_id": "edge-01",
        "name": "Edge node 01",
        "tier": "edge",
        "transport": "mqtt",
        "host": "192.0.2.10",
        "port": 1883,
        "agent_id": "mqtt-agent-1",
        "topic_prefix": "mosaic/v3",
    })
    assert saved["endpoint"]["host"] == "192.0.2.10"  # explicit Settings API can edit it
    summary = store.summary()
    assert summary["enabled_count"] == 1
    assert summary["by_tier"]["edge"] == 1
    assert "192.0.2.10" not in repr(summary)


def test_remote_endpoint_controls_are_real_backend_actions() -> None:
    root = Path(__file__).resolve().parents[3]
    html = (root / "apps/console/frontend/index.html").read_text(encoding="utf-8")
    js = (root / "apps/console/frontend/assets/app.js").read_text(encoding="utf-8")
    server = (root / "apps/console/backend/server.py").read_text(encoding="utf-8")
    assert 'id="endpointHost"' in html
    assert 'id="saveEndpointBtn"' in html
    assert "/api/settings/test-endpoint" in js
    assert "/api/control/start-remote-acceptance" in js
    assert "/api/control/start-remote-acceptance" in server


def test_provider_secrets_are_isolated_per_provider_and_ollama_never_inherits_cloud_key(tmp_path: Path, monkeypatch) -> None:
    for name in ("MOSAIC_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    store = ProviderSettingsStore(tmp_path)
    store.save({
        "provider_id": "deepseek",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat",
        "api_key": "deepseek-only-secret",
    })
    store.save({
        "provider_id": "openai_compatible",
        "base_url": "https://example.invalid/v1",
        "model": "compatible-model",
        "api_key": "openai-only-secret",
    })
    env = store.runtime_environment()
    assert env["MOSAIC_PROVIDER"] == "openai_compatible"
    assert env["MOSAIC_API_KEY"] == "openai-only-secret"
    assert env["OPENAI_API_KEY"] == "openai-only-secret"
    assert "DEEPSEEK_API_KEY" not in env
    assert "deepseek-only-secret" not in repr(env)

    store.save({
        "provider_id": "deepseek",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat",
        "api_key": "",
    })
    deep_env = store.runtime_environment()
    assert deep_env["MOSAIC_API_KEY"] == "deepseek-only-secret"
    assert deep_env["DEEPSEEK_API_KEY"] == "deepseek-only-secret"
    assert "OPENAI_API_KEY" not in deep_env

    public = store.public()
    assert public["provider_key_presence"] == {
        "deepseek": True,
        "openai_compatible": True,
        "ollama": False,
    }
    store.clear_secret("openai_compatible")
    public = store.public()
    assert public["provider_key_presence"]["deepseek"] is True
    assert public["provider_key_presence"]["openai_compatible"] is False

    store.save({
        "provider_id": "ollama",
        "base_url": "http://127.0.0.1:11434/v1",
        "model": "qwen3:8b",
    })
    active = store.public()["active"]
    assert active["base_url"] == "http://127.0.0.1:11434/v1"
    assert active["api_key_present"] is False
    ollama_env = store.runtime_environment()
    assert ollama_env["MOSAIC_API_KEY"] == "ollama-local"
    assert "DEEPSEEK_API_KEY" not in ollama_env
    assert "OPENAI_API_KEY" not in ollama_env
    assert "deepseek-only-secret" not in repr(store.runtime_environment())


def test_public_redaction_covers_windows_spaces_linux_mounts_and_loopback_fragments(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[3]
    control = CompetitionControlPlane(root, tmp_path / "workspace")
    payload = control.redact_public_payload({
        "windows": r"failed at C:\Program Files\MOSAIC Omega\secret.txt",
        "mnt": "failed at /mnt/data/private/secret.txt",
        "opt": "failed at /opt/mosaic/private/secret.txt",
        "loopback": "prefix http://127.0.0.1:8080/private suffix",
    })
    text = repr(payload)
    assert "Program Files" not in text
    assert "/mnt/data" not in text
    assert "/opt/mosaic" not in text
    assert "127.0.0.1" not in text
    assert "local-service://redacted" in payload["loopback"]


def test_artifact_preview_download_are_root_scoped_and_never_return_absolute_path(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[3]
    workspace = tmp_path / "workspace"
    control = CompetitionControlPlane(root, workspace)
    target = workspace / ".mosaic_deliverables" / "run-a" / "final_report.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# Result\nverified content", encoding="utf-8")
    rows = control.artifacts()
    row = next(item for item in rows if item["name"] == "final_report.md")
    preview = control.artifact_preview(row["artifact_id"])
    assert preview["content"].startswith("# Result")
    assert str(workspace) not in repr(preview)
    path, filename = control.artifact_download(row["artifact_id"])
    assert path == target.resolve()
    assert filename == "final_report.md"
    import pytest
    with pytest.raises(PermissionError):
        control.artifact_preview("deliverables:../outside.txt")


def test_frontend_uses_actual_dag_edges_measured_latency_semantics_and_artifact_actions() -> None:
    root = Path(__file__).resolve().parents[3]
    js = (root / "apps/console/frontend/assets/app.js").read_text(encoding="utf-8")
    html = (root / "apps/console/frontend/index.html").read_text(encoding="utf-8")
    assert "task_graph.edges" in js
    assert "actual_execution_tier" in js
    assert "latency_measurement_count" in js
    assert "未测量" in js
    assert "/api/control/artifact-preview" in js
    assert "/api/control/artifact-download" in js
    assert 'id="agentConfigList"' in html
    assert 'id="clearProviderKeyBtn"' in html
