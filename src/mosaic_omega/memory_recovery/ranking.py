"""Deterministic recall ranking and safety gates."""
from __future__ import annotations

import math
import re
from datetime import datetime
from typing import Iterable, List, Optional, Sequence, Tuple

from .models import MemoryRecord, VerificationStatus

_TOKEN_RE = re.compile(r"[\w\u4e00-\u9fff]+", re.UNICODE)


def _tokens(text: str) -> set[str]:
    return {t.lower() for t in _TOKEN_RE.findall(text or "")}


def lexical_similarity(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def recency_score(created_at: str) -> float:
    try:
        dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        age_seconds = max(0.0, (datetime.now(dt.tzinfo) - dt).total_seconds())
        return math.exp(-age_seconds / 86400.0)
    except Exception:
        return 0.3


def scope_allowed(record: MemoryRecord, allowed_scopes: Optional[Iterable[str]]) -> bool:
    scopes = set(allowed_scopes or [])
    if not scopes:
        return True
    return bool(set(record.access_scope).intersection(scopes))


def is_forced(record: MemoryRecord) -> bool:
    return record.is_core_constraint


def score_record(
    record: MemoryRecord,
    *,
    query: str = "",
    taskgraph_nodes: Optional[Iterable[str]] = None,
    allowed_scopes: Optional[Iterable[str]] = None,
    evidence_ids: Optional[Iterable[str]] = None,
    seen_memory_ids: Optional[Iterable[str]] = None,
) -> float:
    # Permission is a hard gate and must precede the forced-memory rule.
    if not scope_allowed(record, allowed_scopes):
        return -1.0
    if record.verification_status in {VerificationStatus.REJECTED, VerificationStatus.STALE}:
        return -1.0
    if is_forced(record):
        return 100.0

    taskgraph_set = set(taskgraph_nodes or [])
    evidence_set = set(evidence_ids or [])
    seen_set = set(seen_memory_ids or [])
    taskgraph_relevance = 1.0 if (not taskgraph_set or record.node_id in taskgraph_set or record.task_id in taskgraph_set) else 0.2
    evidence_relevance = 1.0 if evidence_set.intersection(record.evidence_refs) else 0.25
    novelty = 0.25 if record.memory_id in seen_set else 1.0
    verified_bonus = 0.12 if record.verification_status == VerificationStatus.VERIFIED else 0.0
    semantic = lexical_similarity(query, record.summary + " " + record.content + " " + " ".join(record.tags)) if query else 0.25

    return max(0.0, (
        0.26 * taskgraph_relevance
        + 0.16 * evidence_relevance
        + 0.18 * record.importance
        + 0.10 * record.confidence
        + 0.10 * recency_score(record.created_at)
        + 0.06 * novelty
        + 0.14 * semantic
        + verified_bonus
    ))


def rank_records(records: Sequence[MemoryRecord], **kwargs) -> List[MemoryRecord]:
    scored: List[Tuple[float, MemoryRecord]] = [(score_record(r, **kwargs), r) for r in records]
    scored = [(score, r) for score, r in scored if score >= 0]
    scored.sort(key=lambda pair: (pair[0], pair[1].created_at), reverse=True)
    return [r for _, r in scored]
