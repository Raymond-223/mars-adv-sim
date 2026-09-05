from __future__ import annotations

import re
from pathlib import Path

from scripts.benchmark_long_horizon import _build_monolithic_plan

ROOT = Path(__file__).resolve().parents[3]


def test_every_static_button_declares_button_type_and_new_forms_are_actionable() -> None:
    html = (ROOT / "apps/console/frontend/index.html").read_text(encoding="utf-8")
    buttons = re.findall(r"<button\b[^>]*>", html, flags=re.I)
    assert buttons
    assert all(re.search(r'\btype="button"', tag, flags=re.I) for tag in buttons)
    for control_id in (
        "goalInput", "startCustomBtn", "stopJobBtn", "newAgentBtn", "saveAgentBtn",
        "providerKey", "testProviderBtn", "saveProviderBtn", "newEndpointBtn",
        "testEndpointBtn", "saveEndpointBtn", "endpointEnabled", "requirementInput",
    ):
        assert f'id="{control_id}"' in html


def test_runtime_agent_ui_exposes_binding_transport_endpoint_and_assigned_truth() -> None:
    js = (ROOT / "apps/console/frontend/assets/app.js").read_text(encoding="utf-8")
    assert "auth.assigned_agents||[]" in js
    assert "adapter_bound" in js
    assert "api_transport" in js
    assert "official_endpoint_verified" in js
    assert "API_TEST_ENDPOINT_NOT_COMPETITION_STRICT" not in js  # verdict comes from backend snapshot, never hard-coded
    assert "配置模板不会在这里冒充运行中的 Agent" in js


def test_long_horizon_default_shape_reaches_scalable_64_task_plan() -> None:
    _goal, graph, plan = _build_monolithic_plan()
    assert len(plan) == 64
    assert graph["benchmark_generated"] is True
    assert len(graph["edges"]) == 60
