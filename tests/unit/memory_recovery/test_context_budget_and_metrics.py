from dataclasses import replace

from mosaic_omega.memory_recovery import MemoryService
from mosaic_omega.memory_recovery.config import load_config


def test_optional_content_truncates_before_hard_constraints(tmp_path):
    cfg = replace(
        load_config(), object_store_dir=str(tmp_path), context_pack_max_chars=500,
        context_pack_max_tokens=200, recall_limit=30,
    )
    svc = MemoryService(config=cfg)
    svc.ingest_goal_spec("r", "t", "n", {
        "objective": "Short goal",
        "hard_constraints": ["MUST_KEEP_CONSTRAINT"],
        "prohibitions": ["MUST_KEEP_PROHIBITION"],
    })
    for i in range(20):
        svc.add_fact(run_id="r", task_id="t", node_id="n", fact=("optional detail " * 20) + str(i))
    pack = svc.build_context_pack(run_id="r", node_id="n", task_id="t", query="optional detail")
    assert "MUST_KEEP_CONSTRAINT" in pack.hard_constraints
    assert "MUST_KEEP_PROHIBITION" in pack.prohibitions
    assert pack.truncated is True


def test_metrics_export_required_indicators(tmp_path):
    cfg = replace(load_config(), object_store_dir=str(tmp_path))
    svc = MemoryService(config=cfg)
    svc.ingest_goal_spec("r", "t", "n", {"objective": "goal"})
    svc.build_context_pack(run_id="r", node_id="n", task_id="t")
    metrics = svc.export_metrics()
    for key in [
        "context_pack_tokens", "full_history_tokens", "avg_compression_ratio",
        "avg_recall_latency_ms", "snapshot_restore_consistency_rate",
        "invalidation_propagation_accuracy", "key_memory_recall_rate",
    ]:
        assert key in metrics
