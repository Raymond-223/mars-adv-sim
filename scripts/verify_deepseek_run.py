#!/usr/bin/env python3
"""Verify saved DeepSeek evidence without overclaiming authenticity.

Two verdicts are emitted:
- ``legacy_real_api_passed``: historical evidence has DeepSeek request IDs,
  provider usage and no Mock assignment/result indicators.
- ``current_strict_passed``: in addition, every API call carries the current
  transport/official-endpoint provenance and every OR-Tools assignment carries
  current SimpleMinCostFlow solver provenance with OPTIMAL status.

A historical run can therefore remain useful evidence while *not* being
presented as if it were generated under today's stricter truth gate.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from urllib.parse import urlparse


def _host(base_url: object) -> str:
    try:
        return (urlparse(str(base_url)).hostname or "").lower()
    except Exception:
        return ""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("result", help="path to main-chain result JSON")
    parser.add_argument("--output", help="optional path for the verification summary")
    parser.add_argument(
        "--require-current-strict",
        action="store_true",
        help="exit nonzero unless current transport + solver provenance is complete",
    )
    args = parser.parse_args()

    result_path = Path(args.result)
    document = json.loads(result_path.read_text(encoding="utf-8-sig"))
    events = list(document.get("events", []))
    tool_events = [item for item in events if item.get("type") == "TOOL_EXECUTED"]
    provenance = [
        item.get("payload", {})
        .get("tool_call", {})
        .get("arguments", {})
        .get("api_provenance")
        for item in tool_events
    ]
    provenance = [item for item in provenance if isinstance(item, dict)]
    actor_ids = [str(item.get("actor_id", "")) for item in document.get("capability_profiles", [])]
    serialized = json.dumps(document, ensure_ascii=False).casefold()
    tasks = list(document.get("tasks", []))
    assignments = [item.get("assignment") or {} for item in tasks if item.get("assignment")]

    legacy_checks = {
        "all_succeeded": document.get("all_succeeded") is True,
        "runtime_agent_mode_deepseek": document.get("runtime_metadata", {}).get("agent_mode") == "deepseek",
        "tool_events_present": bool(tool_events),
        "every_tool_event_has_deepseek_provenance": (
            len(provenance) == len(tool_events)
            and all(
                item.get("provider") == "deepseek"
                and item.get("request_id")
                and _host(item.get("base_url")) == "api.deepseek.com"
                for item in provenance
            )
        ),
        "provider_usage_present": bool(provenance)
        and all(isinstance(item.get("usage"), dict) and item.get("usage") for item in provenance),
        "deepseek_agents_registered": any(value.startswith("agent-deepseek-") for value in actor_ids),
        "no_mock_resource_or_result": "mock-model" not in serialized and '"mock"' not in serialized,
        "evidence_for_every_task": len(tasks) > 0 and len(document.get("evidence_manifest", [])) == len(tasks),
        "verification_for_every_task": len(document.get("verification_results", [])) == len(tasks),
    }
    legacy_real_api_passed = all(legacy_checks.values())

    current_transport_checks = {
        "transport_is_real_network": bool(provenance)
        and all(item.get("transport") in {"openai_sdk", "stdlib_http"} for item in provenance),
        "endpoint_host_is_official": bool(provenance)
        and all(item.get("endpoint_host") == "api.deepseek.com" for item in provenance),
        "official_endpoint_verified": bool(provenance)
        and all(item.get("official_endpoint_verified") is True for item in provenance),
    }
    current_solver_checks = {
        "assignments_present": bool(assignments),
        "all_assignments_ortools": bool(assignments) and all(item.get("policy") == "ortools" for item in assignments),
        "every_ortools_assignment_has_solver_provenance": bool(assignments)
        and all(
            isinstance(item.get("solver_provenance"), dict)
            and str(item["solver_provenance"].get("engine", "")).startswith(
                "ortools.graph.python.min_cost_flow.SimpleMinCostFlow"
            )
            and item["solver_provenance"].get("status") == "OPTIMAL"
            for item in assignments
        ),
    }
    current_checks = legacy_checks | current_transport_checks | current_solver_checks
    current_strict_passed = all(current_checks.values())

    classification = (
        "CURRENT_STRICT_REAL_API_AND_ORTOOLS"
        if current_strict_passed
        else "LEGACY_REAL_API_EVIDENCE_REQUIRES_CURRENT_RERUN"
        if legacy_real_api_passed
        else "NOT_VERIFIED_REAL_API_EVIDENCE"
    )
    summary = {
        "result_file": str(result_path),
        "classification": classification,
        "legacy_real_api_passed": legacy_real_api_passed,
        "current_strict_passed": current_strict_passed,
        "legacy_checks": legacy_checks,
        "current_transport_checks": current_transport_checks,
        "current_solver_checks": current_solver_checks,
        "counts": {
            "tasks": len(tasks),
            "events": len(events),
            "api_calls": len(provenance),
            "assignments": len(assignments),
            "evidence": len(document.get("evidence_manifest", [])),
            "verifications": len(document.get("verification_results", [])),
            "messages": len(document.get("communication", [])),
            "context_packs": len(document.get("context_packs", {})),
        },
        "models": sorted({str(item.get("model")) for item in provenance if item.get("model")}),
        "request_ids": [item.get("request_id") for item in provenance if item.get("request_id")],
        "usage": [item.get("usage", {}) for item in provenance],
        "truth_note": (
            "Historical runs without transport/endpoint_host/official_endpoint_verified and "
            "Assignment.solver_provenance are not promoted to the current strict verdict."
        ),
    }
    rendered = json.dumps(summary, ensure_ascii=False, indent=2)
    print(rendered)
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered + "\n", encoding="utf-8")

    required_pass = current_strict_passed if args.require_current_strict else legacy_real_api_passed
    return 0 if required_pass else 5


if __name__ == "__main__":
    raise SystemExit(main())
