"""Decayed Beta posteriors for actor/task-type success probabilities."""

from __future__ import annotations

import math
import time

from .capability import CapabilityRegistry
from .models import CapabilityProfile


class BetaPosteriorUpdater:
    def __init__(
        self,
        registry: CapabilityRegistry,
        *,
        decay_per_day: float = 0.02,
        prior_alpha: float = 2.0,
        prior_beta: float = 1.0,
    ) -> None:
        self.registry = registry
        self.decay_per_day = max(0.0, float(decay_per_day))
        self.prior_alpha = max(1e-6, float(prior_alpha))
        self.prior_beta = max(1e-6, float(prior_beta))

    def update(
        self,
        actor_id: str,
        task_type: str,
        *,
        success: bool,
        quality: float = 1.0,
        timeout: bool = False,
        observed_at: float | None = None,
    ) -> CapabilityProfile:
        now = observed_at or time.time()
        profile = self.registry.get(actor_id)
        current = dict(profile.posterior.get(task_type, {}))
        alpha = float(current.get("alpha", self.prior_alpha))
        beta = float(current.get("beta", self.prior_beta))
        last = float(current.get("updated_at", now))
        age_days = max(0.0, now - last) / 86400.0
        factor = math.exp(-self.decay_per_day * age_days)
        alpha = self.prior_alpha + (alpha - self.prior_alpha) * factor
        beta = self.prior_beta + (beta - self.prior_beta) * factor

        bounded_quality = min(1.0, max(0.0, float(quality)))
        if success and not timeout:
            # Quality controls how much of the observation is counted as success.
            alpha += bounded_quality
            beta += 1.0 - bounded_quality
        else:
            beta += 1.5 if timeout else 1.0

        mean = alpha / (alpha + beta)
        profile.posterior[task_type] = {
            "alpha": alpha,
            "beta": beta,
            "mean": mean,
            "samples": float(current.get("samples", 0.0)) + 1.0,
            "updated_at": now,
        }
        # Global reliability is a conservative running summary; per-task posterior
        # is still used by the scheduler when available.
        profile.reliability = min(profile.reliability, mean) if not success else mean
        return self.registry.save(profile)

    def expected_success(self, profile: CapabilityProfile, task_type: str) -> float:
        sample = profile.posterior.get(task_type)
        if not sample:
            return min(1.0, max(0.0, profile.reliability))
        alpha = float(sample.get("alpha", self.prior_alpha))
        beta = float(sample.get("beta", self.prior_beta))
        return alpha / max(1e-9, alpha + beta)
