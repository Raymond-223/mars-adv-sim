"""Short-term Working Memory for the current run/node execution window."""
from __future__ import annotations

from collections import deque
from typing import Any, Deque, Dict, List, Optional

from .config import MemoryConfig


class WorkingMemory:
    """Maintain the current run/node state under ``working:{run_id}:{node_id}``."""

    def __init__(self, config: MemoryConfig, store=None):
        self.config = config
        self.store = store
        self._local: Dict[str, Dict[str, Any]] = {}
        self._recent: Dict[str, Deque[Dict[str, Any]]] = {}

    def key(self, run_id: str, node_id: str) -> str:
        return f"working:{run_id}:{node_id}"

    def set_state(
        self,
        run_id: str,
        node_id: str,
        *,
        current_goal: str = "",
        active_constraints: Optional[List[str]] = None,
        recent_results: Optional[List[str]] = None,
        required_evidence: Optional[List[str]] = None,
        current_agent: str = "",
        ttl_seconds: Optional[int] = None,
    ) -> Dict[str, Any]:
        state = {
            "run_id": run_id,
            "node_id": node_id,
            "current_goal": current_goal,
            "active_constraints": list(active_constraints or []),
            "recent_results": list(recent_results or [])[-self.config.max_working_items :],
            "required_evidence": list(required_evidence or []),
            "current_agent": current_agent,
        }
        key = self.key(run_id, node_id)
        self._local[key] = state
        self._recent.setdefault(key, deque(maxlen=self.config.max_working_items))
        if self.store:
            self.store.set_json(key, state, ttl_seconds or self.config.working_ttl_seconds)
        return dict(state)

    def get_state(self, run_id: str, node_id: str) -> Dict[str, Any]:
        key = self.key(run_id, node_id)
        if self.store:
            stored = self.store.get_json(key)
            if stored is not None:
                self._local[key] = stored
                return stored
        return dict(self._local.get(key, {
            "run_id": run_id,
            "node_id": node_id,
            "current_goal": "",
            "active_constraints": [],
            "recent_results": [],
            "required_evidence": [],
            "current_agent": "",
        }))

    def add_recent_result(self, run_id: str, node_id: str, result: str) -> None:
        state = self.get_state(run_id, node_id)
        results = list(state.get("recent_results", []))
        results.append(result)
        self.set_state(
            run_id,
            node_id,
            current_goal=state.get("current_goal", ""),
            active_constraints=state.get("active_constraints", []),
            recent_results=results[-self.config.max_working_items :],
            required_evidence=state.get("required_evidence", []),
            current_agent=state.get("current_agent", ""),
        )

    def append_message(self, run_id: str, node_id: str, message: Dict[str, Any]) -> None:
        key = self.key(run_id, node_id)
        self._recent.setdefault(key, deque(maxlen=self.config.max_working_items)).append(dict(message))

    def recent_messages(self, run_id: str, node_id: str, limit: int = 10) -> List[Dict[str, Any]]:
        return list(self._recent.get(self.key(run_id, node_id), []))[-limit:]

    def export_run(self, run_id: str) -> Dict[str, Dict[str, Any]]:
        """Return all known Working Memory states for a run for snapshotting."""
        result: Dict[str, Dict[str, Any]] = {}
        prefix = f"working:{run_id}:"
        if self.store:
            for key in self.store.scan_keys(prefix + "*"):
                value = self.store.get_json(key)
                if value is not None:
                    result[key] = value
        for key, value in self._local.items():
            if key.startswith(prefix):
                result[key] = dict(value)
        return result

    def import_run(self, states: Dict[str, Dict[str, Any]]) -> None:
        for key, state in states.items():
            if not key.startswith("working:"):
                continue
            self._local[key] = dict(state)
            if self.store:
                self.store.set_json(key, state, self.config.working_ttl_seconds)

    def clear(self, run_id: str, node_id: str) -> None:
        key = self.key(run_id, node_id)
        self._local.pop(key, None)
        self._recent.pop(key, None)
        if self.store:
            self.store.delete(key)
