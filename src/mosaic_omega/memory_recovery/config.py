"""Environment-driven configuration for the memory module."""
from __future__ import annotations

import os
from dataclasses import dataclass


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw in (None, ""):
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"Environment variable {name} must be an integer, got {raw!r}") from exc
    if value < 0:
        raise ValueError(f"Environment variable {name} must be >= 0, got {value}")
    return value


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw in (None, ""):
        return default
    return raw.lower() in {"1", "true", "yes", "y", "on"}


@dataclass(frozen=True)
class MemoryConfig:
    redis_url: str
    object_store_dir: str
    default_ttl_seconds: int
    working_ttl_seconds: int
    recall_limit: int
    context_pack_max_chars: int
    context_pack_max_tokens: int
    context_pack_max_facts: int
    context_pack_max_experiences: int
    context_pack_max_procedures: int
    max_working_items: int
    snapshot_compress: bool
    snapshot_every_n_events: int
    vector_candidate_limit: int


def load_config() -> MemoryConfig:
    return MemoryConfig(
        redis_url=os.getenv("MEMORY_REDIS_URL", "redis://localhost:6379/0"),
        object_store_dir=os.getenv("MEMORY_OBJECT_STORE_DIR", "./memory_object_store"),
        default_ttl_seconds=_int_env("MEMORY_DEFAULT_TTL_SECONDS", 7 * 24 * 3600),
        working_ttl_seconds=_int_env("MEMORY_WORKING_TTL_SECONDS", 24 * 3600),
        recall_limit=max(1, _int_env("MEMORY_RECALL_LIMIT", 10)),
        context_pack_max_chars=max(256, _int_env("MEMORY_CONTEXT_PACK_MAX_CHARS", 8000)),
        context_pack_max_tokens=max(64, _int_env("MEMORY_CONTEXT_PACK_MAX_TOKENS", 2500)),
        context_pack_max_facts=max(1, _int_env("MEMORY_CONTEXT_PACK_MAX_FACTS", 12)),
        context_pack_max_experiences=max(1, _int_env("MEMORY_CONTEXT_PACK_MAX_EXPERIENCES", 8)),
        context_pack_max_procedures=max(1, _int_env("MEMORY_CONTEXT_PACK_MAX_PROCEDURES", 5)),
        max_working_items=max(1, _int_env("MEMORY_MAX_WORKING_ITEMS", 50)),
        snapshot_compress=_bool_env("MEMORY_SNAPSHOT_COMPRESS", True),
        snapshot_every_n_events=_int_env("MEMORY_SNAPSHOT_EVERY_N_EVENTS", 0),
        vector_candidate_limit=max(1, _int_env("MEMORY_VECTOR_CANDIDATE_LIMIT", 50)),
    )
