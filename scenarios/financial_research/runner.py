# -*- coding: utf-8 -*-
"""Scenario B Runner: Cross-Domain Multi-Source Financial Research & Risk Analysis."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Any

from mosaic_omega.agent_runtime.edge_cloud import ExecutionTier
from mosaic_omega.agent_runtime.models import AgentProfile
from mosaic_omega.execution_scheduler.adapters.task_spec_agent import TaskSpecAgent
from mosaic_omega.execution_scheduler.adapters.llm_agent import LLMAgentAdapter
from mosaic_omega.execution_scheduler.models import ActorKind, CapabilityProfile
from mosaic_omega.goalspec import compile_goal
from mosaic_omega.integration.main_chain import MainChainRunResult, MosaicMainChain

TASKS = (
    ("ingest", "Ingest multi-source market news and company filing metadata"),
    ("decrypt", "Decrypt confidential financial metrics locally on DEVICE tier"),
    ("sentiment", "Analyze market sentiment using NLP on EDGE tier"),
    ("risk_modeling", "Compute Monte Carlo VaR & Sharpe ratio on CLOUD tier"),
    ("compliance", "Audit privacy compliance and risk constraints"),
    ("report", "Synthesize evidence-backed final Investment Research Report"),
)


def _task(
    task_id: str,
    description: str,
    *,
    depends_on: tuple[str, ...] = (),
    command: list[str],
    acceptance: tuple[str, ...],
    tier: str = "edge",
    privacy: str = "normal",
) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "node_id": task_id,
        "type": "financial_research",
        "description": description,
        "predecessors": list(depends_on),
        "required_capabilities": ["financial_research"],
        "required_permissions": ["shell.execute"],
        "acceptance": list(acceptance),
        "risk": "normal",
        "inputs": {},
        "outputs": {},
        "evidence_dependencies": [],
        "resource_requirements": {"device_location": "local"},
        "metadata": {
            "execution_tier": tier,
            "privacy_level": privacy,
            "tool": {
                "name": "shell",
                "arguments": {"command": command},
                "timeout_s": 25,
            },
        },
    }


def build_plan(research_tool_relative: str) -> list[dict[str, Any]]:
    # Spawn the *running* interpreter by absolute path, as scenarios/ros_repair
    # does. A bare basename requires that name to be resolvable on PATH, which
    # fails on macOS (no `python` since 12.3), on python3-only distributions and
    # under a non-activated venv. The command allowlist is unaffected: it matches
    # on Path(command[0]).name, so an absolute path is still checked.
    py = sys.executable
    return [
        _task(
            "ingest",
            TASKS[0][1],
            command=[py, research_tool_relative, "ingest", "fin_research_workspace"],
            acceptance=("exit_code==0", "file_exists:fin_research_workspace/artifacts/ingest.json"),
            tier="edge",
            privacy="public",
        ),
        _task(
            "decrypt",
            TASKS[1][1],
            depends_on=("ingest",),
            command=[py, research_tool_relative, "decrypt", "fin_research_workspace"],
            acceptance=(
                "exit_code==0",
                "file_exists:fin_research_workspace/artifacts/decrypted_financials.json",
                "file_contains:fin_research_workspace/artifacts/decrypted_financials.json:DEVICE_ENCLAVE_VERIFIED",
            ),
            tier="device",
            privacy="confidential",
        ),
        _task(
            "sentiment",
            TASKS[2][1],
            depends_on=("ingest",),
            command=[py, research_tool_relative, "sentiment", "fin_research_workspace"],
            acceptance=("exit_code==0", "file_exists:fin_research_workspace/artifacts/sentiment_analysis.json"),
            tier="edge",
            privacy="normal",
        ),
        _task(
            "risk_modeling",
            TASKS[3][1],
            depends_on=("decrypt", "sentiment"),
            command=[py, research_tool_relative, "risk_modeling", "fin_research_workspace"],
            acceptance=("exit_code==0", "file_exists:fin_research_workspace/artifacts/risk_model.json"),
            tier="cloud",
            privacy="internal",
        ),
        _task(
            "compliance",
            TASKS[4][1],
            depends_on=("risk_modeling",),
            command=[py, research_tool_relative, "compliance", "fin_research_workspace"],
            acceptance=(
                "exit_code==0",
                "file_exists:fin_research_workspace/artifacts/compliance_audit.json",
                "file_contains:fin_research_workspace/artifacts/compliance_audit.json:PASSED",
            ),
            tier="edge",
            privacy="internal",
        ),
        _task(
            "report",
            TASKS[5][1],
            depends_on=("compliance",),
            command=[py, research_tool_relative, "report", "fin_research_workspace"],
            acceptance=(
                "exit_code==0",
                "file_exists:fin_research_workspace/artifacts/investment_research_report.md",
                "file_contains:fin_research_workspace/artifacts/investment_research_report.md:审计结论",
            ),
            tier="cloud",
            privacy="public",
        ),
    ]


def register_resources(chain: MosaicMainChain, *, agent_mode: str = "tool") -> None:
    provider_id = (os.getenv("MOSAIC_PROVIDER") or "deepseek").strip() or "deepseek"
    provider_base_url = (os.getenv("OPENAI_BASE_URL") or os.getenv("DEEPSEEK_BASE_URL") or "https://api.deepseek.com").rstrip("/")
    provider_model = (os.getenv("LLM_MODEL_NAME") or os.getenv("DEEPSEEK_MODEL") or "deepseek-v4-flash").strip() or "deepseek-v4-flash"
    # Register 3 heterogeneous tier agents: Device, Edge, Cloud
    device_agent = AgentProfile(
        agent_id="fin-device-agent",
        name="Device Confidential Decrypt Agent",
        skills=("financial_research",),
        endpoint="inproc://fin-device-agent",
        max_load=1,
        reliability=0.99,
        tier=ExecutionTier.DEVICE,
        labels=("scenario", "financial_research", "device"),
    )
    edge_agent = AgentProfile(
        agent_id="fin-edge-agent",
        name="Edge Sentiment & Ingest Agent",
        skills=("financial_research",),
        endpoint="inproc://fin-edge-agent",
        max_load=2,
        reliability=0.96,
        tier=ExecutionTier.EDGE,
        labels=("scenario", "financial_research", "edge"),
    )
    cloud_agent = AgentProfile(
        agent_id="fin-cloud-agent",
        name="Cloud Heavy Risk Modeling Agent",
        skills=("financial_research",),
        endpoint="inproc://fin-cloud-agent",
        max_load=4,
        reliability=0.98,
        tier=ExecutionTier.CLOUD,
        labels=("scenario", "financial_research", "cloud"),
    )

    for profile in (device_agent, edge_agent, cloud_agent):
        if agent_mode == "deepseek":
            adapter = LLMAgentAdapter(profile.agent_id, base_url=provider_base_url, model_name=provider_model, allow_fallback=False)
            profile = AgentProfile(
                agent_id=profile.agent_id,
                name=profile.name.replace("Agent", f"{provider_id} Agent"),
                skills=profile.skills,
                endpoint=f"api+{provider_base_url}",
                max_load=profile.max_load,
                reliability=profile.reliability,
                tier=profile.tier,
                labels=tuple(profile.labels) + (provider_id, "provider-api"),
            )
        elif agent_mode == "tool":
            adapter = TaskSpecAgent(profile.agent_id)
        else:
            raise ValueError("agent_mode must be 'deepseek' or 'tool'")
        chain.registry_bridge.register(
            profile,
            permissions=("shell.execute",),
            adapter=adapter,
        )

    chain.execution.register_actor(
        CapabilityProfile(
            actor_id="local-model",
            kind=ActorKind.MODEL,
            task_types=frozenset({"*"}),
            capabilities=frozenset({"*"}),
            permissions=frozenset({"*"}),
            reliability=0.98,
        )
    )
    chain.execution.register_actor(
        CapabilityProfile(
            actor_id="shell",
            kind=ActorKind.TOOL,
            task_types=frozenset({"*"}),
            capabilities=frozenset({"*"}),
            permissions=frozenset({"shell.execute"}),
            reliability=0.99,
        )
    )
    chain.execution.register_actor(
        CapabilityProfile(
            actor_id="local-device",
            kind=ActorKind.DEVICE,
            task_types=frozenset({"*"}),
            capabilities=frozenset({"*"}),
            permissions=frozenset({"*"}),
            reliability=0.99,
            capacity=4,
            device_location="local",
            metadata={"allowed_privacy_levels": ["normal", "public", "internal", "confidential"]},
        )
    )


def run_scenario(workspace: str | Path, *, run_id: str | None = None, agent_mode: str = "tool", round_hook: Any | None = None) -> MainChainRunResult:
    workspace = Path(workspace).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    source_root = Path(__file__).resolve().parent

    fin_ws = workspace / "fin_research_workspace"
    if fin_ws.exists():
        shutil.rmtree(fin_ws)
    fin_ws.mkdir(parents=True, exist_ok=True)

    data_dir = fin_ws / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source_root / "fixture_data", data_dir, dirs_exist_ok=True)

    tool_target = fin_ws / "research_tool.py"
    shutil.copy2(source_root / "research_tool.py", tool_target)

    chain = MosaicMainChain(workspace=workspace, scheduler_policy="ortools" if agent_mode == "deepseek" else "greedy")
    register_resources(chain, agent_mode=agent_mode)

    user_goal = (
        "对星海量子智能科技多源研报与财务数据执行跨域量化分析；"
        "核心敏感财务数据必须在端侧 DEVICE 解密；"
        "必须完成风控建模与合规审计，生成包含 Hash 证据链的投研评估报告。"
    )
    goalspec = compile_goal(user_goal, mode="rule")
    plan = build_plan("fin_research_workspace/research_tool.py")

    graph = {
        "revision": 1,
        "scenario": "financial_research",
        "nodes": plan,
        "edges": [
            {"from": parent, "to": node["task_id"], "type": "exec"}
            for node in plan
            for parent in node.get("predecessors", [])
        ],
    }

    result = chain.run_plan(
        goalspec=goalspec,
        taskgraph=graph,
        plan=plan,
        run_id=run_id,
        max_rounds=30,
        runtime_metadata={
            "scenario": "financial_research",
            "agent_mode": "deepseek" if agent_mode == "deepseek" else "deterministic_tool_executor",
            "api_provider": (os.getenv("MOSAIC_PROVIDER", "deepseek").strip() or "deepseek") if agent_mode == "deepseek" else None,
        },
        round_hook=round_hook,
    )
    # Publish verified scenario outputs into the product-approved deliverable
    # root. The source fixture workspace stays internal and is never surfaced as
    # a machine path to the browser.
    source_artifacts = fin_ws / "artifacts"
    publish_root = workspace / ".mosaic_deliverables" / result.run_id / "financial_research"
    if source_artifacts.is_dir():
        publish_root.mkdir(parents=True, exist_ok=True)
        for artifact in source_artifacts.iterdir():
            if artifact.is_file():
                shutil.copy2(artifact, publish_root / artifact.name)
    return result
