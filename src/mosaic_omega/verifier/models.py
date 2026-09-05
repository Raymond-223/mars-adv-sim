from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class PredicateResult:
    predicate: str
    passed: bool
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class VerificationResult:
    target_id: str
    passed: bool
    predicate_results: tuple[PredicateResult, ...]
    confidence: float
    evidence_refs: tuple[str, ...]
    risk_level: str
    action: str
    verifier: str = "deterministic-verifier"
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_id": self.target_id,
            "passed": self.passed,
            "predicate_results": [item.to_dict() for item in self.predicate_results],
            "confidence": self.confidence,
            "evidence_refs": list(self.evidence_refs),
            "risk_level": self.risk_level,
            "action": self.action,
            "verifier": self.verifier,
            "metadata": dict(self.metadata),
        }
