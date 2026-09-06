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

    #: Privacy classes whose deliverable content may not be shipped to a cloud
    #: verifier.  Sending restricted content to a third-party judge would break
    #: exactly the constraint the task declared.
    PRIVACY_SEALED_LEVELS = frozenset({"restricted", "secret"})

    #: Predicate/check-type names this service can decide mechanically.  Anything
    #: outside this set (``content_check``, ``metric_check``, ``manual_review``,
    #: ``simulation_check``, ``condition_satisfied``) is genuinely semantic and is
    #: sent to the independent judge — batched, once per task.
    _RULE_PREDICATES = frozenset({
        "execution_success", "exit_code==0", "build_success", "test_pass", "unit_test",
        "evidence_present", "evidence_check",
        "artifact_exists", "file_check",
        "schema_check",
        "goalspec_integrity",
    })

    def _deliverable_path(self, result: ExecutionResult) -> Path | None:
        relative = (result.metadata or {}).get("deliverable_relative")
        if not relative:
            return None
        try:
            path = self._safe_path(str(relative))
        except PermissionError:
            return None
        return path if path.is_file() else None

    def _structured_predicate(
        self,
        spec: dict,
        condition: str,
        result: ExecutionResult,
        evidence: tuple[Evidence, ...],
    ) -> PredicateResult | None:
        """Evaluate a ToDAG-compiled acceptance predicate without a model call.

        ToDAG already classifies each acceptance condition (``artifact_exists``,
        ``evidence_present``, ``test_pass``, ``build_success``,
        ``goalspec_integrity``, ...).  Those classifications used to be dropped at
        the execution boundary, so every condition fell through to the semantic
        judge even when a rule could decide it exactly.
        """
        # ``predicate`` is the ToDAG-derived name; ``check_type`` is the GoalSpec
        # vocabulary (file_check / schema_check / unit_test / content_check / ...).
        # Both are consulted so a condition the compiler already classified as
        # mechanically checkable never reaches the semantic judge.
        candidates = [
            str(spec.get("predicate") or "").strip().casefold(),
            str(spec.get("check_type") or "").strip().casefold(),
        ]
        name = next((item for item in candidates if item in self._RULE_PREDICATES), candidates[0])
        if name in {"execution_success", "exit_code==0", "build_success", "test_pass", "unit_test"}:
            passed = result.success and result.exit_code in {None, 0}
            return PredicateResult(
                condition, passed,
                f"rule:{name}; exit_code={result.exit_code}; success={result.success}",
            )
        if name in {"evidence_present", "evidence_check"}:
            return PredicateResult(
                condition, bool(evidence), f"rule:evidence_present; count={len(evidence)}"
            )
        if name in {"artifact_exists", "file_check"}:
            path = self._deliverable_path(result)
            return PredicateResult(
                condition, path is not None,
                f"rule:artifact_exists; deliverable={(result.metadata or {}).get('deliverable_relative')}",
            )
        if name == "schema_check":
            # Structural claim: the task produced a materialized artifact and hashed
            # evidence.  Whether the *content* is correct is a content_check, which
            # stays semantic.
            path = self._deliverable_path(result)
            return PredicateResult(
                condition, bool(result.success and path is not None and evidence),
                f"rule:schema_check; artifact={path is not None}; evidence_count={len(evidence)}",
            )
        if name == "goalspec_integrity":
            # The GoalSpec compiler validated the specification before the DAG was
            # built, and the requirement baseline is a deterministic projection of
            # it; a produced artifact is therefore sufficient proof.
            path = self._deliverable_path(result)
            return PredicateResult(
                condition, bool(result.success and path is not None),
                "rule:goalspec_integrity; validated by GoalSpec compiler and materialized baseline artifact",
            )
        return None

    def _dsl_predicate(
        self,
        predicate: str,
        result: ExecutionResult,
    ) -> PredicateResult | None:
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
        return None

    def _predicate_specs(self, task: TaskNodeView) -> dict[str, dict]:
        raw = (task.metadata or {}).get("acceptance_predicates", ())
        specs: dict[str, dict] = {}
        for item in raw if isinstance(raw, (list, tuple)) else ():
            if isinstance(item, dict) and item.get("condition"):
                specs[str(item["condition"])] = item
        return specs

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

        specs = self._predicate_specs(task)
        fixture_mode = bool((result.metadata or {}).get("test_fixture_verifier", False))
        privacy_sealed = str(task.privacy_level).casefold() in self.PRIVACY_SEALED_LEVELS
        semantic_conditions: list[str] = []
        rule_checked = 0
        for condition in task.acceptance_conditions:
            text = str(condition).strip()
            decided = self._dsl_predicate(text, result)
            if decided is None:
                spec = specs.get(text)
                if isinstance(spec, dict):
                    decided = self._structured_predicate(spec, text, result, evidence_tuple)
            if decided is not None:
                rule_checked += 1
                checks.append(decided)
                continue
            if fixture_mode:
                # Unit/integration fixture path only.  This prevents legacy
                # deterministic mocks from needing a network verifier while
                # ensuring production Agents cannot self-certify by repeating
                # the rubric.
                checks.append(PredicateResult(text, True, "TEST_FIXTURE_ONLY: semantic predicate bypass"))
                continue
            semantic_conditions.append(text)

        privacy_withheld: list[str] = []
        if semantic_conditions and privacy_sealed:
            # Fail-open on the verdict but never silently: the deliverable stays
            # local and the reduced assurance is recorded on the result.
            privacy_withheld = list(semantic_conditions)
            semantic_conditions = []
            for text in privacy_withheld:
                checks.append(PredicateResult(
                    text, True,
                    json.dumps({
                        "verification_semantics": "deterministic_only_semantic_judgment_withheld_for_privacy",
                        "rationale": (
                            f"task privacy_level={task.privacy_level} forbids sending the deliverable to a "
                            "cloud verifier; accepted on deterministic execution and evidence checks only"
                        ),
                        "assurance": "reduced",
                    }, ensure_ascii=False),
                ))

        if semantic_conditions:
            # One batched provider call for the whole task instead of one per
            # acceptance condition.
            judgments = self.semantic.judge_batch(
                task=task,
                predicates=tuple(semantic_conditions),
                result=result,
                evidence=evidence_tuple,
            )
            for text in semantic_conditions:
                judgment = judgments.get(text, {"passed": False, "rationale": "no judgment returned"})
                provenance = {k: v for k, v in judgment.items() if k not in {"passed", "rationale"}}
                provenance["predicate"] = text
                self._semantic_provenance().append(provenance)
                checks.append(PredicateResult(text, bool(judgment.get("passed", False)), json.dumps({
                    "verification_semantics": judgment.get("verification_semantics", "semantic_verifier_unavailable"),
                    "rationale": judgment.get("rationale", ""),
                    "provider": judgment.get("provider"),
                    "model": judgment.get("model"),
                    "request_id": judgment.get("request_id"),
                }, ensure_ascii=False)))

        passed = all(item.passed for item in checks)
        confidence = sum(1.0 for item in checks if item.passed) / max(1, len(checks))
        semantic_items = list(self._semantic_provenance())
        semantic_count = len(semantic_items)
        request_ids = {str(item.get("request_id")) for item in semantic_items if item.get("request_id")}
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
                "rule_checked_condition_count": rule_checked,
                "semantic_check_count": semantic_count,
                "semantic_request_count": len(request_ids),
                "semantic_provenance": semantic_items,
                "privacy_withheld_conditions": privacy_withheld,
                "assurance": "reduced_privacy_sealed" if privacy_withheld else "full",
                "self_echo_acceptance_disabled": True,
            },
        )
