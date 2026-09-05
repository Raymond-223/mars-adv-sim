"""Deterministic benchmark resources.

These adapters are intentionally *not* presented as real LLM Agents. They execute
TaskSpec metadata through the same scheduler / ToolRuntime / verifier / memory /
topology pipeline so offline performance experiments can run without an API key.
All produced snapshots are classified ``DETERMINISTIC_TOOL_EXECUTOR``.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Iterable

from mosaic_omega.agent_runtime.edge_cloud import ExecutionTier
from mosaic_omega.agent_runtime.models import AgentProfile
from mosaic_omega.execution_scheduler.adapters.task_spec_agent import TaskSpecAgent
from mosaic_omega.execution_scheduler.models import ActorKind, Assignment, CapabilityProfile, TaskNodeView, ToolCall
from mosaic_omega.goalspec import compile_goal
from mosaic_omega.todag import ToDAGEngine


class DeterministicContextAgent(TaskSpecAgent):
    """Auditable deterministic executor that consumes the real context/topology path."""

    authenticity_mode = "deterministic_tool_executor"

    def __init__(self, actor_id: str, chain) -> None:
        super().__init__(actor_id)
        self.chain = chain

    def plan(self, task: TaskNodeView, assignment: Assignment, trace_id: str) -> list[ToolCall]:
        context = self.chain.prepare_agent_context(task, assignment, trace_id)
        calls = super().plan(task, assignment, trace_id)
        enriched: list[ToolCall] = []
        for call in calls:
            arguments = dict(call.arguments)
            if call.tool_name == "task":
                arguments.setdefault("description", task.description)
                arguments.setdefault("acceptance_conditions", list(task.acceptance_conditions))
                # Offline benchmark-only semantic acceptance bypass.  The verifier
                # records TEST_FIXTURE_ONLY; production Agents never emit this flag.
                arguments["test_fixture_verifier"] = True
            arguments["context_pack"] = context["context_pack"]
            arguments["low_entropy_messages"] = context["messages"]
            arguments["execution_provenance"] = {
                "mode": "deterministic_benchmark_executor",
                "competition_real_api": False,
            }
            enriched.append(replace(call, arguments=arguments))
        return enriched


def register_deterministic_benchmark_resources(chain, required_skills: Iterable[str]) -> None:
    skills = sorted({str(skill).strip() for skill in required_skills if str(skill).strip()}) or ["general"]
    existing = {profile.actor_id for profile in chain.execution.capabilities.list()}
    for skill in skills:
        actor_id = f"benchmark-deterministic-{skill}"
        if actor_id in existing:
            continue
        profile = AgentProfile(
            agent_id=actor_id,
            name=f"Deterministic benchmark {skill} executor",
            skills=(skill,),
            endpoint=f"inproc://{actor_id}",
            max_load=2,
            reliability=0.99,
            tier=ExecutionTier.EDGE,
            labels=("benchmark", "deterministic", "not-real-api"),
        )
        chain.registry_bridge.register(
            profile,
            permissions=("*",),
            adapter=DeterministicContextAgent(actor_id, chain),
        )
        existing.add(actor_id)

    generalist = "benchmark-deterministic-generalist"
    if generalist not in existing:
        profile = AgentProfile(
            agent_id=generalist,
            name="Deterministic benchmark generalist",
            skills=tuple(skills),
            endpoint=f"inproc://{generalist}",
            max_load=1,
            reliability=0.99,
            tier=ExecutionTier.EDGE,
            labels=("benchmark", "deterministic", "not-real-api", "generalist"),
        )
        chain.registry_bridge.register(
            profile,
            permissions=("*",),
            fixed_cost=5.0,
            adapter=DeterministicContextAgent(generalist, chain),
        )
        existing.add(generalist)

    if "benchmark-deterministic-model" not in existing:
        chain.execution.register_actor(CapabilityProfile(
            actor_id="benchmark-deterministic-model",
            kind=ActorKind.MODEL,
            task_types=frozenset({"*"}),
            capabilities=frozenset({"*"}),
            permissions=frozenset({"*"}),
            reliability=0.99,
            context_limit=100_000,
            cost_per_token=0.0,
            metadata={"model_type": "deterministic_benchmark_placeholder", "real_llm": False},
        ))
        existing.add("benchmark-deterministic-model")
    if "task" not in existing:
        chain.execution.register_actor(CapabilityProfile(
            actor_id="task",
            kind=ActorKind.TOOL,
            task_types=frozenset({"*"}),
            capabilities=frozenset({"*"}),
            permissions=frozenset({"*"}),
            reliability=0.99,
            metadata={"benchmark_tool": True},
        ))
        existing.add("task")
    if "benchmark-local-device" not in existing:
        chain.execution.register_actor(CapabilityProfile(
            actor_id="benchmark-local-device",
            kind=ActorKind.DEVICE,
            task_types=frozenset({"*"}),
            capabilities=frozenset({"*"}),
            permissions=frozenset({"*"}),
            reliability=0.99,
            capacity=max(4, len(skills) + 1),
            device_location="local",
            metadata={"allowed_privacy_levels": ["normal", "public", "internal", "confidential", "restricted"]},
        ))


def run_deterministic_benchmark_chain(chain, user_goal: str, *, run_id: str, planning_horizon: int = 10, max_rounds: int = 100):
    goalspec = compile_goal(user_goal, mode="rule")
    planner = ToDAGEngine(planning_horizon=planning_horizon)
    taskgraph = planner.build(goalspec)
    plan = planner.execution_plan()
    required_skills = [
        skill
        for node in plan
        for skill in node.get("required_skills", (node.get("required_skill"),))
        if skill
    ]
    register_deterministic_benchmark_resources(chain, required_skills)
    return chain.run_plan(
        goalspec=goalspec,
        taskgraph=taskgraph,
        plan=plan,
        run_id=run_id,
        max_rounds=max_rounds,
        auto_register_deepseek_resources=False,
        runtime_metadata={
            "goalspec_mode": "rule",
            "agent_mode": "deterministic_tool_executor",
            "competition_real_api": False,
            "measurement_purpose": "offline benchmark",
        },
    )
