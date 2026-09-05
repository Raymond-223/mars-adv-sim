# -*- coding: utf-8 -*-
"""Integration test for Scenario B: Financial Research & Risk Analysis."""

from __future__ import annotations

import tempfile
from pathlib import Path

from scenarios.financial_research.runner import run_scenario


def test_financial_research_scenario_runs_goal_to_verified_report() -> None:
    with tempfile.TemporaryDirectory() as temp:
        result = run_scenario(Path(temp) / "workspace")
        assert result.all_succeeded
        assert result.completed_task_ids == [
            "ingest",
            "decrypt",
            "sentiment",
            "risk_modeling",
            "compliance",
            "report",
        ]
        assert len(result.verification_results) == 6
        assert all(item["passed"] for item in result.verification_results)
        assert len(result.evidence_manifest) == 6
        assert all(item["verification_status"] == "VERIFIED" for item in result.evidence_manifest)

        # Explicit heterogeneous tier requirements are scheduler-enforced, not labels.
        assignments = {item["node_id"]: item["assignment"] for item in result.tasks}
        assert assignments["decrypt"]["agent_id"] == "fin-device-agent"
        assert assignments["decrypt"]["execution_tier"] == "device"
        assert assignments["risk_modeling"]["agent_id"] == "fin-cloud-agent"
        assert assignments["risk_modeling"]["execution_tier"] == "cloud"
        assert assignments["sentiment"]["agent_id"] == "fin-edge-agent"
        assert assignments["sentiment"]["execution_tier"] == "edge"

        report = (
            Path(temp)
            / "workspace"
            / "fin_research_workspace"
            / "artifacts"
            / "investment_research_report.md"
        )
        assert report.is_file()
        report_text = report.read_text(encoding="utf-8")
        assert "星海量子智能科技股份有限公司" in report_text
        assert "DEVICE_ENCLAVE_VERIFIED" in report_text
        assert "审计结论" in report_text
        published = (
            Path(temp) / "workspace" / ".mosaic_deliverables" / result.run_id
            / "financial_research" / "investment_research_report.md"
        )
        assert published.is_file()
        assert published.read_text(encoding="utf-8") == report_text
