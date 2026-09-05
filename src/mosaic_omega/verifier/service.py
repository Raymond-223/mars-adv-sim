"""Evidence-gated verifier with deterministic predicates and independent semantic checks.

Deterministic DSL:
* ``execution_success`` / ``exit_code==0``
* ``contains:<text>``
* ``file_exists:<relative path>``
* ``file_contains:<relative path>:<text>``

Natural-language acceptance conditions are *not* accepted by echo matching.  In
production they require an independent provider-backed semantic judgment.  A
narrow test-fixture escape hatch exists only when the executor result is marked
``test_fixture_verifier=True``; it is never emitted by real Agents.
"""
from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Iterable
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

from ..execution_scheduler.models import Evidence, ExecutionResult, TaskNodeView
from .models import PredicateResult, VerificationResult
from .semantic import ProviderSemanticJudge


class VerifierService:
    def __init__(self, workspace: str | Path) -> None:
        self.workspace = Path(workspace).resolve()
        self.semantic = ProviderSemanticJudge(self.workspace)
        self._semantic_local = threading.local()

    def _semantic_provenance(self) -> list[dict]:
        items = getattr(self._semantic_local, "provenance", None)
        if items is None:
            items = []
            self._semantic_local.provenance = items
        return items

    def _safe_path(self, relative: str) -> Path:
        path = (self.workspace / relative.replace("\\", "/")).resolve()
        if path != self.workspace and self.workspace not in path.parents:
            raise PermissionError("verification path escapes workspace")
        return path

    @staticmethod
    def _evidence_integrity(evidence: Evidence) -> PredicateResult:
        raw = f"{evidence.content}\n{evidence.metadata.get('error', '') or ''}".encode("utf-8")
        digest = hashlib.sha256(raw).hexdigest()
        detail = "content sha256 verified"
        if evidence.uri and evidence.uri.startswith("file:"):
            artifact_path = evidence.metadata.get("artifact_path") if isinstance(evidence.metadata, dict) else None
            if artifact_path:
                artifact = Path(artifact_path)
            else:
                parsed = urlparse(evidence.uri)
                artifact = Path(url2pathname(unquote(parsed.path)))
            if not artifact.is_file():
                return PredicateResult(
                    predicate=f"evidence_hash:{evidence.evidence_id}",
                    passed=False,
                    detail="evidence artifact missing",
                )
            digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
            detail = "artifact sha256 verified"
        passed = evidence.digest == digest
        return PredicateResult(
            predicate=f"evidence_hash:{evidence.evidence_id}",
            passed=passed,
            detail=detail if passed else "evidence digest mismatch",
        )

    def _predicate(
        self,
        predicate: str,
        task: TaskNodeView,
        result: ExecutionResult,
        evidence: tuple[Evidence, ...],
    ) -> PredicateResult:
        text = predicate.strip()
        folded = text.casefold()
        if folded in {"execution_success", "exit_code==0", "exit_code = 0"}:
            passed = result.success and result.exit_code in {None, 0}
            return PredicateResult(text, passed, f"exit_code={result.exit_code}")
        if folded.startswith("contains:"):
            needle = text.split(":", 1)[1]
            passed = needle.casefold() in result.output.casefold()
            return PredicateResult(text, passed, "output contains value" if passed else "value missing from output")
        if folded.startswith("file_exists:"):
            relative = text.split(":", 1)[1].strip()
            path = self._safe_path(relative)
            return PredicateResult(text, path.is_file(), f"workspace-relative:{relative}")
        if folded.startswith("file_contains:"):
            parts = text.split(":", 2)
            if len(parts) != 3:
                return PredicateResult(text, False, "expected file_contains:<path>:<text>")
            path = self._safe_path(parts[1].strip())
            if not path.is_file():
                return PredicateResult(text, False, f"missing workspace-relative file:{parts[1].strip()}")
            content = path.read_text(encoding="utf-8", errors="replace")
            passed = parts[2].casefold() in content.casefold()
            return PredicateResult(text, passed, f"workspace-relative:{parts[1].strip()}")

        # Unit/integration fixture path only.  This prevents legacy deterministic
        # mocks from needing a network verifier while ensuring production Agents
        # cannot self-certify by repeating the rubric.
        if bool((result.metadata or {}).get("test_fixture_verifier", False)):
            return PredicateResult(text, True, "TEST_FIXTURE_ONLY: semantic predicate bypass")

        judgment = self.semantic.judge(task=task, predicate=text, result=result, evidence=evidence)
        provenance = {k: v for k, v in judgment.items() if k not in {"passed", "rationale"}}
        provenance["predicate"] = text
        self._semantic_provenance().append(provenance)
        detail = {
            "verification_semantics": judgment.get("verification_semantics", "semantic_verifier_unavailable"),
            "rationale": judgment.get("rationale", ""),
            "provider": judgment.get("provider"),
            "model": judgment.get("model"),
            "request_id": judgment.get("request_id"),
        }
        return PredicateResult(text, bool(judgment.get("passed", False)), json.dumps(detail, ensure_ascii=False))

    def verify(
        self,
        task: TaskNodeView,
        result: ExecutionResult,
        evidence: Iterable[Evidence],
    ) -> VerificationResult:
        evidence_tuple = tuple(evidence)
        self._semantic_local.provenance = []
        checks: list[PredicateResult] = [
            PredicateResult("execution_success", bool(result.success), result.error or ""),
            PredicateResult("evidence_present", bool(evidence_tuple), f"count={len(evidence_tuple)}"),
        ]
        checks.extend(self._evidence_integrity(item) for item in evidence_tuple)
        checks.extend(
            self._predicate(item, task, result, evidence_tuple)
            for item in task.acceptance_conditions
        )
        passed = all(item.passed for item in checks)
        confidence = sum(1.0 for item in checks if item.passed) / max(1, len(checks))
        semantic_items = list(self._semantic_provenance())
        semantic_count = len(semantic_items)
        return VerificationResult(
            target_id=task.task_id,
            passed=passed,
            predicate_results=tuple(checks),
            confidence=confidence,
            evidence_refs=tuple(item.evidence_id for item in evidence_tuple),
            risk_level=task.risk,
            action="accept" if passed else "recover_or_safe_stop",
            verifier="deterministic+independent-semantic" if semantic_count else "deterministic-verifier",
            metadata={
                "result_success": result.success,
                "check_count": len(checks),
                "semantic_check_count": semantic_count,
                "semantic_provenance": semantic_items,
                "self_echo_acceptance_disabled": True,
            },
        )
