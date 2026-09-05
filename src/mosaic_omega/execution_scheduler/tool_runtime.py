"""Single permission/argument/timeout/idempotency boundary for all Agent tools."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any

from .capability import CapabilityRegistry
from .config import Settings
from .idempotency import DuplicateOperationInProgress, IdempotencyConflict, IdempotencyManager
from .models import ErrorClass, Evidence, ExecutionResult, ToolCall


@dataclass(frozen=True)
class ToolSpec:
    name: str
    required_arguments: frozenset[str]
    permissions: frozenset[str]
    side_effecting: bool = True


class ToolRuntime:
    def __init__(
        self,
        executor: Any,
        capabilities: CapabilityRegistry,
        idempotency: IdempotencyManager,
        settings: Settings,
    ) -> None:
        self.executor = executor
        self.capabilities = capabilities
        self.idempotency = idempotency
        self.settings = settings
        self._tools: dict[str, ToolSpec] = {}
        self._injected_failures: dict[str, list[dict[str, Any]]] = {}
        self.register(ToolSpec("task", frozenset({"description"}), frozenset(), False))
        self.register(ToolSpec("shell", frozenset({"command"}), frozenset({"shell.execute"})))
        self.register(ToolSpec("read_file", frozenset({"path"}), frozenset({"file.read"}), False))
        self.register(ToolSpec("write_file", frozenset({"path", "content"}), frozenset({"file.write"})))
        self.register(ToolSpec("build", frozenset(), frozenset({"build.execute"})))
        self.register(ToolSpec("test", frozenset(), frozenset({"test.execute"})))


    def inject_failure(
        self,
        run_id: str,
        *,
        reason: str,
        request_id: str,
        tool_name: str | None = None,
    ) -> dict[str, Any]:
        """Arm one explicit competition fault for the next matching tool call.

        This is not a random or fabricated failure.  It is a control-plane fault
        injection contract: the next matching ToolRuntime.execute call returns a
        real failed ExecutionResult, which the normal Orchestrator/RecoveryEngine
        handles through the same failure path as an executor error.
        """
        payload = {
            "run_id": str(run_id),
            "request_id": str(request_id),
            "reason": str(reason),
            "tool_name": str(tool_name) if tool_name else None,
            "armed_at": time.time(),
            "mode": "competition_controlled_next_tool_failure",
        }
        self._injected_failures.setdefault(str(run_id), []).append(payload)
        return dict(payload)

    def _consume_injected_failure(self, call: ToolCall) -> dict[str, Any] | None:
        queue = self._injected_failures.get(call.run_id, [])
        for index, item in enumerate(queue):
            if item.get("tool_name") not in {None, call.tool_name}:
                continue
            chosen = queue.pop(index)
            if not queue:
                self._injected_failures.pop(call.run_id, None)
            return chosen
        return None

    def register(self, spec: ToolSpec) -> None:
        if not spec.name:
            raise ValueError("tool name is required")
        self._tools[spec.name] = spec

    def _validate(self, call: ToolCall) -> tuple[float, ToolSpec]:
        spec = self._tools.get(call.tool_name)
        if spec is None:
            raise ValueError(f"unknown tool: {call.tool_name}")
        missing = spec.required_arguments - set(call.arguments)
        if missing:
            raise ValueError(f"missing tool arguments: {sorted(missing)}")
        actor = self.capabilities.get(call.actor_id)
        required = spec.permissions | call.required_permissions
        if "*" not in actor.permissions and not required.issubset(actor.permissions):
            raise PermissionError(
                f"actor {call.actor_id} lacks permissions: {sorted(required - actor.permissions)}"
            )
        timeout = call.timeout_s if call.timeout_s is not None else self.settings.tool_timeout_s
        if timeout <= 0 or timeout > self.settings.tool_timeout_s:
            raise ValueError(
                f"tool timeout must be within 0..{self.settings.tool_timeout_s} seconds"
            )
        if spec.side_effecting and not call.idempotency_key:
            raise ValueError("side-effecting tool requires idempotency_key")
        return timeout, spec

    @staticmethod
    def _fingerprint(call: ToolCall) -> str:
        document = {
            "run_id": call.run_id,
            "task_id": call.task_id,
            "actor_id": call.actor_id,
            "tool_name": call.tool_name,
            "arguments": call.arguments,
        }
        raw = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def _classify_error(error: str | None) -> ErrorClass | None:
        if not error:
            return None
        text = error.casefold()
        if "timeout" in text or "temporar" in text or "connection" in text or "network" in text:
            return ErrorClass.RETRYABLE
        if "not found" in text or "unavailable" in text or "offline" in text:
            return ErrorClass.REPLACEABLE
        if "permission" in text or "invalid" in text or "unknown tool" in text:
            return ErrorClass.SAFE_STOP
        return ErrorClass.RETRYABLE

    def execute(self, call: ToolCall) -> tuple[ExecutionResult, Evidence]:
        started = time.time()
        injected = self._consume_injected_failure(call)
        if injected is not None:
            error = f"InjectedToolFailure: {injected['reason']}"
            result = ExecutionResult(
                call.call_id,
                False,
                error=error,
                started_at=started,
                finished_at=time.time(),
                metadata={
                    "fault_injection": injected,
                    "measurement_semantics": "controlled competition fault injected at ToolRuntime boundary",
                },
                error_class=ErrorClass.RETRYABLE,
            )
            return result, self._evidence(call, result, reused=False)
        try:
            timeout, spec = self._validate(call)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            result = ExecutionResult(
                call.call_id,
                False,
                error=error,
                started_at=started,
                finished_at=time.time(),
                error_class=self._classify_error(error),
            )
            return result, self._evidence(call, result, reused=False)

        def operation() -> ExecutionResult:
            return self.executor.execute(call, timeout)

        try:
            if spec.side_effecting:
                result, reused = self.idempotency.execute_once(
                    call.idempotency_key,
                    operation,
                    fingerprint=self._fingerprint(call),
                )
            else:
                result, reused = operation(), False
        except (DuplicateOperationInProgress, IdempotencyConflict) as exc:
            error = f"{type(exc).__name__}: {exc}"
            result = ExecutionResult(
                call.call_id,
                False,
                error=error,
                started_at=started,
                finished_at=time.time(),
                error_class=ErrorClass.SAFE_STOP,
            )
            return result, self._evidence(call, result, reused=False)

        metadata = dict(result.metadata)
        metadata.update({"idempotency_key": call.idempotency_key, "reused": reused})
        result = ExecutionResult(
            result.call_id,
            result.success,
            result.output,
            result.error,
            result.exit_code,
            result.started_at,
            result.finished_at,
            metadata,
            result.error_class or self._classify_error(result.error),
        )
        return result, self._evidence(call, result, reused=reused)

    def _evidence(self, call: ToolCall, result: ExecutionResult, *, reused: bool) -> Evidence:
        raw = f"{result.output}\n{result.error or ''}".encode("utf-8")
        digest = hashlib.sha256(raw).hexdigest()
        evidence_dir = self.settings.workspace / ".mosaic_evidence" / call.run_id / call.task_id
        evidence_dir.mkdir(parents=True, exist_ok=True)
        artifact = evidence_dir / f"{digest}.txt"
        # Content-addressed evidence is immutable by digest.  A stale file from
        # an older implementation must never be trusted merely because its name
        # matches; verify bytes and replace atomically on mismatch.
        valid_existing = (
            artifact.is_file()
            and hashlib.sha256(artifact.read_bytes()).hexdigest() == digest
        )
        if not valid_existing:
            temporary = artifact.with_name(f".tmp-{call.call_id}")
            temporary.write_bytes(raw)
            temporary.replace(artifact)
        result_metadata = dict(result.metadata or {})
        deliverable_sha256 = None
        deliverable_relative = result_metadata.get("deliverable_relative")
        if deliverable_relative:
            try:
                deliverable = (self.settings.workspace / str(deliverable_relative).replace("\\", "/")).resolve()
                root = self.settings.workspace.resolve()
                if (deliverable == root or root in deliverable.parents) and deliverable.is_file():
                    deliverable_sha256 = hashlib.sha256(deliverable.read_bytes()).hexdigest()
            except OSError:
                deliverable_sha256 = None
        return Evidence(
            run_id=call.run_id,
            task_id=call.task_id,
            kind="tool_execution",
            digest=digest,
            content=result.output,
            uri=artifact.as_uri(),
            producer=call.actor_id,
            mime_type="text/plain",
            scope="task",
            verification_status="UNVERIFIED",
            trace_id=call.trace_id,
            parent_event_id=call.parent_event_id,
            actor_id=call.actor_id,
            model_id=call.model_id,
            schema_version=call.schema_version,
            metadata={
                "call_id": call.call_id,
                "tool_name": call.tool_name,
                "success": result.success,
                "reused": reused,
                "error_class": result.error_class.value if result.error_class else None,
                "error": result.error,
                "artifact_path": str(artifact),
                "result_metadata": result_metadata,
                "deliverable_relative": deliverable_relative,
                "deliverable_sha256": deliverable_sha256,
            },
        )
