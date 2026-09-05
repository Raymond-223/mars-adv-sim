"""Idempotency-key, request-fingerprint and repeated-call protection."""

from __future__ import annotations

from collections.abc import Callable

from .models import ExecutionResult


class DuplicateOperationInProgress(RuntimeError):
    pass


class IdempotencyConflict(ValueError):
    pass


class IdempotencyManager:
    def __init__(self, database: object) -> None:
        self.database = database

    def execute_once(
        self,
        key: str,
        operation: Callable[[], ExecutionResult],
        *,
        fingerprint: str | None = None,
    ) -> tuple[ExecutionResult, bool]:
        if not key:
            raise ValueError("idempotency_key is required for side-effecting tools")
        claimed, existing = self.database.begin_idempotency(key, fingerprint=fingerprint)
        if not claimed:
            if (
                existing
                and fingerprint
                and existing.get("fingerprint")
                and existing["fingerprint"] != fingerprint
            ):
                raise IdempotencyConflict(
                    f"idempotency key reused with different request: {key}"
                )
            if existing and existing["status"] in {"SUCCEEDED", "FAILED"} and existing.get("result"):
                return ExecutionResult.from_dict(existing["result"]), True
            raise DuplicateOperationInProgress(f"operation is already running: {key}")
        try:
            result = operation()
        except Exception as exc:
            failed = ExecutionResult(
                call_id=key,
                success=False,
                error=f"{type(exc).__name__}: {exc}",
            )
            self.database.finish_idempotency(
                key, result=failed.to_dict(), error=failed.error
            )
            return failed, False
        self.database.finish_idempotency(
            key,
            result=result.to_dict(),
            error=result.error if not result.success else None,
        )
        return result, False

    def lookup(self, key: str) -> dict | None:
        return self.database.get_idempotency(key)
