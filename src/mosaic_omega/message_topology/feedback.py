"""Receiver-reported decision feedback without pretending correlation is causation."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import time
import uuid
from typing import Any


@dataclass(frozen=True)
class DecisionFeedbackEvent:
    feedback_id: str
    message_id: str
    task_id: str
    receiver: str
    consumed: bool
    cited: bool = False
    decision_changed: bool = False
    outcome_score: float | None = None
    evidence_type: str = "runtime_usage_proxy"
    model_id: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    observed_at: float = 0.0

    @classmethod
    def create(
        cls,
        *,
        message_id: str,
        task_id: str,
        receiver: str,
        consumed: bool,
        cited: bool = False,
        decision_changed: bool = False,
        outcome_score: float | None = None,
        evidence_type: str = "runtime_usage_proxy",
        model_id: str | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
    ) -> "DecisionFeedbackEvent":
        event = cls(
            feedback_id=uuid.uuid4().hex,
            message_id=message_id,
            task_id=task_id,
            receiver=receiver,
            consumed=consumed,
            cited=cited,
            decision_changed=decision_changed,
            outcome_score=outcome_score,
            evidence_type=evidence_type,
            model_id=model_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            observed_at=time.time(),
        )
        event.validate()
        return event

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "DecisionFeedbackEvent":
        event = cls(
            feedback_id=str(raw["feedback_id"]),
            message_id=str(raw["message_id"]),
            task_id=str(raw["task_id"]),
            receiver=str(raw["receiver"]),
            consumed=bool(raw["consumed"]),
            cited=bool(raw.get("cited", False)),
            decision_changed=bool(raw.get("decision_changed", False)),
            outcome_score=(float(raw["outcome_score"]) if raw.get("outcome_score") is not None else None),
            evidence_type=str(raw.get("evidence_type", "runtime_usage_proxy")),
            model_id=str(raw["model_id"]) if raw.get("model_id") is not None else None,
            input_tokens=int(raw["input_tokens"]) if raw.get("input_tokens") is not None else None,
            output_tokens=int(raw["output_tokens"]) if raw.get("output_tokens") is not None else None,
            observed_at=float(raw.get("observed_at", time.time())),
        )
        event.validate()
        return event

    def validate(self) -> None:
        if not all((self.feedback_id, self.message_id, self.task_id, self.receiver, self.evidence_type)):
            raise ValueError("decision feedback identifiers must be non-empty")
        if self.outcome_score is not None and not 0 <= self.outcome_score <= 1:
            raise ValueError("outcome_score must be within [0, 1]")
        if self.decision_changed and not self.consumed:
            raise ValueError("an unconsumed message cannot change a decision")
        if any(value is not None and value < 0 for value in (self.input_tokens, self.output_tokens)):
            raise ValueError("token usage values must be non-negative")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def contribution_score(self) -> float:
        score = 0.35 if self.consumed else 0.0
        score += 0.25 if self.cited else 0.0
        score += 0.25 if self.decision_changed else 0.0
        score += 0.15 * (self.outcome_score if self.outcome_score is not None else 0.0)
        return round(min(score, 1.0), 6)


class DecisionFeedbackStore:
    def __init__(self) -> None:
        self._events: dict[str, DecisionFeedbackEvent] = {}

    def report(self, event: DecisionFeedbackEvent) -> bool:
        event.validate()
        if event.feedback_id in self._events:
            return False
        self._events[event.feedback_id] = event
        return True

    def for_message(self, message_id: str) -> tuple[DecisionFeedbackEvent, ...]:
        return tuple(event for event in self._events.values() if event.message_id == message_id)

    def contribution(self, message_id: str) -> float:
        events = self.for_message(message_id)
        return max((event.contribution_score for event in events), default=0.0)

    def token_usage(self) -> dict[str, int]:
        measured = [
            event for event in self._events.values()
            if event.input_tokens is not None and event.output_tokens is not None
        ]
        return {
            "measured_events": len(measured),
            "input_tokens": sum(event.input_tokens or 0 for event in measured),
            "output_tokens": sum(event.output_tokens or 0 for event in measured),
        }
