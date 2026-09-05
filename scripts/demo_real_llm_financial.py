# -*- coding: utf-8 -*-
"""MOSAIC-Ω Live LLM API Verification with Financial Scenario."""
import sys
from pathlib import Path
import time
import os

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mosaic_omega.integration import MosaicMainChain
from mosaic_omega.agent_runtime.models import AgentProfile, ExecutionTier
from mosaic_omega.execution_scheduler.models import CapabilityProfile, ActorKind
from mosaic_omega.execution_scheduler.adapters.llm_agent import LLMAgentAdapter
from scenarios.financial_research.runner import build_plan, compile_goal

import shutil

def register_real_llm_resources(chain: MosaicMainChain) -> None:
    # Use real LLMAgentAdapter for the 3 tiers
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

    llm_adapter = LLMAgentAdapter(actor_id="shared")
    if not llm_adapter._client:
        print("[错误] 真实大模型 API 客户端初始化失败！请检查密钥。")
        sys.exit(1)
        
    print(f"[配置信息] 使用模型: {llm_adapter.model_name}")
    print(f"[配置信息] API端点: {llm_adapter.base_url}")
    print(f"[配置信息] 密钥长度: {len(llm_adapter.api_key)}")

    for profile in (device_agent, edge_agent, cloud_agent):
        chain.registry_bridge.register(
            profile,
            permissions=("shell.execute",),
            adapter=LLMAgentAdapter(actor_id=profile.agent_id),
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


def run_real_llm_demo():
    print("==========================================================================")
    print("      MOSAIC-Ω 跨域金融场景 (真实 API 连通性压测)")
    print("==========================================================================")
    
    workspace = Path(".llm_test_workspace").resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    
    source_root = Path(__file__).resolve().parent.parent / "scenarios" / "financial_research"
    fin_ws = workspace / "fin_research_workspace"
    if fin_ws.exists():
        shutil.rmtree(fin_ws)
    fin_ws.mkdir(parents=True, exist_ok=True)

    data_dir = fin_ws / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source_root / "fixture_data", data_dir, dirs_exist_ok=True)

    tool_target = fin_ws / "research_tool.py"
    shutil.copy2(source_root / "research_tool.py", tool_target)

    chain = MosaicMainChain(workspace=workspace, scheduler_policy="greedy")
    register_real_llm_resources(chain)

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

    print(f"\n[任务派发] {user_goal}")
    print("[运行中] 正在通过真实的 DeepSeek API 进行端-边-云跨域协同规划，请稍候...\n")
    
    start_time = time.time()
    
    result = chain.run_plan(
        goalspec=goalspec,
        taskgraph=graph,
        plan=plan,
        max_rounds=30,
        run_id="real-llm-fin-run-1"
    )
    
    elapsed = time.time() - start_time
    print(f"\n[执行完毕] 耗时: {elapsed:.2f} 秒")
    print(f"[执行结果] 所有任务成功: {result.all_succeeded}")
    print(f"[执行结果] 任务总数: {len(result.tasks)} / 成功数: {len(result.completed_task_ids)}")
    
    print("\n--- 真实大模型下发的策略规划 (ToolCalls) ---")
    for event_obj in chain.execution.events.events(run_id="real-llm-fin-run-1"):
        event = event_obj.to_dict() if hasattr(event_obj, "to_dict") else dict(event_obj) if isinstance(event_obj, dict) else vars(event_obj)
        if event.get("type") == "TOOL_EXECUTED" or getattr(event_obj, "event_type", "") == "TOOL_EXECUTED":
            payload = getattr(event_obj, "payload", event.get("payload", {}))
            print(f"  [Agent: {payload.get('actor_id')}] 调用工具: {payload.get('tool_name')}")
            print(f"    Result: {payload.get('result', {}).get('output')}")
            print(f"    Error: {payload.get('result', {}).get('error')}")
            
    print("\n--- 任务失败详情 ---")
    for task in result.tasks:
        if task.get("state") != "SUCCEEDED":
            print(f"  Task {task.get('task_id')}: {task.get('state')}")
            for event_obj in chain.execution.events.events(run_id="real-llm-fin-run-1"):
                event = event_obj.to_dict() if hasattr(event_obj, "to_dict") else dict(event_obj) if isinstance(event_obj, dict) else vars(event_obj)
                event_type = event.get("type") or getattr(event_obj, "event_type", "")
                node_id = event.get("node_id") or getattr(event_obj, "node_id", "")
                if node_id == task.get('task_id') and event_type == "TASK_FAILED":
                    payload = getattr(event_obj, "payload", event.get("payload", {}))
                    print(f"    Error: {payload.get('error_reason')}")

    print("\n==========================================================================")
    print("      真实大模型 API 端到端测试完成")
    print("==========================================================================")


if __name__ == "__main__":
    run_real_llm_demo()
