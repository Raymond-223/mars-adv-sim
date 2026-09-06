"""Build a bounded fixed-format ContextPack."""
from __future__ import annotations

import json
from typing import Iterable, List, Optional

from .config import MemoryConfig
from .models import ContextPack, MemoryRecord, MemoryType
from .retriever import Retriever


def estimate_tokens(text: str) -> int:
    """Conservative tokenizer-free estimate suitable for budget enforcement."""
    if not text:
        return 0
    # Chinese is often close to one token per character; Latin prose is closer
    # to 3-4 chars/token.  len//2 is intentionally conservative for mixed text.
    return max(1, (len(text) + 1) // 2)


def _serialized(pack: ContextPack) -> str:
    return json.dumps(pack.to_dict(), ensure_ascii=False, separators=(",", ":"))


class ContextBuilder:
    def __init__(self, retriever: Retriever, config: MemoryConfig):
        self.retriever = retriever
        self.config = config

    def build(
        self,
        *,
        run_id: str,
        node_id: str,
        task_id: Optional[str] = None,
        taskgraph_nodes: Optional[Iterable[str]] = None,
        query: str = "",
        evidence_ids: Optional[Iterable[str]] = None,
        allowed_scopes: Optional[Iterable[str]] = None,
    ) -> ContextPack:
        recalled = self.retriever.retrieve(
            run_id=run_id,
            node_id=node_id,
            task_id=task_id,
            taskgraph_nodes=taskgraph_nodes,
            query=query,
            evidence_ids=evidence_ids,
            allowed_scopes=allowed_scopes,
            limit=self.config.recall_limit,
        )
        working = recalled["working"]
        records: List[MemoryRecord] = recalled["records"]  # type: ignore[assignment]

        pack = ContextPack(
            run_id=run_id,
            node_id=node_id,
            goal=working.get("current_goal", "") if isinstance(working, dict) else "",
            hard_constraints=list(working.get("active_constraints", [])) if isinstance(working, dict) else [],
            previous_results=list(working.get("recent_results", [])) if isinstance(working, dict) else [],
            evidence_refs=list(working.get("required_evidence", [])) if isinstance(working, dict) else [],
        )
        for record in records:
            self._add_record(pack, record)

        before_dedupe = self._bucket_sizes(pack)
        self._dedupe_pack(pack)
        after_dedupe = self._bucket_sizes(pack)
        before_limits = dict(after_dedupe)
        self._apply_item_limits(pack)
        after_limits = self._bucket_sizes(pack)
        tokens_before_budget = estimate_tokens(_serialized(pack))
        self._truncate_optional_content(pack)
        pack.token_estimate = estimate_tokens(_serialized(pack))

        trace = dict(recalled.get("trace") or {})
        # Compression is the last stage of the pipeline and is reported with the
        # same granularity as retrieval, so a reviewer can see exactly which
        # buckets shrank and why.
        trace["compression"] = {
            "deduplicated": {
                key: before_dedupe[key] - after_dedupe[key]
                for key in before_dedupe if before_dedupe[key] != after_dedupe[key]
            },
            "item_limit_dropped": {
                key: before_limits[key] - after_limits[key]
                for key in before_limits if before_limits[key] != after_limits[key]
            },
            "budget_dropped": {
                key: after_limits[key] - self._bucket_sizes(pack)[key]
                for key in after_limits if after_limits[key] != self._bucket_sizes(pack)[key]
            },
            "token_estimate_before_budget": tokens_before_budget,
            "token_estimate_final": pack.token_estimate,
            "truncated": bool(pack.truncated),
            "budget": {
                "max_tokens": self.config.context_pack_max_tokens,
                "max_chars": self.config.context_pack_max_chars,
            },
        }
        trace["final_pack_sizes"] = self._bucket_sizes(pack)
        pack.selection_trace = trace
        return pack

    @staticmethod
    def _bucket_sizes(pack: ContextPack) -> dict:
        return {
            "hard_constraints": len(pack.hard_constraints),
            "prohibitions": len(pack.prohibitions),
            "relevant_facts": len(pack.relevant_facts),
            "previous_results": len(pack.previous_results),
            "evidence_refs": len(pack.evidence_refs),
            "relevant_experiences": len(pack.relevant_experiences),
            "procedures": len(pack.procedures),
            "memory_ids": len(pack.memory_ids),
        }

    def build_with_awakening(
        self,
        *,
        run_id: str,
        node_id: str,
        trigger_event: str,
        task_id: Optional[str] = None,
        taskgraph_nodes: Optional[Iterable[str]] = None,
        query: str = "",
        evidence_ids: Optional[Iterable[str]] = None,
        allowed_scopes: Optional[Iterable[str]] = None,
    ) -> ContextPack:
        """Trigger-Awakening Mechanism: Activated on failures, goal changes, or drift.

        Ensures critical goal constraints, root-cause evidence, and repair procedures
        are forcefully retrieved and un-truncated regardless of token limits.
        """
        # Expand query with trigger semantics
        awakened_query = f"{trigger_event} {query}".strip()
        pack = self.build(
            run_id=run_id,
            node_id=node_id,
            task_id=task_id,
            taskgraph_nodes=taskgraph_nodes,
            query=awakened_query,
            evidence_ids=evidence_ids,
            allowed_scopes=allowed_scopes,
        )

        # Inject awakening metadata tag
        pack.prohibitions.append(f"TRIGGER_AWAKENING_ACTIVE: event='{trigger_event}'")
        self._dedupe_pack(pack)
        pack.token_estimate = estimate_tokens(_serialized(pack))
        return pack


    @staticmethod
    def _append_unique(bucket: List[str], value: str) -> None:
        value = (value or "").strip()
        if value and value not in bucket:
            bucket.append(value)

    def _add_record(self, pack: ContextPack, record: MemoryRecord) -> None:
        if record.memory_id not in pack.memory_ids:
            pack.memory_ids.append(record.memory_id)
        for evidence_ref in record.evidence_refs:
            self._append_unique(pack.evidence_refs, evidence_ref)

        if "goal" in record.tags:
            # Stable semantic goal fills an empty working goal; a live working
            # goal may represent a later valid goal update and therefore wins.
            if not pack.goal:
                pack.goal = record.content
            self._append_unique(pack.relevant_facts, f"{record.summary}: {record.content}")
        elif "hard_constraint" in record.tags:
            self._append_unique(pack.hard_constraints, record.content)
        elif "prohibition" in record.tags:
            self._append_unique(pack.prohibitions, record.content)
        elif record.memory_type == MemoryType.SEMANTIC:
            self._append_unique(pack.relevant_facts, f"{record.summary}: {record.content}")
        elif record.memory_type == MemoryType.EPISODIC:
            self._append_unique(pack.relevant_experiences, record.summary)
        elif record.memory_type == MemoryType.PROCEDURAL:
            self._append_unique(pack.procedures, f"{record.summary}\n{record.content}")

    def _dedupe_pack(self, pack: ContextPack) -> None:
        for attr in [
            "hard_constraints", "prohibitions", "relevant_facts",
            "previous_results", "evidence_refs", "relevant_experiences",
            "procedures", "memory_ids",
        ]:
            setattr(pack, attr, list(dict.fromkeys(getattr(pack, attr))))

    def _apply_item_limits(self, pack: ContextPack) -> None:
        # Core goal/constraints/prohibitions are never item-truncated.
        before = (
            len(pack.relevant_facts), len(pack.relevant_experiences), len(pack.procedures),
            len(pack.previous_results),
        )
        pack.relevant_facts = pack.relevant_facts[: self.config.context_pack_max_facts]
        pack.relevant_experiences = pack.relevant_experiences[: self.config.context_pack_max_experiences]
        pack.procedures = pack.procedures[: self.config.context_pack_max_procedures]
        pack.previous_results = pack.previous_results[-self.config.max_working_items :]
        after = (
            len(pack.relevant_facts), len(pack.relevant_experiences), len(pack.procedures),
            len(pack.previous_results),
        )
        if before != after:
            pack.truncated = True

    def _over_budget(self, pack: ContextPack) -> bool:
        text = _serialized(pack)
        return (
            len(text) > self.config.context_pack_max_chars
            or estimate_tokens(text) > self.config.context_pack_max_tokens
        )

    def _truncate_optional_content(self, pack: ContextPack) -> None:
        if not self._over_budget(pack):
            return
        pack.truncated = True
        # History and optional knowledge are sacrificed before constraints.
        trim_order = [
            pack.relevant_experiences,
            pack.previous_results,
            pack.procedures,
            pack.relevant_facts,
            pack.memory_ids,
            pack.evidence_refs,
        ]
        for bucket in trim_order:
            while bucket and self._over_budget(pack):
                bucket.pop()
        # If goal + hard constraints + prohibitions alone exceed the configured
        # budget, they are intentionally retained rather than silently lost.
