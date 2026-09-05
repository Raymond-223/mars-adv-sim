from dataclasses import replace
from pathlib import Path

from mosaic_omega.memory_recovery import MemoryService
from mosaic_omega.memory_recovery.config import load_config


def test_vector_index_deletion_does_not_break_fact_recovery(tmp_path):
    cfg = replace(load_config(), object_store_dir=str(tmp_path))
    svc = MemoryService(config=cfg)
    svc.ingest_goal_spec("r", "t", "n0", {
        "objective": "Persistent goal", "hard_constraints": ["Persistent constraint"]
    })
    svc.add_fact(run_id="r", task_id="t", node_id="n1", fact="persistent semantic fact", title="fact")
    svc.clear_vector_index()
    pack = svc.build_context_pack(run_id="r", node_id="n1", task_id="t", query="persistent semantic")
    text = str(pack.to_dict())
    assert "Persistent goal" in text
    assert "Persistent constraint" in text
    assert "persistent semantic fact" in text


def test_corrupt_snapshot_falls_back_to_previous_and_can_apply(tmp_path):
    cfg = replace(load_config(), object_store_dir=str(tmp_path), snapshot_compress=True)
    svc = MemoryService(config=cfg)
    svc.add_fact(run_id="r", task_id="t", node_id="n", fact="version one")
    snap1 = svc.create_snapshot("r")
    svc.add_fact(run_id="r", task_id="t", node_id="n", fact="version two")
    snap2 = svc.create_snapshot("r")

    bad_key = snap2.state["object_key"]
    Path(svc.object_store.uri(bad_key)).write_bytes(b"not-a-valid-gzip")
    restored = svc.restore_snapshot(bad_key, apply=False, fallback_to_previous=True)
    assert restored["used_fallback"] is True
    assert restored["snapshot_id"] == snap1.snapshot_id

    new_svc = MemoryService(config=cfg)
    applied = new_svc.restore_snapshot(snap1.snapshot_id, apply=True)
    assert applied["run_id"] == "r"
    assert any(r.content == "version one" for r in new_svc.repository.query(run_id="r", limit=100))
    metrics = new_svc.export_metrics()
    assert metrics["snapshot_restore_consistency_rate"] == 1.0
