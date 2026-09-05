"""Safety checks and repairs for task-critical communication paths."""

from __future__ import annotations

from collections import defaultdict, deque

from .models import EdgeCandidate
from .graph_metrics import weak_components


class ConnectivityGuard:
    """Retains mandatory dependency edges and adds feasible high-risk backups."""

    @staticmethod
    def protect(candidates: list[EdgeCandidate]) -> list[EdgeCandidate]:
        by_key: dict[tuple[str, str], EdgeCandidate] = {}
        for candidate in candidates:
            existing = by_key.get(candidate.key)
            if existing is None:
                by_key[candidate.key] = candidate
                continue
            by_key[candidate.key] = EdgeCandidate(
                source=candidate.source,
                target=candidate.target,
                task_ids=existing.task_ids | candidate.task_ids,
                score=max(existing.score, candidate.score),
                required=existing.required or candidate.required,
                high_risk=existing.high_risk or candidate.high_risk,
                reason=existing.reason if existing.required else candidate.reason,
            )
        selected = dict(by_key)
        # Add a backup only when an alternative directed path already exists
        # among task-relevant edges.
        for edge in tuple(by_key.values()):
            if edge.required and edge.high_risk:
                for backup in ConnectivityGuard._alternate_path(edge, tuple(by_key.values())):
                    # A backup is a safety edge, not a best-effort Top-K
                    # suggestion. It may exceed the normal neighbour limit.
                    selected[backup.key] = EdgeCandidate(
                        source=backup.source,
                        target=backup.target,
                        task_ids=backup.task_ids | edge.task_ids,
                        score=backup.score,
                        required=True,
                        high_risk=True,
                        reason="high_risk_backup",
                    )
        return list(selected.values())

    @staticmethod
    def repair(selected: list[EdgeCandidate], candidates: list[EdgeCandidate]) -> list[EdgeCandidate]:
        """Actively add the best known cross-component bridge candidates.

        This never invents an Agent that cannot carry the task. Candidate
        generation stays in the topology manager, where capabilities and
        runtime signals are available.
        """
        repaired = {candidate.key: candidate for candidate in selected}
        all_nodes = {
            node
            for candidate in (*selected, *(item for item in candidates if item.required))
            for node in candidate.key
        }
        while True:
            components = list(weak_components(repaired))
            covered = {node for component in components for node in component}
            components.extend(frozenset({node}) for node in sorted(all_nodes - covered))
            if len(components) <= 1:
                break
            component_index = {
                node: index for index, component in enumerate(components) for node in component
            }
            bridges = [
                candidate for candidate in candidates
                if candidate.key not in repaired
                and component_index.get(candidate.source) != component_index.get(candidate.target)
            ]
            if not bridges:
                break
            bridge = max(bridges, key=lambda item: (item.score, item.source, item.target))
            repaired[bridge.key] = EdgeCandidate(
                source=bridge.source,
                target=bridge.target,
                task_ids=bridge.task_ids,
                score=bridge.score,
                required=True,
                high_risk=bridge.high_risk,
                reason="active_bridge",
            )
        return list(repaired.values())

    @staticmethod
    def _alternate_path(required: EdgeCandidate, candidates: tuple[EdgeCandidate, ...]) -> tuple[EdgeCandidate, ...]:
        graph: dict[str, list[EdgeCandidate]] = defaultdict(list)
        for candidate in candidates:
            if candidate.key != required.key:
                graph[candidate.source].append(candidate)
        queue: deque[tuple[str, tuple[EdgeCandidate, ...]]] = deque([(required.source, ())])
        seen = {required.source}
        while queue:
            node, path = queue.popleft()
            if node == required.target and path:
                return path
            for edge in graph[node]:
                if edge.target not in seen:
                    seen.add(edge.target)
                    queue.append((edge.target, path + (edge,)))
        return ()
