"""Connectivity metrics for the weak, undirected projection of a task graph."""

from __future__ import annotations

import math
from collections import defaultdict, deque
from collections.abc import Iterable


def active_nodes(edges: Iterable[tuple[str, str]]) -> set[str]:
    return {node for edge in edges for node in edge}


def weak_components(edges: Iterable[tuple[str, str]]) -> tuple[frozenset[str], ...]:
    pairs = tuple(edges)
    nodes = active_nodes(pairs)
    graph: dict[str, set[str]] = defaultdict(set)
    for source, target in pairs:
        graph[source].add(target)
        graph[target].add(source)
    components: list[frozenset[str]] = []
    unseen = set(nodes)
    while unseen:
        start = min(unseen)
        queue = deque([start])
        component = {start}
        unseen.remove(start)
        while queue:
            node = queue.popleft()
            for neighbor in graph[node]:
                if neighbor in unseen:
                    unseen.remove(neighbor)
                    component.add(neighbor)
                    queue.append(neighbor)
        components.append(frozenset(component))
    return tuple(sorted(components, key=lambda item: tuple(sorted(item))))


def algebraic_connectivity(edges: Iterable[tuple[str, str]]) -> float:
    """Return lambda2 of the symmetric Laplacian using a Jacobi eigensolver.

    The logical topology is directed, while algebraic connectivity is defined
    here on its weak, undirected projection. Directed task reachability remains
    a separate safety condition.
    """
    pairs = tuple(edges)
    nodes = sorted(active_nodes(pairs))
    size = len(nodes)
    if size < 2:
        return 0.0
    index = {node: position for position, node in enumerate(nodes)}
    adjacency = [[0.0 for _ in range(size)] for _ in range(size)]
    for source, target in pairs:
        left, right = index[source], index[target]
        adjacency[left][right] = 1.0
        adjacency[right][left] = 1.0
    matrix = [[0.0 for _ in range(size)] for _ in range(size)]
    for row in range(size):
        degree = sum(adjacency[row])
        matrix[row][row] = degree
        for column in range(size):
            if row != column and adjacency[row][column]:
                matrix[row][column] = -1.0

    tolerance = 1e-12
    for _ in range(max(20, size * size * 25)):
        p, q = 0, 1
        largest = 0.0
        for row in range(size):
            for column in range(row + 1, size):
                value = abs(matrix[row][column])
                if value > largest:
                    largest, p, q = value, row, column
        if largest < tolerance:
            break
        app, aqq, apq = matrix[p][p], matrix[q][q], matrix[p][q]
        angle = 0.5 * math.atan2(2.0 * apq, aqq - app)
        cosine, sine = math.cos(angle), math.sin(angle)
        for row in range(size):
            if row in (p, q):
                continue
            arp, arq = matrix[row][p], matrix[row][q]
            matrix[row][p] = matrix[p][row] = cosine * arp - sine * arq
            matrix[row][q] = matrix[q][row] = sine * arp + cosine * arq
        matrix[p][p] = cosine * cosine * app - 2 * sine * cosine * apq + sine * sine * aqq
        matrix[q][q] = sine * sine * app + 2 * sine * cosine * apq + cosine * cosine * aqq
        matrix[p][q] = matrix[q][p] = 0.0
    eigenvalues = sorted(max(0.0, matrix[row][row]) for row in range(size))
    return round(eigenvalues[1], 6)

