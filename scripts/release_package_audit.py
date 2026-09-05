#!/usr/bin/env python3
"""Static final-release hygiene audit for MOSAIC-Ω v1.9.0."""
from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    checks: list[dict[str, object]] = []

    def check(name: str, passed: bool, detail: object) -> None:
        checks.append({"name": name, "passed": bool(passed), "detail": str(detail)})

    py_files = sorted(ROOT.rglob("*.py"))
    syntax_errors: list[str] = []
    for path in py_files:
        try:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except Exception as exc:
            syntax_errors.append(f"{path.relative_to(ROOT)}: {type(exc).__name__}: {exc}")
    check("python_ast_all_files", not syntax_errors, f"files={len(py_files)} errors={syntax_errors[:5]}")

    js_files = sorted(ROOT.rglob("*.js"))
    node = shutil.which("node")
    js_errors: list[str] = []
    if node:
        for path in js_files:
            p = subprocess.run([node, "--check", str(path)], capture_output=True, text=True)
            if p.returncode != 0:
                js_errors.append(f"{path.relative_to(ROOT)}: {(p.stderr or p.stdout).strip()}")
        check("javascript_syntax", not js_errors, f"files={len(js_files)} errors={js_errors[:3]}")
    else:
        check("javascript_syntax", False, "node executable unavailable")

    forbidden_dirs = {"__pycache__", ".pytest_cache", ".ruff_cache", ".git", ".venv", "node_modules", ".benchmark_workspace"}
    bad_dirs = sorted(str(p.relative_to(ROOT)) for p in ROOT.rglob("*") if p.is_dir() and p.name in forbidden_dirs)
    bad_bytecode = sorted(str(p.relative_to(ROOT)) for p in ROOT.rglob("*") if p.is_file() and p.suffix.lower() in {".pyc", ".pyo"})
    check("no_cache_vcs_venv_bytecode", not bad_dirs and not bad_bytecode, {"dirs": bad_dirs[:10], "bytecode": bad_bytecode[:10]})

    env_files = sorted(str(p.relative_to(ROOT)) for p in ROOT.rglob(".env") if p.is_file())
    check("no_real_env_file", not env_files, env_files)

    secret_patterns = [
        re.compile(rb"sk-[A-Za-z0-9_-]{20,}"),
        re.compile(rb"(?i)authorization\s*:\s*bearer\s+[A-Za-z0-9._-]{20,}"),
    ]
    binary_skip = {".pdf", ".png", ".jpg", ".jpeg", ".gif", ".zip"}
    secret_hits: list[str] = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or path.suffix.lower() in binary_skip:
            continue
        data = path.read_bytes()
        for pat in secret_patterns:
            if pat.search(data):
                secret_hits.append(str(path.relative_to(ROOT)))
                break
    check("no_obvious_api_secret", not secret_hits, sorted(set(secret_hits)))

    prod_mock = [
        str(p.relative_to(ROOT)) for p in (ROOT / "src").rglob("*")
        if p.is_file() and ("mock_agent" in p.name.lower() or p.name.lower().startswith("mock"))
    ]
    check("production_src_has_no_mock_agent_module", not prod_mock, prod_mock)

    stale_top_pdfs = [p.name for p in ROOT.glob("*LEGACY_PRE_FUSION*.pdf")]
    stale_evidence = [
        str(p.relative_to(ROOT)) for p in (ROOT / "evidence").rglob("*")
        if p.is_file()
        and "legacy" not in p.relative_to(ROOT).parts
        and re.search(r"v1\.[5678](?:\.|_|$)", p.name, re.I)
    ]
    stale_result_files = [
        str(p.relative_to(ROOT)) for p in (ROOT / "experiments" / "results").glob("*")
        if p.is_file() and re.search(r"v1\.[5678](?:\.|_|$)", p.name, re.I)
    ]
    stale_top_level = [p.name for p in ROOT.glob("V1.7.1*")]
    check("no_stale_prefusion_pdf", not stale_top_pdfs, stale_top_pdfs)
    check("no_stale_pre_v190_current_evidence", not stale_evidence, stale_evidence)
    check("no_stale_pre_v190_current_results", not stale_result_files, stale_result_files)
    check("no_v171_release_summary", not stale_top_level, stale_top_level)

    current_results = {p.name for p in (ROOT / "experiments/results").glob("*.json")}
    required_results = {
        "benchmark_1000_events_v3_monolithic.json",
        "topology_ablation_v1.9.0.json",
        "split_inference_reference_v1.9.0.json",
        "scheduler_ablation_v1.9.0.json",
        "memory_ablation_v1.9.0.json",
    }
    check("current_benchmark_results_present", required_results <= current_results, sorted(current_results))

    acceptance = ROOT / "FINAL_ACCEPTANCE.json"
    if acceptance.is_file():
        try:
            data = json.loads(acceptance.read_text(encoding="utf-8"))
            acceptance_ok = "v1.9.0" in str(data.get("release", ""))
        except Exception:
            acceptance_ok = False
    else:
        acceptance_ok = False
    check("current_final_acceptance_is_v190", acceptance_ok, acceptance.name if acceptance.exists() else "missing")

    report = {
        "audit": "MOSAIC-Ω v1.9.0 release package static audit",
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "passed": sum(1 for x in checks if x["passed"]),
        "total": len(checks),
        "all_passed": all(x["passed"] for x in checks),
        "checks": checks,
    }
    out = ROOT / "evidence/release_package_audit_v1.9.0.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["all_passed"] else 7


if __name__ == "__main__":
    raise SystemExit(main())
