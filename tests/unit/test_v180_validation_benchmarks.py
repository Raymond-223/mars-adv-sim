from __future__ import annotations

from scripts.benchmark_memory_ablation import run_memory_ablation
from scripts.benchmark_scheduler_ablation import run_scheduler_ablation


def test_memory_ablation_keeps_estimates_separate_from_provider_tokens(tmp_path):
    result = run_memory_ablation(str(tmp_path / "memory"))
    modes = result["modes"]
    assert modes["full_history"]["key_fact_recall_pct"] == 100.0
    assert modes["context_pack"]["key_fact_recall_pct"] == 100.0
    assert modes["context_pack"]["estimated_tokens"] < modes["full_history"]["estimated_tokens"]
    assert modes["context_pack"]["provider_api_input_tokens"] is None
    assert "estimated" in result["measurement_mode"]


def test_scheduler_ablation_never_labels_fallback_as_ortools(tmp_path):
    result = run_scheduler_ablation(str(tmp_path / "scheduler"))
    assert result["modes"]["round_robin"]["available"] is True
    assert result["modes"]["greedy"]["available"] is True
    ortools = result["modes"]["ortools"]
    if ortools["available"]:
        # The invariant is that an OR-Tools-labelled assignment really came from an
        # OR-Tools solver, not that the algorithm is literally named "ortools".
        assert all(
            str(item["solver_provenance"]["engine"]).startswith("ortools.")
            and item["solver_provenance"]["status"] in {"OPTIMAL", "FEASIBLE"}
            for item in ortools["assignments"]
        )
    else:
        assert "fallback" in ortools["reason"].lower()
        assert ortools["assignments"] == []
