from __future__ import annotations

from pathlib import Path

from mosaic_omega.integration.live_faults import LiveFaultController, LiveFaultMailbox
from scenarios.ros_repair.runner import run_scenario


def test_live_tool_failure_is_consumed_by_runtime_and_recovers(tmp_path: Path) -> None:
    workspace = tmp_path / "live-tool-failure"
    run_id = "test-live-tool-failure"
    mailbox = LiveFaultMailbox(workspace)
    mailbox.enqueue(run_id, "tool_failure", requested_by="pytest")
    controller = LiveFaultController(workspace)

    result = run_scenario(
        workspace, run_id=run_id, agent_mode="tool",
        round_hook=controller.round_hook,
    )

    rows = mailbox.status(run_id)
    assert rows and rows[0]["state"] == "APPLIED"
    event_types = [str(event.get("type") or event.get("event_type")) for event in result.events]
    assert "FAULT_INJECTED" in event_types
    assert "RECOVERY_PLANNED" in event_types
    assert "TASK_RECOVERED" in event_types
    assert result.all_succeeded is True


def test_live_agent_offline_reroutes_to_registered_backup(tmp_path: Path) -> None:
    workspace = tmp_path / "live-agent-offline"
    run_id = "test-live-agent-offline"
    mailbox = LiveFaultMailbox(workspace)
    mailbox.enqueue(run_id, "agent_offline", requested_by="pytest")
    controller = LiveFaultController(workspace)

    result = run_scenario(
        workspace, run_id=run_id, agent_mode="tool",
        round_hook=controller.round_hook,
    )

    rows = mailbox.status(run_id)
    assert rows and rows[0]["state"] == "APPLIED"
    assigned = {
        task["assignment"]["agent_id"]
        for task in result.tasks
        if isinstance(task.get("assignment"), dict)
    }
    assert "ros-repair-agent-backup" in assigned
    assert result.all_succeeded is True
