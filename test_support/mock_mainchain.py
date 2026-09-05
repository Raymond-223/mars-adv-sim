"""TEST-ONLY deterministic main-chain fixture.

This module is deliberately outside ``src/mosaic_omega`` so production imports,
CLI defaults and competition runtime code do not contain or register MockAgent.
Every snapshot produced through this helper remains classified MOCK_EXECUTION.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Iterable

from mosaic_omega.agent_runtime.edge_cloud import ExecutionTier
from mosaic_omega.agent_runtime.models import AgentProfile
from test_support.mock_agent import MockAgent
from mosaic_omega.execution_scheduler.models import ActorKind, Assignment, CapabilityProfile, TaskNodeView, ToolCall
from mosaic_omega.goalspec import compile_goal
from mosaic_omega.todag import ToDAGEngine


class MemoryTopologyTestMock(MockAgent):
    """Deterministic test fixture that consumes real Memory/Topology context."""

    def __init__(self, actor_id: str, chain) -> None:
        super().__init__(actor_id)
        self.chain = chain

    def plan(self, task: TaskNodeView, assignment: Assignment, trace_id: str) -> list[ToolCall]:
        context = self.chain.prepare_agent_context(task, assignment, trace_id)
        calls = super().plan(task, assignment, trace_id)
        enriched: list[ToolCall] = []
        for call in calls:
            arguments = dict(call.arguments)
            arguments["context_pack"] = context["context_pack"]
            arguments["low_entropy_messages"] = context["messages"]
            enriched.append(replace(call, arguments=arguments))
        return enriched


def register_test_mock_resources(chain, required_skills: Iterable[str]) -> None:
    """Register explicit test fixtures; never use for competition evidence."""
    skills = sorted({str(skill).strip() for skill in required_skills if str(skill).strip()}) or ["general"]
    existing = {profile.actor_id for profile in chain.execution.capabilities.list()}
    for skill in skills:
        actor_id = f"test-mock-agent-{skill}"
        if actor_id in existing:
            continue
        runtime_profile = AgentProfile(
            agent_id=actor_id,
            name=f"TEST-ONLY Mock {skill} agent",
            skills=(skill,),
            endpoint=f"test-inproc://{actor_id}",
            max_load=2,
            reliability=0.95,
            tier=ExecutionTier.EDGE,
            labels=("test-only", "mock"),
        )
        chain.registry_bridge.register(
            runtime_profile,
            permissions=("*",),
            adapter=MemoryTopologyTestMock(runtime_profile.agent_id, chain),
        )
        existing.add(actor_id)

    generalist_id = "test-mock-agent-generalist"
    if generalist_id not in existing:
        runtime_profile = AgentProfile(
            agent_id=generalist_id,
            name="TEST-ONLY multi-skill mock generalist",
            skills=tuple(skills),
            endpoint=f"test-inproc://{generalist_id}",
            max_load=1,
            reliability=0.90,
            tier=ExecutionTier.EDGE,
            labels=("test-only", "mock", "generalist"),
        )
        chain.registry_bridge.register(
            runtime_profile,
            permissions=("*",),
            fixed_cost=5.0,
            adapter=MemoryTopologyTestMock(runtime_profile.agent_id, chain),
        )
        existing.add(generalist_id)

    if "test-mock-model" not in existing:
        chain.execution.register_actor(CapabilityProfile(
            actor_id="test-mock-model",
            kind=ActorKind.MODEL,
            task_types=frozenset({"*"}),
            capabilities=frozenset({"*"}),
            permissions=frozenset({"*"}),
            reliability=0.95,
            context_limit=100_000,
            cost_per_token=0.000001,
            metadata={"test_only": True},
        ))
        existing.add("test-mock-model")
    if "task" not in existing:
        chain.execution.register_actor(CapabilityProfile(
            actor_id="task",
            kind=ActorKind.TOOL,
            task_types=frozenset({"*"}),
            capabilities=frozenset({"*"}),
            permissions=frozenset({"*"}),
            reliability=0.99,
            metadata={"test_only_fixture_tool": True},
        ))
        existing.add("task")
    if "test-local-device" not in existing:
        chain.execution.register_actor(CapabilityProfile(
            actor_id="test-local-device",
            kind=ActorKind.DEVICE,
            task_types=frozenset({"*"}),
            capabilities=frozenset({"*"}),
            permissions=frozenset({"*"}),
            reliability=0.99,
            capacity=max(4, len(skills) + 1),
            device_location="local",
            metadata={
                "test_only": True,
                "allowed_privacy_levels": ["normal", "public", "internal", "confidential", "restricted"],
            },
        ))


def run_test_mock(chain, user_goal: str, *, run_id: str, goalspec_mode: str = "rule", planning_horizon: int = 10, max_rounds: int = 100):
    """Compile and execute through the authoritative chain using TEST-ONLY mocks."""
    goalspec = compile_goal(user_goal, mode=goalspec_mode)
    planner = ToDAGEngine(planning_horizon=planning_horizon)
    taskgraph = planner.build(goalspec)
    plan = planner.execution_plan()
    required_skills = [
        skill
        for node in plan
        for skill in node.get("required_skills", (node.get("required_skill"),))
        if skill
    ]
    register_test_mock_resources(chain, required_skills)
    return chain.run_plan(
        goalspec=goalspec,
        taskgraph=taskgraph,
        plan=plan,
        run_id=run_id,
        max_rounds=max_rounds,
        auto_register_deepseek_resources=False,
        runtime_metadata={
            "goalspec_mode": goalspec_mode,
            "agent_mode": "TEST_ONLY_MOCK",
            "test_only": True,
            "competition_evidence_eligible": False,
        },
    )
