"""Compressed snapshot creation, checksum verification and fallback restore."""
from __future__ import annotations

import gzip
import hashlib
import json
from typing import Any, Dict, Optional, Tuple
from uuid import uuid4

from ..storage import LocalObjectStore
from .config import MemoryConfig
from .models import MemoryRecord, Snapshot, utc_now
from .repository import MemoryRepository
from .working_memory import WorkingMemory


class SnapshotManager:
    def __init__(
        self,
        repository: MemoryRepository,
        object_store: LocalObjectStore,
        config: MemoryConfig,
        working_memory: Optional[WorkingMemory] = None,
    ):
        self.repository = repository
        self.object_store = object_store
        self.config = config
        self.working_memory = working_memory

    @staticmethod
    def _state_checksum(state: Dict[str, Any]) -> str:
        raw = json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def create_snapshot(self, run_id: str) -> Snapshot:
        records = [r.to_dict() for r in self.repository.query(run_id=run_id, limit=100000)]
        working = self.working_memory.export_run(run_id) if self.working_memory else {}
        state: Dict[str, Any] = {"records": records, "working": working}
        snapshot = Snapshot(
            snapshot_id=f"snap_{uuid4().hex}",
            run_id=run_id,
            created_at=utc_now(),
            state=state,
            compressed=self.config.snapshot_compress,
            checksum=self._state_checksum(state),
        )
        key = f"snapshots/{run_id}/{snapshot.created_at.replace(':', '-')}_{snapshot.snapshot_id}.json"
        if self.config.snapshot_compress:
            key += ".gz"
        # URI/object_key are metadata, not part of the checksum-protected state.
        payload = snapshot.to_dict()
        payload["object_key"] = key
        payload["uri"] = self.object_store.uri(key)
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if self.config.snapshot_compress:
            raw = gzip.compress(raw)
        self.object_store.put_bytes(key, raw)
        # Preserve compatibility with callers that expect key in snapshot.state.
        snapshot.state["object_key"] = key
        snapshot.state["uri"] = self.object_store.uri(key)
        return snapshot

    def _resolve_key(self, snapshot_id_or_key: str) -> str:
        if "/" in snapshot_id_or_key or snapshot_id_or_key.endswith((".json", ".json.gz")):
            return snapshot_id_or_key
        matches = [
            key for key in self.object_store.list_keys("snapshots")
            if snapshot_id_or_key in key
        ]
        if not matches:
            raise FileNotFoundError(f"snapshot not found: {snapshot_id_or_key}")
        return matches[0]

    def _load(self, key: str) -> Dict[str, Any]:
        data = self.object_store.get_bytes(key)
        if data is None:
            raise FileNotFoundError(f"snapshot not found: {key}")
        if key.endswith(".gz"):
            data = gzip.decompress(data)
        parsed = json.loads(data.decode("utf-8"))
        state = dict(parsed.get("state", {}))
        # Compatibility fields were added after checksum computation.
        state.pop("object_key", None)
        state.pop("uri", None)
        expected = parsed.get("checksum", "")
        if expected and expected != self._state_checksum(state):
            raise ValueError(f"snapshot checksum mismatch: {key}")
        parsed["state"] = state
        parsed["object_key"] = key
        return parsed

    def _fallback_key(self, bad_key: str) -> Optional[str]:
        parts = bad_key.split("/")
        if len(parts) < 3 or parts[0] != "snapshots":
            return None
        run_id = parts[1]
        candidates = [k for k in self.object_store.list_keys(f"snapshots/{run_id}") if k != bad_key]
        for key in candidates:
            try:
                self._load(key)
                return key
            except Exception:
                continue
        return None

    def restore_snapshot(
        self,
        snapshot_id_or_key: str,
        *,
        apply: bool = False,
        fallback_to_previous: bool = True,
    ) -> Dict[str, Any]:
        key = self._resolve_key(snapshot_id_or_key)
        used_fallback = False
        try:
            parsed = self._load(key)
        except Exception:
            if not fallback_to_previous:
                raise
            fallback = self._fallback_key(key)
            if fallback is None:
                raise
            parsed = self._load(fallback)
            used_fallback = True
            parsed["fallback_from"] = key

        if apply:
            for item in parsed.get("state", {}).get("records", []):
                self.repository.save(MemoryRecord.from_dict(item))
            if self.working_memory:
                self.working_memory.import_run(parsed.get("state", {}).get("working", {}))
        parsed["used_fallback"] = used_fallback
        return parsed

    def verify_consistency(self, snapshot_data: Dict[str, Any]) -> Tuple[bool, Dict[str, int]]:
        run_id = snapshot_data.get("run_id", "")
        expected_ids = {
            item.get("memory_id")
            for item in snapshot_data.get("state", {}).get("records", [])
            if item.get("memory_id")
        }
        actual_ids = {r.memory_id for r in self.repository.query(run_id=run_id, limit=100000)}
        ok = expected_ids.issubset(actual_ids)
        return ok, {"expected": len(expected_ids), "actual": len(actual_ids)}
