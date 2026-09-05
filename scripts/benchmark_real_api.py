# -*- coding: utf-8 -*-
"""MOSAIC-Ω Comprehensive Real API Benchmark (All Scenarios)."""

import sys
import time
import json
import shutil
from pathlib import Path
from dataclasses import dataclass, asdict

# Add src to python path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mosaic_omega.integration import MosaicMainChain
from mosaic_omega.agent_runtime.models import AgentProfile, ExecutionTier, AgentStatus
from mosaic_omega.execution_scheduler.models import CapabilityProfile, ActorKind, ErrorClass
from mosaic_omega.execution_scheduler.adapters.llm_agent import LLMAgentAdapter

from scenarios.ros_repair.runner import build_plan as build_ros_plan, compile_goal as compile_ros_goal
from scenarios.financial_research.runner import build_plan as build_fin_plan, compile_goal as compile_fin_goal


def reset_tokens():
    LLMAgentAdapter.total_api_tokens = 0
    LLMAgentAdapter.total_api_calls = 0

def get_tokens():
    return LLMAgentAdapter.total_api_calls, LLMAgentAdapter.total_api_tokens

def _register_shared_resources(chain: MosaicMainChain):
    chain.execution.register_actor(CapabilityProfile(
        actor_id="local-model", kind=ActorKind.MODEL, task_types=frozenset({"*"}),
        capabilities=frozenset({"*"}), permissions=frozenset({"*"}), reliability=0.98,
    ))
    chain.execution.register_actor(CapabilityProfile(
        actor_id="shell", kind=ActorKind.TOOL, task_types=frozenset({"*"}),
        capabilities=frozenset({"*"}), permissions=frozenset({"shell.execute"}), reliability=0.99,
    ))
    chain.execution.register_actor(CapabilityProfile(
        actor_id="local-device", kind=ActorKind.DEVICE, task_types=frozenset({"*"}),
        capabilities=frozenset({"*"}), permissions=frozenset({"*"}), reliability=0.99,
        capacity=4, device_location="local",
        metadata={"allowed_privacy_levels": ["normal", "public", "internal", "confidential"]},
    ))


def run_ros_repair_api(workspace: Path):
    print("\n--- Running ROS Repair Scenario (API) ---")
    reset_tokens()
    
    ws = workspace / "ros_workspace"
    if ws.exists(): shutil.rmtree(ws)
    ws.mkdir(parents=True, exist_ok=True)
    
    chain = MosaicMainChain(workspace=ws, scheduler_policy="greedy")
    
    agent_profile = AgentProfile(
        agent_id="ros-repair-agent", name="ROS repair executor", skills=("ros_repair",),
        endpoint="inproc://ros-repair-agent", max_load=1, reliability=0.95, tier=ExecutionTier.EDGE,
    )
    chain.registry_bridge.register(agent_profile, permissions=("shell.execute",), adapter=LLMAgentAdapter(actor_id="ros-repair-agent"))
    _register_shared_resources(chain)
    
    source_root = Path(__file__).resolve().parent.parent / "scenarios" / "ros_repair"
    shutil.copytree(source_root / "fixture_repo", ws / "fixture_repo", dirs_exist_ok=True)
    shutil.copy2(source_root / "repair_tool.py", ws / "repair_tool.py")

    goalspec = compile_ros_goal("修复 ROS 机器人导航控制模块，必须通过单元测试，不得修改公共 API 接口", mode="rule")
    plan = build_ros_plan("fixture_repo", "repair_tool.py")
    graph = {
        "revision": 1, "scenario": "ros_repair", "nodes": plan,
        "edges": [{"from": parent, "to": node["task_id"], "type": "exec"} for node in plan for parent in node.get("predecessors", [])]
    }
    
    start = time.time()
    result = chain.run_plan(goalspec=goalspec, taskgraph=graph, plan=plan, run_id="run-ros")
    elapsed = time.time() - start
    
    if not result.all_succeeded:
        print("  [ERROR] ROS tasks failed:")
        for task in result.tasks:
            if task.get("state") != "SUCCEEDED":
                for e_obj in chain.execution.events.events(run_id="run-ros"):
                    e = e_obj.to_dict() if hasattr(e_obj, "to_dict") else dict(e_obj) if isinstance(e_obj, dict) else vars(e_obj)
                    if (e.get("node_id") or getattr(e_obj, "node_id", "")) == task.get('task_id') and (e.get("type") or getattr(e_obj, "event_type", "")) == "TASK_FAILED":
                        payload = getattr(e_obj, "payload", e.get("payload", {}))
                        print(f"    {task.get('task_id')}: {payload.get('error_reason')}")

    calls, tokens = get_tokens()
    return {
        "scenario": "ROS Repair",
        "success": result.all_succeeded,
        "tasks_run": len(result.tasks),
        "time_s": elapsed,
        "api_calls": calls,
        "api_tokens": tokens,
    }


def run_financial_research_api(workspace: Path):
    print("\n--- Running Financial Research Scenario (API) ---")
    reset_tokens()
    
    ws = workspace / "fin_test_root"
    if ws.exists(): shutil.rmtree(ws)
    ws.mkdir(parents=True, exist_ok=True)
    
    chain = MosaicMainChain(workspace=ws, scheduler_policy="greedy")
    
    import sys
    if hasattr(sys.modules[__name__], "_chain_hook"):
        sys.modules[__name__]._chain_hook(chain)
    
    for tier, name, agent_id in [
        (ExecutionTier.DEVICE, "Device Decrypt", "fin-device"),
        (ExecutionTier.EDGE, "Edge Sentiment", "fin-edge"),
        (ExecutionTier.CLOUD, "Cloud Risk", "fin-cloud"),
    ]:
        prof = AgentProfile(agent_id=agent_id, name=name, skills=("financial_research",), endpoint=f"inproc://{agent_id}", max_load=2, tier=tier)
        chain.registry_bridge.register(prof, permissions=("shell.execute",), adapter=LLMAgentAdapter(actor_id=agent_id))
    
    _register_shared_resources(chain)
    
    source_root = Path(__file__).resolve().parent.parent / "scenarios" / "financial_research"
    
    target_ws = ws / "fin_research_workspace"
    target_ws.mkdir(parents=True, exist_ok=True)
    
    data_dir = target_ws / "data"
    shutil.copytree(source_root / "fixture_data", data_dir, dirs_exist_ok=True)
    shutil.copy2(source_root / "research_tool.py", target_ws / "research_tool.py")

    goalspec = compile_fin_goal("对星海量子执行跨域量化分析；数据在端侧解密；生成风控建模", mode="rule")
    plan = build_fin_plan("fin_research_workspace/research_tool.py")
    graph = {
        "revision": 1, "scenario": "financial_research", "nodes": plan,
        "edges": [{"from": parent, "to": node["task_id"], "type": "exec"} for node in plan for parent in node.get("predecessors", [])]
    }
    
    start = time.time()
    result = chain.run_plan(goalspec=goalspec, taskgraph=graph, plan=plan, run_id="run-fin")
    elapsed = time.time() - start
    
    if not result.all_succeeded:
        print("  [ERROR] Financial tasks failed:")
        for t in result.tasks:
            print(f"TASK {t.get('task_id')}: {t.get('state')}")

    calls, tokens = get_tokens()
    return {
        "scenario": "Financial Research",
        "success": result.all_succeeded,
        "tasks_run": len(result.tasks),
        "time_s": elapsed,
        "api_calls": calls,
        "api_tokens": tokens,
    }


def run_full_chaos_api(workspace: Path):
    """Fault-injection benchmark whose task Agents are all strict DeepSeek API adapters.

    No MockAgent is registered on this path. API errors fail visibly through the
    authoritative recovery path rather than silently switching executors.
    """
    print("\n--- Running Full Chaos Fault-Injection (STRICT REAL API) ---")
    reset_tokens()
    ws = workspace / "chaos_workspace"
    if ws.exists():
        shutil.rmtree(ws)
    ws.mkdir(parents=True, exist_ok=True)

    chain = MosaicMainChain(workspace=ws, scheduler_policy="greedy")
    start = time.time()
    initial_goal = "修复 ROS 机器人导航控制模块，必须通过单元测试，不得修改公共 API 接口。"
    run_id = "chaos-run"

    # Baseline: DeepSeekAgent is explicitly selected; this path never registers MockAgent.
    res1 = chain.run(initial_goal, run_id=run_id, agent_mode="deepseek")

    assigned = [
        (task.get("assignment") or {}).get("agent_id")
        for task in res1.tasks
        if isinstance(task.get("assignment"), dict)
    ]
    target_agent = next((actor for actor in assigned if actor), None)
    if target_agent is None:
        raise RuntimeError("strict chaos benchmark has no assigned real agent")

    # Agent-offline control-plane recovery uses the same registered real adapter.
    chain.registry_bridge.offline(target_agent)
    first_task = res1.tasks[0].get("node_id") or res1.tasks[0].get("task_id")
    plan_rec = chain.execution.recovery.plan(
        run_id=run_id,
        task_id=first_task,
        error_class=ErrorClass.REPLACEABLE,
        reason="injected real-agent offline",
    )
    chain.registry_bridge.heartbeat(target_agent, status=AgentStatus.ONLINE)

    # Tool-failure recovery plan is event/control logic; no execution result is fabricated.
    second_task = res1.tasks[min(1, len(res1.tasks)-1)].get("node_id") or res1.tasks[min(1, len(res1.tasks)-1)].get("task_id")
    plan_tool = chain.execution.recovery.plan(
        run_id=run_id,
        task_id=second_task,
        error_class=ErrorClass.RETRYABLE,
        reason="injected tool exit code 127",
    )

    # Requirement mutation is a second strict real-API run.
    mutated_goal = initial_goal + "，新增内存限制 < 500MB。"
    res_mut = chain.run(mutated_goal, run_id="chaos-mut", agent_mode="deepseek")

    elapsed = time.time() - start
    # DeepSeekAgent usage is recorded in TOOL_EXECUTED api_provenance; LLMAgentAdapter
    # class counters are not authoritative for this DeepSeekAgent path.
    events = [
        e.to_dict() if hasattr(e, "to_dict") else dict(e)
        for rid in (run_id, "chaos-mut")
        for e in chain.execution.events.events(run_id=rid)
    ]
    api_calls = []
    api_tokens = 0
    for event in events:
        if (event.get("type") or event.get("event_type")) != "TOOL_EXECUTED":
            continue
        prov = (((event.get("payload") or {}).get("tool_call") or {}).get("arguments") or {}).get("api_provenance")
        if not isinstance(prov, dict):
            continue
        api_calls.append(prov)
        usage = prov.get("usage") or {}
        if isinstance(usage.get("total_tokens"), (int, float)):
            api_tokens += int(usage["total_tokens"])

    success = (
        res1.all_succeeded
        and res_mut.all_succeeded
        and len(plan_rec.affected_task_ids) > 0
        and len(plan_tool.affected_task_ids) > 0
        and len(api_calls) > 0
    )
    return {
        "scenario": "Full Chaos Fault-Injection (Strict Real API)",
        "success": success,
        "tasks_run": len(res1.tasks) + len(res_mut.tasks),
        "time_s": elapsed,
        "api_calls": len(api_calls),
        "api_tokens": api_tokens,
        "agent_authenticity": "real_api_only",
        "mock_agent_count": 0,
    }


def main():
    print("==========================================================================")
    print("      MOSAIC-Ω Real LLM API Benchmark Suite")
    print("==========================================================================")
    
    workspace = Path(".real_api_benchmark_ws").resolve()
    results = []
    
    adapter = LLMAgentAdapter(actor_id="test")
    if not adapter._client:
        print("ERROR: No valid DEEPSEEK_API_KEY found in process environment")
        sys.exit(1)
        
    try:
        res1 = run_ros_repair_api(workspace)
        print(json.dumps(res1, indent=2))
        results.append(res1)
        
        res2 = run_financial_research_api(workspace)
        print(json.dumps(res2, indent=2))
        results.append(res2)
        
        res3 = run_full_chaos_api(workspace)
        print(json.dumps(res3, indent=2))
        results.append(res3)
    except Exception as e:
        import traceback
        traceback.print_exc()
        
    summary = {
        "timestamp": time.time(),
        "model": adapter.model_name,
        "total_time_s": sum(r["time_s"] for r in results),
        "total_api_calls": sum(r["api_calls"] for r in results),
        "total_api_tokens": sum(r["api_tokens"] for r in results),
        "scenarios": results,
    }
    
    out_dir = Path("experiments/results")
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / "real_api_benchmark.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)
        
    print("\n[Benchmark Complete] Saved to experiments/results/real_api_benchmark.json")

if __name__ == "__main__":
    main()
