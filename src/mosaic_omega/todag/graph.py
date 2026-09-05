"""Deterministic TaskGraph validation, traversal, impact analysis and ordering."""

from __future__ import annotations

import heapq
from collections import deque
from collections.abc import Iterable, Mapping
from typing import Any

from .models import DAGNode


def outgoing_index(nodes: Mapping[str, DAGNode]) -> dict[str, list[str]]:
    outgoing = {task_id: [] for task_id in nodes}
    for node in nodes.values():
        for dependency in node.depends_on:
            if dependency in outgoing:
                outgoing[dependency].append(node.task_id)
    for values in outgoing.values():
        values.sort()
    return outgoing


def validate_references(nodes: Mapping[str, DAGNode]) -> None:
    node_ids = set(nodes)
    for task_id, node in nodes.items():
        node.validate()
        if task_id != node.task_id:
            raise ValueError(f"node key {task_id} does not match task_id {node.task_id}")
        if task_id in node.depends_on:
            raise ValueError(f"task {task_id} cannot depend on itself")
        missing = sorted(set(node.depends_on) - node_ids)
        if missing:
            raise ValueError(f"task {task_id} has missing dependencies: {missing}")
        if task_id in node.mutex_with:
            raise ValueError(f"task {task_id} cannot be mutex with itself")
        missing_mutex = sorted(set(node.mutex_with) - node_ids)
        if missing_mutex:
            raise ValueError(f"task {task_id} has missing mutex peers: {missing_mutex}")


def topological_sort(nodes: Mapping[str, DAGNode]) -> list[str]:
    if not nodes:
        raise ValueError("DAG must contain at least one node")
    validate_references(nodes)
    indegree = {task_id: len(node.depends_on) for task_id, node in nodes.items()}
    outgoing = outgoing_index(nodes)
    ready: list[tuple[int, str]] = [
        (-nodes[task_id].priority, task_id)
        for task_id, degree in indegree.items()
        if degree == 0
    ]
    heapq.heapify(ready)
    order: list[str] = []
    while ready:
        _, task_id = heapq.heappop(ready)
        order.append(task_id)
        for successor in outgoing[task_id]:
            indegree[successor] -= 1
            if indegree[successor] == 0:
                heapq.heappush(ready, (-nodes[successor].priority, successor))
    if len(order) != len(nodes):
        raise ValueError("task dependencies contain a cycle")
    return order


def validate_single_terminal(nodes: Mapping[str, DAGNode]) -> str:
    outgoing = outgoing_index(nodes)
    terminals = sorted(task_id for task_id, successors in outgoing.items() if not successors)
    if len(terminals) != 1:
        raise ValueError(f"DAG must have exactly one terminal node; found {terminals}")
    return terminals[0]


def descendants(nodes: Mapping[str, DAGNode], task_id: str) -> set[str]:
    if task_id not in nodes:
        raise KeyError(task_id)
    outgoing = outgoing_index(nodes)
    found: set[str] = set()
    queue = deque(outgoing[task_id])
    while queue:
        current = queue.popleft()
        if current in found:
            continue
        found.add(current)
        queue.extend(outgoing[current])
    return found


def ancestors(nodes: Mapping[str, DAGNode], task_id: str) -> set[str]:
    if task_id not in nodes:
        raise KeyError(task_id)
    found: set[str] = set()
    queue = deque(nodes[task_id].depends_on)
    while queue:
        current = queue.popleft()
        if current in found:
            continue
        found.add(current)
        queue.extend(nodes[current].depends_on)
    return found


def affected_closure(nodes: Mapping[str, DAGNode], changed_ids: Iterable[str]) -> set[str]:
    """Return changed nodes and all downstream decision/evidence dependents."""
    affected: set[str] = set()
    for task_id in changed_ids:
        if task_id not in nodes:
            continue
        affected.add(task_id)
        affected.update(descendants(nodes, task_id))
    return affected


def graph_levels(nodes: Mapping[str, DAGNode], order: list[str]) -> dict[str, int]:
    levels: dict[str, int] = {}
    for task_id in order:
        dependencies = nodes[task_id].depends_on
        levels[task_id] = 0 if not dependencies else max(levels[parent] for parent in dependencies) + 1
    return levels


def edges(nodes: Mapping[str, DAGNode]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for node in sorted(nodes.values(), key=lambda item: item.task_id):
        for dependency in node.depends_on:
            result.append(
                {
                    "source": dependency,
                    "target": node.task_id,
                    "type": node.dependency_types.get(dependency, "exec"),
                }
            )
    # Mutex is a symmetric relation rather than a precedence dependency, so it
    # is emitted for visualisation/scheduling but ignored by topological sort.
    seen_mutex: set[tuple[str, str]] = set()
    for node in sorted(nodes.values(), key=lambda item: item.task_id):
        for peer in node.mutex_with:
            key = tuple(sorted((node.task_id, peer)))
            if key in seen_mutex:
                continue
            seen_mutex.add(key)
            result.append({"source": key[0], "target": key[1], "type": "mutex"})
    return result


def ready_task_ids(nodes: Mapping[str, DAGNode]) -> list[str]:
    """Return executable nodes whose precedence dependencies are completed."""
    ready = [
        task_id
        for task_id, node in nodes.items()
        if node.status in {"pending", "ready"}
        and all(nodes[parent].status == "completed" for parent in node.depends_on)
    ]
    return sorted(ready, key=lambda task_id: (-nodes[task_id].priority, task_id))


def rolling_window(nodes: Mapping[str, DAGNode], horizon: int = 10) -> list[str]:
    """Return a bounded future window without dropping the full replayable graph.

    Nodes are selected in topological order.  A future node may enter the window
    if all unfinished predecessors are already in the same window.  This gives a
    deterministic rolling-planning view while the full DAG remains available for
    replay and impact analysis.
    """
    if horizon < 1:
        raise ValueError("horizon must be >= 1")
    order = topological_sort(nodes)
    selected: list[str] = []
    selected_set: set[str] = set()
    completed = {task_id for task_id, node in nodes.items() if node.status == "completed"}
    for task_id in order:
        node = nodes[task_id]
        if node.status == "completed":
            continue
        unfinished_parents = {parent for parent in node.depends_on if parent not in completed}
        if unfinished_parents.issubset(selected_set):
            selected.append(task_id)
            selected_set.add(task_id)
        if len(selected) >= horizon:
            break
    return selected


def _duration_weight(node: DAGNode) -> float:
    for key in ("duration_s", "time_s", "estimated_duration_s", "duration", "time"):
        value = node.estimated_cost.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
            return float(value)
    return 1.0


def critical_path(nodes: Mapping[str, DAGNode]) -> dict[str, Any]:
    """Return a deterministic longest path using estimated duration as weight."""
    order = topological_sort(nodes)
    best_cost: dict[str, float] = {}
    predecessor: dict[str, str | None] = {}
    for task_id in order:
        node = nodes[task_id]
        weight = _duration_weight(node)
        if not node.depends_on:
            best_cost[task_id] = weight
            predecessor[task_id] = None
            continue
        parent = max(node.depends_on, key=lambda item: (best_cost[item], item))
        best_cost[task_id] = best_cost[parent] + weight
        predecessor[task_id] = parent
    terminal = max(order, key=lambda item: (best_cost[item], item))
    path: list[str] = []
    current: str | None = terminal
    while current is not None:
        path.append(current)
        current = predecessor[current]
    path.reverse()
    return {"task_ids": path, "estimated_duration": best_cost[terminal]}
