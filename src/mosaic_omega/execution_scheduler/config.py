"""Centralized configuration for the execution scheduler.

All tunable values are read from environment variables once through ``get_settings``;
modules do not read ``os.environ`` independently.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Mapping


def _number(env: Mapping[str, str], name: str, default: float) -> float:
    try:
        return float(env.get(name, default))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be numeric") from exc


def _integer(env: Mapping[str, str], name: str, default: int) -> int:
    try:
        return int(env.get(name, default))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _boolean(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = str(env.get(name, "true" if default else "false")).strip().casefold()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be boolean")


@dataclass(frozen=True)
class Settings:
    database_url: str
    workspace: Path
    tool_timeout_s: float
    scheduler_policy: str
    resource_refresh_s: float
    outbox_batch_size: int
    allowed_commands: frozenset[str]
    weight_latency: float
    weight_token: float
    weight_energy: float
    weight_failure: float
    weight_migration: float
    posterior_decay_per_day: float
    schema_version: str
    snapshot_interval: int
    max_task_retries: int
    scheduler_allow_fallback: bool
    scheduler_solver_timeout_s: float
    agent_pool_min_instances: int
    agent_pool_max_instances: int
    provider_max_concurrency: int

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        source = env or os.environ
        workspace = Path(source.get("EXECUTION_WORKSPACE", ".")).resolve()
        policy = source.get("SCHEDULER_POLICY", "ortools").strip().lower()
        if policy not in {"ortools", "greedy", "round_robin"}:
            raise ValueError("SCHEDULER_POLICY must be ortools, greedy, or round_robin")
        timeout = _number(source, "TOOL_TIMEOUT_S", 30.0)
        refresh = _number(source, "RESOURCE_REFRESH_S", 5.0)
        snapshot_interval = _integer(source, "EVENT_SNAPSHOT_INTERVAL", 100)
        max_task_retries = _integer(source, "MAX_TASK_RETRIES", 1)
        if timeout <= 0:
            raise ValueError("TOOL_TIMEOUT_S must be > 0")
        if refresh <= 0:
            raise ValueError("RESOURCE_REFRESH_S must be > 0")
        if snapshot_interval < 0:
            raise ValueError("EVENT_SNAPSHOT_INTERVAL must be >= 0")
        if max_task_retries < 0:
            raise ValueError("MAX_TASK_RETRIES must be >= 0")
        solver_timeout = _number(source, "SCHEDULER_SOLVER_TIMEOUT_S", 5.0)
        if solver_timeout <= 0:
            raise ValueError("SCHEDULER_SOLVER_TIMEOUT_S must be > 0")
        pool_min = _integer(source, "AGENT_POOL_MIN_INSTANCES", 2)
        pool_max = _integer(source, "AGENT_POOL_MAX_INSTANCES", 4)
        if pool_min < 1:
            raise ValueError("AGENT_POOL_MIN_INSTANCES must be >= 1")
        if pool_max < pool_min:
            raise ValueError("AGENT_POOL_MAX_INSTANCES must be >= AGENT_POOL_MIN_INSTANCES")
        provider_concurrency = _integer(source, "PROVIDER_MAX_CONCURRENCY", 6)
        if provider_concurrency < 1:
            raise ValueError("PROVIDER_MAX_CONCURRENCY must be >= 1")
        return cls(
            database_url=source.get(
                "EXECUTION_DATABASE_URL",
                "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/execution_scheduler",
            ),
            workspace=workspace,
            tool_timeout_s=timeout,
            scheduler_policy=policy,
            resource_refresh_s=refresh,
            outbox_batch_size=_integer(source, "OUTBOX_BATCH_SIZE", 100),
            allowed_commands=frozenset(
                item.strip()
                for item in source.get("ALLOWED_COMMANDS", "python,python.exe").split(",")
                if item.strip()
            ),
            weight_latency=_number(source, "SCHEDULER_WEIGHT_LATENCY", 1.0),
            weight_token=_number(source, "SCHEDULER_WEIGHT_TOKEN", 1.0),
            weight_energy=_number(source, "SCHEDULER_WEIGHT_ENERGY", 1.0),
            weight_failure=_number(source, "SCHEDULER_WEIGHT_FAILURE", 10.0),
            weight_migration=_number(source, "SCHEDULER_WEIGHT_MIGRATION", 2.0),
            posterior_decay_per_day=_number(source, "POSTERIOR_DECAY_PER_DAY", 0.02),
            schema_version=source.get("EXECUTION_SCHEMA_VERSION", "0.1").strip() or "0.1",
            snapshot_interval=snapshot_interval,
            max_task_retries=max_task_retries,
            scheduler_allow_fallback=_boolean(source, "SCHEDULER_ALLOW_FALLBACK", True),
            scheduler_solver_timeout_s=solver_timeout,
            agent_pool_min_instances=pool_min,
            agent_pool_max_instances=pool_max,
            provider_max_concurrency=provider_concurrency,
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass
    return Settings.from_env()
