# -*- coding: utf-8 -*-
"""Unit tests for explicit dynamic Agent binding and real model switching semantics."""
from __future__ import annotations

from pathlib import Path
import pytest

from mosaic_omega.integration import MosaicMainChain
from mosaic_omega.agent_runtime.models import AgentProfile
from mosaic_omega.agent_runtime.edge_cloud import ExecutionTier
from mosaic_omega.execution_scheduler.adapters.task_spec_agent import TaskSpecAgent
from test_support.mock_mainchain import run_test_mock


class SwitchableTestExecutor(TaskSpecAgent):
    """Explicit unit-test fixture; never presented as a real API Agent."""
    authenticity_mode = "test_fixture"

    def __init__(self, actor_id: str) -> None:
        super().__init__(actor_id)
        self.model_id = "fixture-model-a"

    def set_model(self, model_id: str) -> None:
        self.model_id = model_id


def _profile() -> AgentProfile:
    return AgentProfile(
        agent_id="agent-custom-edge",
        name="Custom Edge Inspector",
        endpoint="inproc://agent-custom-edge",
        skills=("plan", "coding", "analysis"),
        tier=ExecutionTier.EDGE,
        reliability=0.99,
    )


def test_dynamic_agent_requires_explicit_adapter(tmp_path: Path) -> None:
    chain = MosaicMainChain(workspace=tmp_path / "workspace", scheduler_policy="greedy")
    with pytest.raises(ValueError, match="explicit execution adapter"):
        chain.add_agent(_profile())


def test_dynamic_model_replacement_changes_bound_adapter_and_agent_lifecycle(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    chain = MosaicMainChain(workspace=workspace, scheduler_policy="greedy")

    result1 = run_test_mock(chain, "修复 ROS 仓库，必须通过测试，不得修改公共接口。", run_id="run-base")
    assert result1.all_succeeded

    adapter = SwitchableTestExecutor("agent-custom-edge")
    chain.add_agent(_profile(), adapter=adapter)
    current_profile = chain.execution.capabilities.get("agent-custom-edge")
    assert current_profile is not None and current_profile.online is True
    assert current_profile.metadata["adapter_bound"] is True
    assert current_profile.metadata["authenticity_mode"] == "test_fixture"

    chain.replace_model("agent-custom-edge", "qwen2.5-72b-instruct")
    updated_profile = chain.execution.capabilities.get("agent-custom-edge")
    assert adapter.model_id == "qwen2.5-72b-instruct"
    assert updated_profile.metadata.get("model_id") == "qwen2.5-72b-instruct"
    assert updated_profile.metadata.get("model_switch_verified") is True

    chain.remove_agent("agent-custom-edge")
    offline_profile = chain.execution.capabilities.get("agent-custom-edge")
    assert offline_profile.online is False
    assert offline_profile.metadata.get("status_source") == "registry.offline"

    result2 = run_test_mock(chain, "修复 ROS 仓库，必须通过测试，不得修改公共接口。", run_id="run-after-offline")
    assert result2.all_succeeded
