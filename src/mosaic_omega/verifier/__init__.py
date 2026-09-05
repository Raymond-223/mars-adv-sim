"""Deterministic evidence-gated verification service."""
from .models import PredicateResult, VerificationResult
from .service import VerifierService

__all__ = ["PredicateResult", "VerificationResult", "VerifierService"]
