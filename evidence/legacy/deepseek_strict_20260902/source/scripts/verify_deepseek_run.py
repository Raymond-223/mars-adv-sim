#!/usr/bin/env python3
"""Verify that a saved main-chain result is real-API rather than MockAgent output."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("result", help="path to main-chain result JSON")
    parser.add_argument("--output", help="optional path for the verification summary")
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
    actor_ids = [
        str(item.get("actor_id", ""))
        for item in document.get("capability_profiles", [])
    ]
    serialized = json.dumps(document, ensure_ascii=False).casefold()
    checks = {
        "all_succeeded": document.get("all_succeeded") is True,
        "runtime_agent_mode_deepseek": (
            document.get("runtime_metadata", {}).get("agent_mode") == "deepseek"
        ),
        "tool_events_present": bool(tool_events),
        "every_tool_event_has_deepseek_provenance": (
            len(provenance) == len(tool_events)
            and all(item.get("provider") == "deepseek" for item in provenance)
        ),
        "deepseek_agents_registered": any(value.startswith("agent-deepseek-") for value in actor_ids),
        "no_mock_resource_or_result": "mock-model" not in serialized and '"mock"' not in serialized,
        "evidence_for_every_task": (
            len(document.get("tasks", [])) > 0
            and len(document.get("evidence_manifest", [])) == len(document.get("tasks", []))
        ),
        "verification_for_every_task": (
            len(document.get("verification_results", [])) == len(document.get("tasks", []))
        ),
    }
    summary = {
        "result_file": str(result_path),
        "passed": all(checks.values()),
        "checks": checks,
        "counts": {
            "tasks": len(document.get("tasks", [])),
            "events": len(events),
            "api_calls": len(provenance),
            "evidence": len(document.get("evidence_manifest", [])),
            "verifications": len(document.get("verification_results", [])),
            "messages": len(document.get("communication", [])),
            "context_packs": len(document.get("context_packs", {})),
        },
        "models": sorted({str(item.get("model")) for item in provenance if item.get("model")}),
        "request_ids": [item.get("request_id") for item in provenance if item.get("request_id")],
        "usage": [item.get("usage", {}) for item in provenance],
    }
    rendered = json.dumps(summary, ensure_ascii=False, indent=2)
    print(rendered)
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered + "\n", encoding="utf-8")
    return 0 if summary["passed"] else 5


if __name__ == "__main__":
    raise SystemExit(main())
