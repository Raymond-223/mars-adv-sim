"""Production wiring: PostgreSQL EventStore + Redis Memory + shared main chain."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..execution_scheduler import ExecutionSchedulerService
from ..execution_scheduler.config import Settings
from ..memory_recovery import MemoryService
from .main_chain import MosaicMainChain


@dataclass(frozen=True)
class ProductionHealth:
    postgres: bool
    redis: bool

    @property
    def ready(self) -> bool:
        return self.postgres and self.redis

    def to_dict(self) -> dict[str, bool]:
        return {"postgres": self.postgres, "redis": self.redis, "ready": self.ready}


def build_production_chain(*, workspace: str | Path | None = None) -> MosaicMainChain:
    settings = Settings.from_env()
    if workspace is not None:
        # Settings is frozen; reconstruct from the same environment contract.
        import os
        env = dict(os.environ)
        env["EXECUTION_WORKSPACE"] = str(Path(workspace).resolve())
        settings = Settings.from_env(env)
    execution = ExecutionSchedulerService(settings)
    memory = MemoryService(use_redis=True)
    if memory.store is None or not memory.store.ping():
        raise RuntimeError("Redis memory backend is not reachable")
    return MosaicMainChain(
        workspace=settings.workspace,
        scheduler_policy=settings.scheduler_policy,
        execution=execution,
        memory=memory,
    )


def production_health(chain: MosaicMainChain) -> ProductionHealth:
    postgres_ok = False
    redis_ok = False
    database: Any = chain.execution.database
    try:
        with database.engine.connect() as conn:
            conn.exec_driver_sql("SELECT 1")
        postgres_ok = True
    except Exception:
        postgres_ok = False
    try:
        redis_ok = bool(chain.memory.store and chain.memory.store.ping())
    except Exception:
        redis_ok = False
    return ProductionHealth(postgres=postgres_ok, redis=redis_ok)
