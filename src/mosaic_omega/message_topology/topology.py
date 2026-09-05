"""In-memory directed graph used by the dynamic topology manager."""

from __future__ import annotations

import time

from .config import TopologyConfig
from .models import EdgeCandidate, EdgeState, TopologySnapshot
from .graph_metrics import algebraic_connectivity, weak_components


class TaskTopology:
    """Maintains only active task communication edges, not physical MQTT links."""

    def __init__(self, config: TopologyConfig) -> None:
        self._config = config
        self._nodes: set[str] = set()
        self._edges: dict[tuple[str, str], EdgeState] = {}
        self._version = 0
        self._effective_from = 0.0

    def set_nodes(self, nodes: set[str]) -> bool:
        if nodes == self._nodes:
            return False
        self._nodes = set(nodes)
        self._edges = {
            key: edge for key, edge in self._edges.items()
            if edge.source in self._nodes and edge.target in self._nodes
        }
        self._version += 1
        return True

    def edge(self, key: tuple[str, str]) -> EdgeState | None:
        return self._edges.get(key)

    def edges(self) -> tuple[EdgeState, ...]:
        return tuple(sorted(self._edges.values(), key=lambda edge: edge.key))

    def neighbors(self, agent_id: str) -> tuple[str, ...]:
        return tuple(sorted(edge.target for edge in self._edges.values() if edge.source == agent_id))

    def add_edge(self, candidate: EdgeCandidate, now: float) -> EdgeState:
        if candidate.source not in self._nodes or candidate.target not in self._nodes:
            raise ValueError("both edge endpoints must be online nodes")
        previous = self._edges.get(candidate.key)
        effective_from = previous.effective_from if previous is not None else now
        edge = EdgeState(
            source=candidate.source,
            target=candidate.target,
            task_ids=candidate.task_ids,
            score=candidate.score,
            required=candidate.required,
            high_risk=candidate.high_risk,
            reason=candidate.reason,
            effective_from=effective_from,
            min_hold_until=now + self._config.min_hold_time_s,
        )
        if previous != edge:
            self._edges[candidate.key] = edge
            self._version += 1
            self._effective_from = now
        return edge

    def remove_edge(self, key: tuple[str, str]) -> EdgeState | None:
        removed = self._edges.pop(key, None)
        if removed is not None:
            self._version += 1
        return removed

    def snapshot(self) -> TopologySnapshot:
        edge_keys = tuple(edge.key for edge in self._edges.values())
        components = weak_components(edge_keys)
        lambda2 = algebraic_connectivity(edge_keys)
        connected = len(components) <= 1
        return TopologySnapshot(
            version=self._version,
            nodes=tuple(sorted(self._nodes)),
            edges=self.edges(),
            top_k=self._config.top_k,
            effective_from=self._effective_from,
            min_hold_time_s=self._config.min_hold_time_s,
            connected=connected,
            component_count=len(components),
            lambda2=lambda2,
            spectral_target=self._config.min_algebraic_connectivity,
            spectral_target_met=connected and lambda2 >= self._config.min_algebraic_connectivity,
            generated_at=time.time(),
        )
