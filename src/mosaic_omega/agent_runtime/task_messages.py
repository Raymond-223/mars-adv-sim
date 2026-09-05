"""The fixed, low-entropy task-message schema.

Message category remains in the existing outer ``protocol.envelope`` ``type``
field.  The task-message body deliberately contains only the ten agreed
fields defined by ``TaskMessage`` below.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


MAX_SUMMARY_CHARS = 240
MAX_DELTA_TEXT_CHARS = 1_024
MAX_TTL_SECONDS = 86_400
TASK_MESSAGE_FIELDS = frozenset({
    "message_id", "sender", "receiver", "task_id", "summary", "facts",
    "constraints", "evidence_refs", "priority", "ttl",
})


class TaskMessageValidationError(ValueError):
    """Raised when a task message does not follow the shared schema."""


class DeltaOperation(str, Enum):
    ADD = "add"
    REPLACE = "replace"
    REMOVE = "remove"


def _required_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TaskMessageValidationError(f"{field_name} must be a non-empty string")
    return value


def _bounded_string(value: Any, field_name: str, maximum: int, *, required: bool) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise TaskMessageValidationError(f"{field_name} must be a string")
    if required and not value.strip():
        raise TaskMessageValidationError(f"{field_name} must not be empty")
    if len(value) > maximum:
        raise TaskMessageValidationError(f"{field_name} exceeds {maximum} characters")
    return value


def _dict_collection(raw: dict[str, Any], field_name: str) -> list[dict[str, Any]]:
    value = raw.get(field_name, [])
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise TaskMessageValidationError(f"{field_name} must be a list of objects")
    return value


@dataclass(frozen=True)
class FactDelta:
    """One fact change. ``remove`` carries no text; other operations do."""

    id: str
    op: DeltaOperation
    text: str | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "FactDelta":
        return cls(
            id=_required_string(raw.get("id"), "facts[].id"),
            op=DeltaOperation(raw.get("op")),
            text=raw.get("text"),
        )

    def validate(self, field_name: str = "facts") -> None:
        _required_string(self.id, f"{field_name}[].id")
        if not isinstance(self.op, DeltaOperation):
            raise TaskMessageValidationError(f"{field_name}[].op is invalid")
        if self.op is DeltaOperation.REMOVE:
            if self.text is not None:
                raise TaskMessageValidationError(f"{field_name}[].text must be null for remove")
            return
        _bounded_string(self.text, f"{field_name}[].text", MAX_DELTA_TEXT_CHARS, required=True)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "op": self.op.value, "text": self.text}


@dataclass(frozen=True)
class ConstraintDelta:
    """One constraint change, using the same add/replace/remove semantics."""

    id: str
    op: DeltaOperation
    text: str | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ConstraintDelta":
        return cls(
            id=_required_string(raw.get("id"), "constraints[].id"),
            op=DeltaOperation(raw.get("op")),
            text=raw.get("text"),
        )

    def validate(self) -> None:
        FactDelta(self.id, self.op, self.text).validate("constraints")

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "op": self.op.value, "text": self.text}


@dataclass(frozen=True)
class EvidenceRef:
    """A compact external-evidence delta.

    Existing payloads without ``op`` remain compatible and mean replace/add.
    ``remove`` requires only the evidence ID, allowing revoked/contaminated
    evidence to be withdrawn without forcing a full snapshot.
    """

    id: str
    artifact_id: str | None = None
    note: str | None = None
    op: DeltaOperation = DeltaOperation.REPLACE

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "EvidenceRef":
        try:
            op = DeltaOperation(raw.get("op", DeltaOperation.REPLACE.value))
        except ValueError as exc:
            raise TaskMessageValidationError("evidence_refs[].op is invalid") from exc
        artifact_id = raw.get("artifact_id")
        if op is not DeltaOperation.REMOVE:
            artifact_id = _required_string(artifact_id, "evidence_refs[].artifact_id")
        elif artifact_id is not None and not isinstance(artifact_id, str):
            raise TaskMessageValidationError("evidence_refs[].artifact_id must be a string or null")
        return cls(
            id=_required_string(raw.get("id"), "evidence_refs[].id"),
            artifact_id=artifact_id,
            note=raw.get("note"),
            op=op,
        )

    def validate(self) -> None:
        _required_string(self.id, "evidence_refs[].id")
        if not isinstance(self.op, DeltaOperation):
            raise TaskMessageValidationError("evidence_refs[].op is invalid")
        if self.op is DeltaOperation.REMOVE:
            if self.note is not None:
                raise TaskMessageValidationError("evidence_refs[].note must be null for remove")
            return
        _required_string(self.artifact_id, "evidence_refs[].artifact_id")
        _bounded_string(self.note, "evidence_refs[].note", MAX_DELTA_TEXT_CHARS, required=False)

    def to_dict(self) -> dict[str, Any]:
        # Preserve the old compact representation for the common replace/add
        # case; only removals pay the extra ``op`` byte cost.
        if self.op is DeltaOperation.REPLACE:
            return {"id": self.id, "artifact_id": self.artifact_id, "note": self.note}
        return {"id": self.id, "artifact_id": self.artifact_id, "note": self.note, "op": self.op.value}


@dataclass(frozen=True)
class TaskMessage:
    """Exactly the ten fields agreed for task-to-task communication."""

    message_id: str
    sender: str
    receiver: str
    task_id: str
    summary: str | None
    facts: tuple[FactDelta, ...]
    constraints: tuple[ConstraintDelta, ...]
    evidence_refs: tuple[EvidenceRef, ...]
    priority: int
    ttl: int

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "TaskMessage":
        if not isinstance(raw, dict):
            raise TaskMessageValidationError("task message must be a JSON object")
        actual_fields = frozenset(raw)
        if actual_fields != TASK_MESSAGE_FIELDS:
            missing = sorted(TASK_MESSAGE_FIELDS - actual_fields)
            extra = sorted(actual_fields - TASK_MESSAGE_FIELDS)
            raise TaskMessageValidationError(
                f"task message must contain exactly the ten agreed fields; missing={missing}, extra={extra}"
            )
        try:
            message = cls(
                message_id=raw["message_id"],
                sender=raw["sender"],
                receiver=raw["receiver"],
                task_id=raw["task_id"],
                summary=raw["summary"],
                facts=tuple(FactDelta.from_dict(item) for item in _dict_collection(raw, "facts")),
                constraints=tuple(ConstraintDelta.from_dict(item) for item in _dict_collection(raw, "constraints")),
                evidence_refs=tuple(EvidenceRef.from_dict(item) for item in _dict_collection(raw, "evidence_refs")),
                priority=raw["priority"],
                ttl=raw["ttl"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise TaskMessageValidationError(f"invalid task message: {exc}") from exc
        message.validate()
        return message

    @classmethod
    def create(
        cls,
        *,
        message_id: str,
        sender: str,
        receiver: str,
        task_id: str,
        summary: str | None = None,
        facts: tuple[FactDelta, ...] = (),
        constraints: tuple[ConstraintDelta, ...] = (),
        evidence_refs: tuple[EvidenceRef, ...] = (),
        priority: int = 5,
        ttl: int = 300,
    ) -> "TaskMessage":
        message = cls(
            message_id=message_id,
            sender=sender,
            receiver=receiver,
            task_id=task_id,
            summary=summary,
            facts=facts,
            constraints=constraints,
            evidence_refs=evidence_refs,
            priority=priority,
            ttl=ttl,
        )
        message.validate()
        return message

    def validate(self) -> None:
        for field_name in ("message_id", "sender", "receiver", "task_id"):
            _required_string(getattr(self, field_name), field_name)
        _bounded_string(self.summary, "summary", MAX_SUMMARY_CHARS, required=False)
        if type(self.priority) is not int or not 1 <= self.priority <= 10:
            raise TaskMessageValidationError("priority must be an integer from 1 to 10")
        if type(self.ttl) is not int or not 1 <= self.ttl <= MAX_TTL_SECONDS:
            raise TaskMessageValidationError(f"ttl must be an integer from 1 to {MAX_TTL_SECONDS}")
        for item in self.facts:
            item.validate()
        for item in self.constraints:
            item.validate()
        for item in self.evidence_refs:
            item.validate()

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "sender": self.sender,
            "receiver": self.receiver,
            "task_id": self.task_id,
            "summary": self.summary,
            "facts": [item.to_dict() for item in self.facts],
            "constraints": [item.to_dict() for item in self.constraints],
            "evidence_refs": [item.to_dict() for item in self.evidence_refs],
            "priority": self.priority,
            "ttl": self.ttl,
        }
