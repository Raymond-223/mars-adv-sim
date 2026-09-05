from __future__ import annotations

import json
import uuid

from mosaic_omega.integration.production import build_production_chain, production_health


def main() -> int:
    """Verify production backends and one strict OR-Tools end-to-end run."""
    chain = build_production_chain()
    health = production_health(chain)
    run = None
    error = None
    try:
        run = chain.run(
            "生成一个可验证的最小交付物，必须保留证据。",
            run_id=f"production-smoke-{uuid.uuid4().hex[:8]}",
        )
    except Exception as exc:  # smoke must expose dependency/config errors verbatim
        error = f"{type(exc).__name__}: {exc}"

    policies = sorted(set(run.scheduler_policies)) if run is not None else []
    ready = bool(
        health.ready
        and run is not None
        and run.all_succeeded
        and policies
        and set(policies) == {"ortools"}
    )
    report = {
        "health": health.to_dict(),
        "run_id": run.run_id if run is not None else None,
        "all_succeeded": run.all_succeeded if run is not None else False,
        "scheduler_policies": policies,
        "event_count": len(run.events) if run is not None else 0,
        "evidence_count": len(run.evidence_manifest) if run is not None else 0,
        "verification_count": len(run.verification_results) if run is not None else 0,
        "error": error,
        "ready": ready,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if ready else 2


if __name__ == "__main__":
    raise SystemExit(main())
