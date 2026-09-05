from __future__ import annotations

import tempfile
from pathlib import Path

from scenarios.ros_repair.runner import run_scenario


def test_ros_repair_scenario_runs_goal_to_verified_report() -> None:
    with tempfile.TemporaryDirectory() as temp:
        result = run_scenario(Path(temp) / "workspace")
        assert result.all_succeeded
        assert result.completed_task_ids == ["inventory", "diagnose", "patch", "build", "test", "report"]
        assert len(result.verification_results) == 6
        assert all(item["passed"] for item in result.verification_results)
        assert len(result.evidence_manifest) == 6
        assert all(item["verification_status"] == "VERIFIED" for item in result.evidence_manifest)
        report = Path(temp) / "workspace" / "ros_repo" / "artifacts" / "repair_report.md"
        assert report.is_file()
        assert "Final pytest: PASS" in report.read_text(encoding="utf-8")
        published = (
            Path(temp) / "workspace" / ".mosaic_deliverables" / result.run_id
            / "ros_repair" / "repair_report.md"
        )
        assert published.is_file()
        assert published.read_text(encoding="utf-8") == report.read_text(encoding="utf-8")
