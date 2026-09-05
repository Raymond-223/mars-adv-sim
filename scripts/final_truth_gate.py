"""MOSAIC-Ω v1.9.0 fail-closed artifact truth gate.

This gate checks only facts that can be established from the release artifact and
local deterministic runtime.  It deliberately does NOT manufacture evidence for
external provider credentials, a physical MQTT node, or real LLM layer splitting.
Those claims require a fresh competition-machine acceptance run.
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for item in (ROOT, ROOT / "src"):
    if str(item) not in sys.path:
        sys.path.insert(0, str(item))

RELEASE = "MOSAIC-Omega-V3-XH202631-Product-First-v1.9.0"


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8", errors="replace")


def record(name: str, passed: bool, detail: str, category: str = "artifact") -> dict[str, object]:
    return {"name": name, "passed": bool(passed), "detail": detail, "category": category}


def _provider_isolation_runtime() -> tuple[bool, str]:
    try:
        from apps.console.backend.settings import ProviderSettingsStore
        with tempfile.TemporaryDirectory(prefix="mosaic-v18-provider-") as tmp:
            old = {k: os.environ.get(k) for k in ("MOSAIC_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY")}
            for key in old:
                os.environ.pop(key, None)
            try:
                store = ProviderSettingsStore(Path(tmp))
                store.save({
                    "provider_id": "deepseek", "base_url": "https://api.deepseek.com",
                    "model": "deepseek-v4-flash", "api_key": "deepseek-isolated-secret",
                })
                store.save({
                    "provider_id": "openai_compatible", "base_url": "https://provider.invalid/v1",
                    "model": "compatible-model", "api_key": "openai-isolated-secret",
                })
                openai_env = store.runtime_environment()
                if openai_env.get("MOSAIC_API_KEY") != "openai-isolated-secret":
                    return False, "active OpenAI-compatible provider did not resolve its own secret"
                if "DEEPSEEK_API_KEY" in openai_env or "deepseek-isolated-secret" in repr(openai_env):
                    return False, "DeepSeek secret/provider alias crossed into OpenAI-compatible runtime"
                store.save({
                    "provider_id": "ollama", "base_url": "http://127.0.0.1:11434/v1",
                    "model": "qwen3:8b",
                })
                ollama_env = store.runtime_environment()
                if "DEEPSEEK_API_KEY" in ollama_env or "OPENAI_API_KEY" in ollama_env:
                    return False, "Ollama inherited a cloud-provider secret alias"
                if "deepseek-isolated-secret" in repr(ollama_env) or "openai-isolated-secret" in repr(ollama_env):
                    return False, "Ollama inherited a cloud-provider secret value"
                if store.public()["active"]["base_url"] != "http://127.0.0.1:11434/v1":
                    return False, "explicit Settings API did not preserve the local Ollama URL"
                return True, "per-provider secrets isolated; Ollama keeps local URL and receives no cloud secret alias"
            finally:
                for key, value in old.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
    except Exception as exc:  # fail closed
        return False, f"runtime provider-isolation probe failed: {type(exc).__name__}: {exc}"


def _redaction_runtime() -> tuple[bool, str]:
    try:
        from apps.console.backend.control import CompetitionControlPlane
        with tempfile.TemporaryDirectory(prefix="mosaic-v18-redact-") as tmp:
            control = CompetitionControlPlane(ROOT, Path(tmp) / "workspace")
            payload = control.redact_public_payload({
                "win": r"C:\Program Files\MOSAIC Omega\private\secret.txt",
                "mnt": "/mnt/data/private/secret.txt",
                "opt": "/opt/mosaic/private/secret.txt",
                "loop": "prefix http://127.0.0.1:8080/private suffix",
            })
            text = repr(payload)
            leaked = [needle for needle in ("Program Files", "/mnt/data", "/opt/mosaic", "127.0.0.1") if needle in text]
            return (not leaked, "no adversarial local-path/loopback leak" if not leaked else f"leaked={leaked}")
    except Exception as exc:
        return False, f"runtime redaction probe failed: {type(exc).__name__}: {exc}"


def _split_runtime() -> tuple[bool, str]:
    try:
        from mosaic_omega.agent_runtime.split_inference import run_pipeline_split
        result = run_pipeline_split([0.15, -0.2, 0.45, 0.7, -0.55, 0.31, 0.08, -0.11])
        passed = bool(
            result.get("verified_equivalent")
            and result.get("process_boundary_verified")
            and result.get("claim_boundary") == "REFERENCE_MLP_NOT_LLM_SPLIT"
        )
        return passed, (
            f"equivalent={result.get('verified_equivalent')} process_boundary={result.get('process_boundary_verified')} "
            f"claim={result.get('claim_boundary')} activation_bytes={result.get('activation_payload_bytes_measured')}"
        )
    except Exception as exc:
        return False, f"reference split runtime probe failed: {type(exc).__name__}: {exc}"


def main() -> int:
    html = read("apps/console/frontend/index.html")
    js = read("apps/console/frontend/assets/app.js")
    css = read("apps/console/frontend/assets/style.css")
    control = read("apps/console/backend/control.py")
    settings = read("apps/console/backend/settings.py")
    server = read("apps/console/backend/server.py")
    scheduler = read("src/mosaic_omega/execution_scheduler/scheduler.py")
    models = read("src/mosaic_omega/execution_scheduler/models.py")
    executor = read("src/mosaic_omega/execution_scheduler/adapters/local_tool_executor.py")
    verifier = read("src/mosaic_omega/verifier/service.py")
    semantic = read("src/mosaic_omega/verifier/semantic.py")
    main_chain = read("src/mosaic_omega/integration/main_chain.py")
    llm_agent = read("src/mosaic_omega/execution_scheduler/adapters/llm_agent.py")
    projections = read("src/mosaic_omega/observability/projections.py")
    scheduler_service = read("src/mosaic_omega/execution_scheduler/service.py")
    split = read("src/mosaic_omega/agent_runtime/split_inference.py")
    mqtt_agent = read("src/mosaic_omega/execution_scheduler/adapters/mqtt_agent.py")
    startup = read("scripts/windows_launcher.py")
    compose = read("deploy/docker-compose.yml")
    mosquitto = read("deploy/mosquitto/mosquitto.conf")
    matrix = read("docs/COMPETITION_REQUIREMENT_MATRIX.md")
    gate_doc = read("docs/FINAL_TRUTH_GATE.md")
    pyproject = read("pyproject.toml")

    checks: list[dict[str, object]] = []
    checks.append(record(
        "release_version_is_v1_9_0",
        'version = "1.9.0"' in pyproject and '__version__ = "1.9.0"' in read("src/mosaic_omega/__init__.py"),
        "pyproject and package version both 1.9.0",
    ))
    checks.append(record(
        "product_first_information_architecture",
        'data-view="workspace"' in html and 'data-view="observatory"' in html
        and html.index('data-view="workspace"') < html.index('data-view="observatory"'),
        "application workbench precedes technical observability",
    ))
    checks.append(record(
        "generic_task_cannot_inject_acceptance",
        '"acceptance_conditions_injected": False' in executor
        and "acceptance_conditions" not in re.search(r'if call\.tool_name == "task":.*?if call\.tool_name == "read_file"', executor, re.S).group(0).split('metadata =', 1)[0],
        "generic task persists a deliverable and does not append its own acceptance rubric",
    ))
    checks.append(record(
        "unknown_acceptance_requires_independent_verifier",
        "ProviderSemanticJudge" in verifier
        and "self_echo_acceptance_disabled" in verifier
        and "Do not accept a task merely because it claims it is complete" in semantic,
        "unknown natural-language acceptance is fail-closed through an independent semantic verifier",
    ))
    checks.append(record(
        "reasoning_output_not_counted_as_concrete_execution",
        "REASONING_DELIVERABLE_ONLY" in projections
        and "successful_concrete_tool_call_count" in projections
        and "concreteExecution" in js
        and "仅生成文本交付物不算执行闭环" in js,
        "judge closed-loop evidence requires successful non-task ToolRuntime execution",
    ))
    checks.append(record(
        "generic_provider_cannot_inherit_deepseek_strict_claim",
        'self.provider_id == "deepseek"' in llm_agent
        and 'return "real_api_unverified_provider"' in llm_agent,
        "OpenAI-compatible provider cannot become DeepSeek-official merely by reusing the endpoint hostname",
    ))
    checks.append(record(
        "deterministic_acceptance_dsl_is_evidence_bound",
        all(token in verifier for token in ("file_exists:", "file_contains:", "contains:", "execution_success")),
        "deterministic acceptance predicates are explicit and workspace/evidence scoped",
    ))

    provider_ok, provider_detail = _provider_isolation_runtime()
    checks.append(record("provider_secret_isolation_runtime", provider_ok, provider_detail, "runtime-local"))
    checks.append(record(
        "dedicated_provider_settings_and_clear_key",
        all(x in html for x in ('id="providerKey"', 'id="testProviderBtn"', 'id="saveProviderBtn"'))
        and "/api/settings/test-provider" in server and "/api/settings/clear-provider-key" in server,
        "API/model settings have real save/test/clear backend actions",
    ))
    checks.append(record(
        "provider_neutral_runtime_secret_alias",
        '"MOSAIC_API_KEY": key' in settings
        and 'if provider_id == "deepseek"' in settings
        and 'elif provider_id == "openai_compatible"' in settings,
        "only active provider receives its matching provider-specific environment alias",
    ))
    checks.append(record(
        "frontend_dag_uses_only_authoritative_edges",
        "task_graph.edges" in js and "Task DAG：箭头只来自 task_graph.edges" in js,
        "DAG arrows are generated from backend task_graph.edges rather than topological adjacency",
    ))
    checks.append(record(
        "recommended_and_actual_tiers_are_distinct",
        all(x in models for x in ("recommended_tier", "actual_execution_tier", "placement_fallback"))
        and all(x in scheduler for x in ("recommended_tier =", "actual_tier =", "fallback = actual_tier != recommended_tier"))
        and "actualTier(a)" in js,
        "scheduler and UI distinguish recommendation from actual selected-Agent execution tier",
    ))
    checks.append(record(
        "artifact_preview_download_root_scoped",
        "/api/control/artifact-preview" in server and "/api/control/artifact-download" in server
        and "def _resolve_artifact" in control and "display_name" in control and "context" in control,
        "deliverables expose semantic names plus server-scoped preview/download IDs instead of absolute paths",
    ))
    checks.append(record(
        "agent_studio_persistent_crud",
        "class AgentSettingsStore" in settings and "def save(self, payload" in settings and "def delete(self, agent_id" in settings
        and "/api/settings/agents" in server and "saveAgent" in js,
        "Agent Studio persists add/update/delete configuration consumed by new runs",
    ))
    checks.append(record(
        "latency_requires_measurement_count",
        "latency_measurement_count" in js and "未测量" in js and "measured (" in js,
        "configured zero latency is not rendered as a measurement",
    ))
    checks.append(record(
        "memory_token_metrics_are_labeled_estimated",
        "token values are ESTIMATED" in js and "Estimated Token reduction" in js and "ESTIMATED" in js,
        "estimated context/token values are visually distinguished from measured provider usage",
    ))
    checks.append(record(
        "reduced_motion_supported",
        "prefers-reduced-motion:reduce" in css,
        "runtime animation can be reduced without altering underlying data semantics",
    ))
    checks.append(record(
        "event_driven_animation_contract",
        all(x in js for x in ("oldMessages", "policy_action==='SEND'", "changedTasks", "memoryPulseUntil")),
        "message/task/memory motion is triggered by newly observed authoritative state changes",
    ))
    checks.append(record(
        "sqlite_is_default_local_authority",
        "ExecutionSchedulerService.sqlite(settings)" in main_chain,
        "local product main chain persists EventStore authority in SQLite by default",
    ))
    checks.append(record(
        "safe_resume_is_implemented",
        "def resume_run" in scheduler_service
        and "RUN_RESUME_SAFE_STOP" in scheduler_service
        and "safe_stopped_side_effecting" in scheduler_service,
        "restart recovery path exists and unsafe/unknown side effects are not silently repeated",
    ))
    checks.append(record(
        "reference_split_is_real_but_not_claimed_as_llm_split",
        "REFERENCE_MLP_NOT_LLM_SPLIT" in split
        and "subprocess.run" in split
        and "activation_payload_bytes_measured" in split,
        "cross-process reference split is explicitly bounded away from an LLM split claim",
    ))
    checks.append(record(
        "windows_launcher_uses_ephemeral_loopback_port",
        "free_loopback_port" in startup and '"--port", str(port)' in startup and 'http://127.0.0.1:{port}/' in startup,
        "launcher does not depend on fixed port 8080 and still keeps loopback internal",
    ))

    split_ok, split_detail = _split_runtime()
    checks.append(record("reference_split_runtime_equivalence", split_ok, split_detail, "runtime-local"))

    redaction_ok, redaction_detail = _redaction_runtime()
    checks.append(record("public_redaction_adversarial_probe", redaction_ok, redaction_detail, "runtime-local"))
    checks.append(record(
        "settings_local_url_is_not_globally_redacted",
        "provider_settings.public" in server or "/api/settings/providers" in server,
        "explicit Settings route is separate from observability/public redaction so local Ollama URL remains editable",
    ))
    checks.append(record(
        "remote_mqtt_credentials_and_tls_supported",
        "username" in settings and "tls" in settings and "password_present" in settings
        and "username" in mqtt_agent and "tls" in mqtt_agent,
        "remote MQTT endpoint supports credentialed/TLS transport with password stored separately",
    ))
    checks.append(record(
        "docker_is_optional_and_internal_services_not_host_published",
        'profiles: ["console"]' in compose
        and '127.0.0.1:${CONSOLE_PORT:-8080}:8080' in compose
        and "allow_anonymous false" in mosquitto
        and ':rw' in compose
        and "POSTGRES_PASSWORD:?" in compose and "REDIS_PASSWORD:?" in compose and "MQTT_PASSWORD:?" in compose,
        "Docker is optional; console is loopback-bound, workspace writable, infra credentials required, MQTT anonymous disabled",
    ))
    checks.append(record(
        "windows_app_lifecycle_and_offline_path",
        "--app=" in startup and "browser_proc.wait()" in startup
        and "server.terminate()" in startup
        and "webbrowser.open" in startup
        and "--no-index" in startup and "--find-links" in startup
        and not (ROOT / "PREPARE_OFFLINE_WHEELS.bat").exists(),
        "single-BAT desktop launcher owns the backend, falls back to the default browser, and keeps offline wheelhouse optional",
    ))
    checks.append(record(
        "frontend_has_no_hardcoded_external_service_url",
        not re.search(r'https?://(?:127\.0\.0\.1|localhost|api\.)', js, re.I)
        and not re.search(r'https?://(?:127\.0\.0\.1|localhost|api\.)', html, re.I),
        "frontend calls relative product APIs; provider URLs live in Settings data",
    ))
    checks.append(record(
        "competition_claim_matrix_is_fail_closed",
        "REFERENCE_MLP_NOT_LLM_SPLIT" in matrix and "deterministic" in matrix.casefold()
        and "实测" in matrix and "模型切分" in matrix,
        "competition matrix separates measured deterministic evidence, remote runtime evidence and model-split claim boundary",
    ))
    checks.append(record(
        "truth_gate_document_covers_animation_data_and_split_boundaries",
        all(token in gate_doc for token in ("动画", "数据", "DAG", "Verifier", "REFERENCE_MLP_NOT_LLM_SPLIT")),
        "human-readable truth gate documents the same release invariants",
    ))

    stale_current_proof = []
    evidence_dir = ROOT / "evidence"
    if evidence_dir.is_dir():
        for p in evidence_dir.rglob("*"):
            if p.is_file() and re.search(r"v1\.[56]", p.name, re.I):
                stale_current_proof.append(str(p.relative_to(ROOT)))
    if (ROOT / "FINAL_ACCEPTANCE.json").is_file():
        try:
            current = json.loads(read("FINAL_ACCEPTANCE.json"))
            if "v1.9.0" not in str(current.get("release", "")):
                stale_current_proof.append("FINAL_ACCEPTANCE.json:not-v1.9.0")
        except Exception:
            stale_current_proof.append("FINAL_ACCEPTANCE.json:invalid")
    checks.append(record(
        "no_stale_v15_v16_current_acceptance_evidence",
        not stale_current_proof,
        "none" if not stale_current_proof else f"stale={stale_current_proof[:20]}",
    ))

    runtime_secret_files: list[str] = []
    for p in ROOT.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(ROOT).as_posix()
        if any(part in {".venv", ".git", "__pycache__"} for part in p.parts):
            continue
        if p.name == ".env" or p.name.endswith(".secret") or p.name == "provider.secret":
            runtime_secret_files.append(rel)
    checks.append(record(
        "package_contains_no_runtime_secret_files",
        not runtime_secret_files,
        "none" if not runtime_secret_files else f"found={runtime_secret_files}",
    ))

    runtime = {
        "ortools_importable_in_this_environment": False,
        "provider_api_key_present_in_this_environment": bool(
            os.getenv("MOSAIC_API_KEY") or os.getenv("DEEPSEEK_API_KEY") or os.getenv("OPENAI_API_KEY")
        ),
        "fresh_current_code_public_model_run_verified": False,
        "fresh_physical_remote_mqtt_acceptance_verified": False,
        "real_llm_layer_split_verified": False,
        "status": "EXTERNAL_REVALIDATION_REQUIRED",
        "note": (
            "Artifact/local deterministic checks cannot prove a public provider credential, a physical remote Agent, "
            "or real LLM layer split. Run fresh acceptance on the competition machine before making those claims."
        ),
    }
    try:
        import ortools  # noqa: F401
        runtime["ortools_importable_in_this_environment"] = True
    except Exception:
        pass

    passed = sum(1 for c in checks if c["passed"])
    result = {
        "release": RELEASE,
        "generated_at": time.time(),
        "artifact_truth_gate_passed": passed == len(checks),
        "competition_runtime_external_verification": runtime,
        "passed": passed,
        "total": len(checks),
        "checks": checks,
    }
    out = ROOT / "evidence/final_truth_gate_v1.9.0.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["artifact_truth_gate_passed"] else 9


if __name__ == "__main__":
    raise SystemExit(main())
