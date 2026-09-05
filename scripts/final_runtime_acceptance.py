#!/usr/bin/env python3
"""Fresh current-code external acceptance for a competition machine.

Run after ``START_MOSAIC.bat`` and configure DeepSeek in the application's
Settings screen. The script loads that backend-managed provider configuration and
fails closed if OR-Tools, the key, or the official endpoint is missing. It executes the current code with
DeepSeek + the default strict OR-Tools scheduler, then requires the current
transport/endpoint/solver provenance gate to pass.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
for item in (ROOT, ROOT / "src"):
    if str(item) not in sys.path:
        sys.path.insert(0, str(item))
RESULT = ROOT / "experiments/results/final_current_strict_run.json"
VERIFY = ROOT / "evidence/final_current_strict_run_verification.json"
REPORT = ROOT / "evidence/final_runtime_acceptance.json"


def run(cmd: list[str], *, env: dict[str, str] | None = None, timeout_s: float = 600.0) -> subprocess.CompletedProcess[str]:
    print("+", " ".join(cmd))
    try:
        return subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True, env=env, timeout=timeout_s)
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        return subprocess.CompletedProcess(cmd, 124, stdout, stderr + f"\nTIMEOUT after {timeout_s:.0f}s")


def main() -> int:
    checks: list[dict[str, object]] = []
    ortools_ok = importlib.util.find_spec("ortools") is not None

    # v1.9.0 primary configuration path is the product Settings screen.  Keep
    # environment variables as a compatible fallback, but do not require users
    # to duplicate a Key they already saved in the application.
    from apps.console.backend.settings import ProviderSettingsStore
    store = ProviderSettingsStore(ROOT / ".mosaic_workspace/competition-console")
    provider_public = store.public()["active"]
    provider_env = store.runtime_environment()
    child_env = dict(os.environ)
    child_env.update(provider_env)
    # Competition laptops may have unrelated globally-installed pytest plugins.
    # Disable autoload so acceptance is determined by this project's own test
    # matrix instead of third-party instrumentation/atexit hooks.
    child_env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    provider_id = str(provider_public.get("provider_id") or "")
    key_ok = bool(provider_env.get("DEEPSEEK_API_KEY"))
    base_url = str(provider_public.get("base_url") or provider_env.get("DEEPSEEK_BASE_URL") or "")
    endpoint_host = (urlsplit(base_url).hostname or "").casefold()
    endpoint_ok = provider_id == "deepseek" and endpoint_host == "api.deepseek.com"
    checks.append({"name": "ortools_importable", "passed": ortools_ok})
    checks.append({"name": "deepseek_provider_selected", "passed": provider_id == "deepseek"})
    checks.append({"name": "deepseek_key_present", "passed": key_ok})
    checks.append({"name": "deepseek_official_endpoint", "passed": endpoint_ok, "endpoint_host": endpoint_host})
    if not ortools_ok or provider_id != "deepseek" or not key_ok or not endpoint_ok:
        report = {
            "status": "BLOCKED_FAIL_CLOSED",
            "passed": False,
            "checks": checks,
            "instruction": "Run START_MOSAIC.bat, configure DeepSeek in Settings, test the connection, and keep the official api.deepseek.com endpoint.",
        }
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 20

    workspace = ROOT / ".mosaic_workspace/final-current-strict"
    if workspace.exists():
        shutil.rmtree(workspace)
    RESULT.parent.mkdir(parents=True, exist_ok=True)
    VERIFY.parent.mkdir(parents=True, exist_ok=True)

    unit = run([sys.executable, "scripts/run_isolated_pytest.py", "tests/unit", "-q"], env=child_env, timeout_s=300)
    checks.append({"name": "unit_pytest", "passed": unit.returncode == 0, "stdout_tail": unit.stdout[-4000:], "stderr_tail": unit.stderr[-4000:]})
    integration_rows = []
    integration_ok = True
    for test_file in sorted((ROOT / "tests/integration").glob("test_*.py")):
        row = run([
            sys.executable, "scripts/run_isolated_pytest.py",
            str(test_file.relative_to(ROOT)), "-q",
        ], env=child_env, timeout_s=180)
        integration_rows.append({
            "file": str(test_file.relative_to(ROOT)),
            "passed": row.returncode == 0,
            "stdout_tail": row.stdout[-2500:],
            "stderr_tail": row.stderr[-2500:],
        })
        integration_ok = integration_ok and row.returncode == 0
    checks.append({"name": "integration_pytest", "passed": integration_ok, "files": integration_rows})
    if unit.returncode != 0 or not integration_ok:
        status = "FAILED_TEST_MATRIX"
    else:
        demo = run([
            sys.executable,
            "scripts/demo_main_chain.py",
            "完成一个可验证的软件工程长程任务；必须给出证据并通过验收。",
            "--run-id", f"final-current-strict-{int(time.time())}",
            "--workspace", str(workspace),
            "--output", str(RESULT),
            "--goalspec-mode", "deepseek",
            "--agent-mode", "deepseek",
            "--scheduler-policy", "ortools",
        ], env=child_env)
        checks.append({"name": "current_deepseek_ortools_run", "passed": demo.returncode == 0, "stdout_tail": demo.stdout[-5000:], "stderr_tail": demo.stderr[-5000:]})
        if demo.returncode != 0:
            status = "FAILED_CURRENT_RUN"
        else:
            verify = run([
                sys.executable,
                "scripts/verify_deepseek_run.py",
                str(RESULT),
                "--output", str(VERIFY),
                "--require-current-strict",
            ], env=child_env)
            checks.append({"name": "current_strict_provenance", "passed": verify.returncode == 0, "stdout_tail": verify.stdout[-5000:], "stderr_tail": verify.stderr[-5000:]})
            status = "CURRENT_STRICT_VERIFIED" if verify.returncode == 0 else "FAILED_CURRENT_STRICT_PROVENANCE"

    passed = all(bool(item.get("passed")) for item in checks)
    report = {
        "status": status,
        "passed": passed,
        "checks": checks,
        "result_file": str(RESULT.relative_to(ROOT)) if RESULT.exists() else None,
        "verification_file": str(VERIFY.relative_to(ROOT)) if VERIFY.exists() else None,
    }
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if passed else 21


if __name__ == "__main__":
    raise SystemExit(main())
