"""Scenario A: reproducible ROS-like repository diagnosis, repair, test and report."""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Any

from mosaic_omega.execution_scheduler.adapters.task_spec_agent import TaskSpecAgent
from mosaic_omega.execution_scheduler.adapters.llm_agent import LLMAgentAdapter
from mosaic_omega.execution_scheduler.models import ActorKind, CapabilityProfile
from mosaic_omega.goalspec import compile_goal
from mosaic_omega.integration.main_chain import MosaicMainChain, MainChainRunResult
from mosaic_omega.agent_runtime.edge_cloud import ExecutionTier
from mosaic_omega.agent_runtime.models import AgentProfile


TASKS = (
    ("inventory", "Inventory repository files and ROS package metadata"),
    ("diagnose", "Run tests and produce a root-cause diagnosis"),
    ("patch", "Apply the smallest patch justified by diagnosis"),
    ("build", "Compile the repaired repository"),
    ("test", "Run the regression test suite"),
    ("report", "Generate evidence-carrying repair report"),
)


def _task(
    task_id: str,
    description: str,
    *,
    depends_on: tuple[str, ...] = (),
    command: list[str],
    acceptance: tuple[str, ...],
) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "node_id": task_id,
        "type": "ros_repair",
        "description": description,
        "predecessors": list(depends_on),
        "required_capabilities": ["ros_repair"],
        "required_permissions": ["shell.execute"],
        "acceptance": list(acceptance),
        "risk": "normal",
        "inputs": {},
        "outputs": {},
        "evidence_dependencies": [],
        "resource_requirements": {"device_location": "local"},
        "metadata": {
            "tool": {
                "name": "shell",
                "arguments": {"command": command},
                "timeout_s": 20,
            }
        },
    }


def build_plan(repo_relative: str, repair_tool_relative: str) -> list[dict[str, Any]]:
    py = sys.executable
    return [
        _task(
            "inventory", TASKS[0][1],
            command=[py, repair_tool_relative, "inventory", repo_relative],
            acceptance=("exit_code==0", f"file_exists:{repo_relative}/artifacts/inventory.json"),
        ),
        _task(
            "diagnose", TASKS[1][1], depends_on=("inventory",),
            command=[py, repair_tool_relative, "diagnose", repo_relative],
            acceptance=("exit_code==0", f"file_exists:{repo_relative}/artifacts/diagnosis.json"),
        ),
        _task(
            "patch", TASKS[2][1], depends_on=("diagnose",),
            command=[py, repair_tool_relative, "patch", repo_relative],
            acceptance=(
                "exit_code==0",
                f"file_contains:{repo_relative}/src/demo_ros_pkg/demo_ros_pkg/controller.py:return left + right",
                f"file_exists:{repo_relative}/artifacts/patch.json",
            ),
        ),
        _task(
            "build", TASKS[3][1], depends_on=("patch",),
            command=[py, repair_tool_relative, "build", repo_relative],
            acceptance=("exit_code==0", f"file_exists:{repo_relative}/artifacts/build.json"),
        ),
        _task(
            "test", TASKS[4][1], depends_on=("build",),
            command=[py, repair_tool_relative, "test", repo_relative],
            acceptance=("exit_code==0",),
        ),
        _task(
            "report", TASKS[5][1], depends_on=("test",),
            command=[py, repair_tool_relative, "report", repo_relative],
            acceptance=(
                "exit_code==0",
                f"file_exists:{repo_relative}/artifacts/repair_report.md",
                f"file_contains:{repo_relative}/artifacts/repair_report.md:Final pytest: PASS",
            ),
        ),
    ]


def register_resources(chain: MosaicMainChain, *, agent_mode: str = "tool") -> None:
    provider_id = (os.getenv("MOSAIC_PROVIDER") or "deepseek").strip() or "deepseek"
    provider_base_url = (os.getenv("OPENAI_BASE_URL") or os.getenv("DEEPSEEK_BASE_URL") or "https://api.deepseek.com").rstrip("/")
    provider_model = (os.getenv("LLM_MODEL_NAME") or os.getenv("DEEPSEEK_MODEL") or "deepseek-v4-flash").strip() or "deepseek-v4-flash"
    agent_profile = AgentProfile(
        agent_id="ros-repair-agent",
        name="ROS repair executor",
        skills=("ros_repair",),
        endpoint="inproc://ros-repair-agent",
        max_load=1,
        reliability=0.95,
        tier=ExecutionTier.EDGE,
        labels=("scenario", "ros-repair"),
    )
    if agent_mode == "deepseek":
        adapter = LLMAgentAdapter(agent_profile.agent_id, base_url=provider_base_url, model_name=provider_model, allow_fallback=False)
        agent_profile = AgentProfile(
            agent_id=agent_profile.agent_id,
            name=f"ROS repair {provider_id} executor",
            skills=agent_profile.skills,
            endpoint=f"api+{provider_base_url}",
            max_load=agent_profile.max_load,
            reliability=agent_profile.reliability,
            tier=agent_profile.tier,
            labels=("scenario", "ros-repair", provider_id, "provider-api"),
        )
    elif agent_mode == "tool":
        adapter = TaskSpecAgent(agent_profile.agent_id)
    else:
        raise ValueError("agent_mode must be 'deepseek' or 'tool'")
    chain.registry_bridge.register(
        agent_profile,
        permissions=("shell.execute",),
        adapter=adapter,
    )
    # A second independently registered executor is intentional: it gives the
    # live Agent-offline experiment a real alternate candidate instead of
    # pretending one-agent recovery succeeded. In strict mode this is a second
    # provider-backed Agent identity; in offline benchmark mode it is a second
    # deterministic TaskSpec executor and is labelled as such by authenticity.
    backup_profile = AgentProfile(
        agent_id="ros-repair-agent-backup",
        name="ROS repair backup executor",
        skills=("ros_repair",),
        endpoint=f"api+{provider_base_url}" if agent_mode == "deepseek" else "inproc://ros-repair-agent-backup",
        max_load=1,
        reliability=0.94,
        tier=ExecutionTier.EDGE,
        labels=("scenario", "ros-repair", "backup") + ((provider_id, "provider-api") if agent_mode == "deepseek" else ("deterministic-tool",)),
    )
    backup_adapter = (
        LLMAgentAdapter(
            backup_profile.agent_id,
            base_url=provider_base_url,
            model_name=provider_model,
            allow_fallback=False,
        )
        if agent_mode == "deepseek"
        else TaskSpecAgent(backup_profile.agent_id)
    )
    chain.registry_bridge.register(
        backup_profile,
        permissions=("shell.execute",),
        adapter=backup_adapter,
    )
    chain.execution.register_actor(CapabilityProfile(
        actor_id="local-model",
        kind=ActorKind.MODEL,
        task_types=frozenset({"*"}),
        capabilities=frozenset({"*"}),
        permissions=frozenset({"*"}),
        reliability=0.98,
    ))
    chain.execution.register_actor(CapabilityProfile(
        actor_id="shell",
        kind=ActorKind.TOOL,
        task_types=frozenset({"*"}),
        capabilities=frozenset({"*"}),
        permissions=frozenset({"shell.execute"}),
        reliability=0.99,
    ))
    chain.execution.register_actor(CapabilityProfile(
        actor_id="local-device",
        kind=ActorKind.DEVICE,
        task_types=frozenset({"*"}),
        capabilities=frozenset({"*"}),
        permissions=frozenset({"*"}),
        reliability=0.99,
        capacity=2,
        device_location="local",
        metadata={"allowed_privacy_levels": ["normal", "public", "internal"]},
    ))


def run_scenario(workspace: str | Path, *, run_id: str | None = None, agent_mode: str = "tool", round_hook: Any | None = None) -> MainChainRunResult:
    workspace = Path(workspace).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    source_root = Path(__file__).resolve().parent
    repo_target = workspace / "ros_repo"
    if repo_target.exists():
        shutil.rmtree(repo_target)
    shutil.copytree(source_root / "fixture_repo", repo_target)
    tool_target = workspace / "repair_tool.py"
    shutil.copy2(source_root / "repair_tool.py", tool_target)

    chain = MosaicMainChain(workspace=workspace, scheduler_policy="ortools" if agent_mode == "deepseek" else "greedy")
    register_resources(chain, agent_mode=agent_mode)
    user_goal = (
        "自动修复给定 ROS 软件仓库中的可复现故障；不得跳过测试；"
        "所有修改必须留下证据并输出根因、补丁和复现报告。"
    )
    goalspec = compile_goal(user_goal, mode="rule")
    plan = build_plan("ros_repo", "repair_tool.py")
    graph = {
        "revision": 1,
        "scenario": "ros_repair",
        "nodes": plan,
        "edges": [
            {"from": parent, "to": node["task_id"], "type": "exec"}
            for node in plan for parent in node.get("predecessors", [])
        ],
    }
    result = chain.run_plan(
        goalspec=goalspec,
        taskgraph=graph,
        plan=plan,
        run_id=run_id,
        max_rounds=30,
        runtime_metadata={
            "scenario": "ros_repair",
            "agent_mode": "deepseek" if agent_mode == "deepseek" else "deterministic_tool_executor",
            "api_provider": (os.getenv("MOSAIC_PROVIDER", "deepseek").strip() or "deepseek") if agent_mode == "deepseek" else None,
        },
        round_hook=round_hook,
    )
    # Publish user-facing scenario outputs into the product's approved artifact
    # root.  The source fixture workspace remains internal; the Deliverables UI
    # can preview/download these copies without ever exposing an absolute path.
    source_artifacts = repo_target / "artifacts"
    publish_root = workspace / ".mosaic_deliverables" / result.run_id / "ros_repair"
    if source_artifacts.is_dir():
        publish_root.mkdir(parents=True, exist_ok=True)
        for artifact in source_artifacts.iterdir():
            if artifact.is_file():
                shutil.copy2(artifact, publish_root / artifact.name)
    return result
