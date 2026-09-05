"""Incremental reconstruction of the task communication graph."""

from __future__ import annotations

import time
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence

from .config import TopologyConfig
from .connectivity_guard import ConnectivityGuard
from .edge_scoring import EdgeScorer, EdgeSignals
from .models import EdgeCandidate, EdgeState, RebuildResult
from .topology import TaskTopology


class TopologyManager:
    """Builds only task-relevant edges and changes only their affected region.

    ``task_dependencies`` maps a child task to its direct parent task IDs;
    ``assignments`` maps task IDs to their current assignee.  A parent assignee
    communicates with its child assignee once both are known.
    """

    def __init__(self, config: TopologyConfig | None = None) -> None:
        self.config = config or TopologyConfig()
        self._graph = TaskTopology(self.config)
        self._scorer = EdgeScorer()
        self.last_rebuild: RebuildResult | None = None

    def get_snapshot(self):
        return self._graph.snapshot()

    def rebuild(
        self,
        *,
        online_agents: Iterable[str],
        task_dependencies: Mapping[str, Sequence[str]],
        assignments: Mapping[str, str | None],
        task_priorities: Mapping[str, int] | None = None,
        agent_reliability: Mapping[str, float] | None = None,
        agent_latency_scores: Mapping[str, float] | None = None,
        task_information_values: Mapping[str, float] | None = None,
        standby_assignments: Mapping[str, Sequence[str]] | None = None,
        extra_candidates: Iterable[EdgeCandidate] = (),
        changed_agents: Iterable[str] = (),
        changed_task_ids: Iterable[str] = (),
        now: float | None = None,
    ) -> RebuildResult:
        timestamp = time.monotonic() if now is None else now
        online = set(online_agents)
        priorities = task_priorities or {}
        reliability = agent_reliability or {}
        latency = agent_latency_scores or {}
        information = task_information_values or {}
        candidates = self._dependency_candidates(
            task_dependencies, assignments, priorities, reliability, latency, information
        )
        candidates.extend(self._standby_candidates(
            task_dependencies,
            assignments,
            priorities,
            standby_assignments or {},
            reliability,
            latency,
            information,
        ))
        candidates.extend(
            self._score_runtime_candidate(candidate, reliability, latency)
            for candidate in extra_candidates
            if candidate.source in online and candidate.target in online
        )
        candidates = [
            candidate for candidate in candidates
            if candidate.source in online and candidate.target in online
        ]
        candidates = ConnectivityGuard.protect(candidates)
        initially_selected = list(self._select_edges(candidates).values())
        desired = {
            candidate.key: candidate
            for candidate in ConnectivityGuard.repair(initially_selected, candidates)
        }

        previous_edges = {edge.key: edge for edge in self._graph.edges()}
        explicit_agents = set(changed_agents)
        explicit_tasks = set(changed_task_ids)
        # With no active business edges there is no existing task region to
        # preserve, so the first task graph can be built as one complete pass.
        initial = not previous_edges
        affected_tasks, affected_agents = self._scope(
            candidates=candidates,
            previous_edges=previous_edges.values(),
            changed_agents=explicit_agents,
            changed_task_ids=explicit_tasks,
            initial=initial,
        )
        offline_agents = {edge_agent for edge_agent in explicit_agents if edge_agent not in online}
        affected_agents |= offline_agents
        removed: list[EdgeState] = []
        # An offline endpoint is an immediate safety exception to min-hold.
        # Remove it before changing the node set so the rebuild result reports
        # the exact local edge deletion rather than losing it silently.
        for edge in tuple(self._graph.edges()):
            if edge.source not in online or edge.target not in online:
                removed_edge = self._graph.remove_edge(edge.key)
                if removed_edge is not None:
                    removed.append(removed_edge)
        self._graph.set_nodes(online)

        keys_to_consider = {
            key for key, edge in previous_edges.items()
            if edge.source in affected_agents or edge.target in affected_agents or edge.task_ids & affected_tasks
        }
        keys_to_consider |= {
            key for key, candidate in desired.items()
            if candidate.source in affected_agents or candidate.target in affected_agents or candidate.task_ids & affected_tasks
        }
        if initial:
            keys_to_consider |= set(desired)

        added: list[EdgeState] = []
        retained: list[EdgeState] = []
        for key in sorted(keys_to_consider):
            old = self._graph.edge(key)
            wanted = desired.get(key)
            if wanted is not None:
                new = self._graph.add_edge(wanted, timestamp)
                if old is None:
                    added.append(new)
                else:
                    retained.append(new)
                continue
            if old is None:
                continue
            endpoints_online = old.source in online and old.target in online
            if endpoints_online and timestamp < old.min_hold_until:
                retained.append(old)
                continue
            removed_edge = self._graph.remove_edge(key)
            if removed_edge is not None:
                removed.append(removed_edge)

        result = RebuildResult(
            snapshot=self._graph.snapshot(),
            added=tuple(added),
            removed=tuple(removed),
            retained=tuple(retained),
            affected_agents=frozenset(affected_agents),
            affected_task_ids=frozenset(affected_tasks),
            full_rebuild=initial,
        )
        self.last_rebuild = result
        return result

    def _dependency_candidates(
        self,
        dependencies: Mapping[str, Sequence[str]],
        assignments: Mapping[str, str | None],
        priorities: Mapping[str, int],
        reliability: Mapping[str, float],
        latency: Mapping[str, float],
        information: Mapping[str, float],
    ) -> list[EdgeCandidate]:
        candidates: list[EdgeCandidate] = []
        for child_task, parents in dependencies.items():
            target = assignments.get(child_task)
            if target is None:
                continue
            for parent_task in parents:
                source = assignments.get(parent_task)
                if source is None or source == target:
                    continue
                priority = priorities.get(child_task, 5)
                candidate = self._scorer.candidate(
                    source,
                    target,
                    task_id=child_task,
                    signals=EdgeSignals(
                        dependency_strength=1.0,
                        information_value=information.get(child_task, priority / 10),
                        reliability=reliability.get(target, 0.95),
                        latency_score=latency.get(target, 1.0),
                    ),
                    required=True,
                    high_risk=priority >= self.config.high_risk_priority,
                    reason="task_dependency",
                )
                candidates.append(EdgeCandidate(
                    source=candidate.source,
                    target=candidate.target,
                    task_ids=frozenset({parent_task, child_task}),
                    score=candidate.score,
                    required=candidate.required,
                    high_risk=candidate.high_risk,
                    reason=candidate.reason,
                ))
        return candidates

    def _standby_candidates(
        self,
        dependencies: Mapping[str, Sequence[str]],
        assignments: Mapping[str, str | None],
        priorities: Mapping[str, int],
        standby_assignments: Mapping[str, Sequence[str]],
        reliability: Mapping[str, float],
        latency: Mapping[str, float],
        information: Mapping[str, float],
    ) -> list[EdgeCandidate]:
        candidates: list[EdgeCandidate] = []
        for child_task, standby_agents in standby_assignments.items():
            if priorities.get(child_task, 5) < self.config.high_risk_priority:
                continue
            primary = assignments.get(child_task)
            for parent_task in dependencies.get(child_task, ()):
                source = assignments.get(parent_task)
                if source is None:
                    continue
                for target in standby_agents:
                    if target in {source, primary}:
                        continue
                    scored = self._scorer.candidate(
                        source,
                        target,
                        task_id=child_task,
                        signals=EdgeSignals(
                            dependency_strength=0.85,
                            information_value=information.get(child_task, priorities.get(child_task, 5) / 10),
                            reliability=reliability.get(target, 0.95),
                            latency_score=latency.get(target, 1.0),
                        ),
                        required=True,
                        high_risk=True,
                        reason="high_risk_standby",
                    )
                    candidates.append(EdgeCandidate(
                        source=scored.source,
                        target=scored.target,
                        task_ids=frozenset({parent_task, child_task}),
                        score=scored.score,
                        required=True,
                        high_risk=True,
                        reason=scored.reason,
                    ))
        return candidates

    def _score_runtime_candidate(
        self,
        candidate: EdgeCandidate,
        reliability: Mapping[str, float],
        latency: Mapping[str, float],
    ) -> EdgeCandidate:
        task_id = min(candidate.task_ids) if candidate.task_ids else "runtime"
        scored = self._scorer.candidate(
            candidate.source,
            candidate.target,
            task_id=task_id,
            signals=EdgeSignals(
                dependency_strength=0.25 if candidate.reason == "context_demand" else 0.0,
                information_value=candidate.score,
                reliability=reliability.get(candidate.target, 0.95),
                latency_score=latency.get(candidate.target, 1.0),
            ),
            required=candidate.required,
            high_risk=candidate.high_risk,
            reason=candidate.reason,
        )
        return EdgeCandidate(
            source=scored.source,
            target=scored.target,
            task_ids=candidate.task_ids,
            score=scored.score,
            required=scored.required,
            high_risk=scored.high_risk,
            reason=scored.reason,
        )

    def _select_edges(self, candidates: list[EdgeCandidate]) -> dict[tuple[str, str], EdgeCandidate]:
        required = [candidate for candidate in candidates if candidate.required]
        optional_by_source: dict[str, list[EdgeCandidate]] = defaultdict(list)
        for candidate in candidates:
            if not candidate.required and candidate.score >= self.config.edge_score_threshold:
                optional_by_source[candidate.source].append(candidate)
        selected = {candidate.key: candidate for candidate in required}
        for group in optional_by_source.values():
            for candidate in sorted(group, key=lambda item: (-item.score, item.target))[:self.config.top_k]:
                existing = selected.get(candidate.key)
                if existing is None:
                    selected[candidate.key] = candidate
        return selected

    @staticmethod
    def _scope(
        *,
        candidates: list[EdgeCandidate],
        previous_edges: Iterable[EdgeState],
        changed_agents: set[str],
        changed_task_ids: set[str],
        initial: bool,
    ) -> tuple[set[str], set[str]]:
        if initial:
            agents = {agent for candidate in candidates for agent in candidate.key}
            tasks = {task_id for candidate in candidates for task_id in candidate.task_ids}
            return tasks, agents
        tasks = set(changed_task_ids)
        agents = set(changed_agents)
        all_edges = list(previous_edges) + list(candidates)
        # Deliberately expand exactly one hop. A transitive closure here would
        # turn one failed agent into a whole-graph rebuild on a long task DAG.
        for edge in all_edges:
            if bool({edge.source, edge.target} & changed_agents) or bool(edge.task_ids & changed_task_ids):
                agents.update((edge.source, edge.target))
                tasks.update(edge.task_ids)
        return tasks, agents
