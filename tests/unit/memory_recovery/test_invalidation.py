from dataclasses import replace

from mosaic_omega.memory_recovery import MemoryService, VerificationStatus
from mosaic_omega.memory_recovery.config import load_config


def test_evidence_invalidation_propagates_and_retriever_filters(tmp_path):
    cfg = replace(load_config(), object_store_dir=str(tmp_path))
    svc = MemoryService(config=cfg)
    root = svc.add_fact(
        run_id="r", task_id="t", node_id="n1", fact="bad evidence conclusion",
        evidence_refs=["ev_bad"],
    )
    dependent = svc.add_fact(
        run_id="r", task_id="t", node_id="n2", fact="derived conclusion",
        metadata={"depends_on_memory_ids": [root.memory_id]},
    )
    updated = svc.invalidate_by_evidence("ev_bad", reason="evidence rejected")
    assert {r.memory_id for r in updated} >= {root.memory_id, dependent.memory_id}
    assert svc.repository.get(root.memory_id).verification_status == VerificationStatus.STALE
    assert svc.repository.get(dependent.memory_id).verification_status == VerificationStatus.STALE
    recalled = svc.retrieve(run_id="r", node_id="n2", task_id="t", query="conclusion", limit=10)
    ids = {r.memory_id for r in recalled["records"]}
    assert root.memory_id not in ids
    assert dependent.memory_id not in ids


def test_rejected_memory_never_returns_as_valid_fact(tmp_path):
    cfg = replace(load_config(), object_store_dir=str(tmp_path))
    svc = MemoryService(config=cfg)
    record = svc.add_fact(run_id="r", task_id="t", node_id="n", fact="false memory")
    svc.reject_memory(record.memory_id, "verifier rejected")
    recalled = svc.retrieve(run_id="r", node_id="n", task_id="t", query="false memory")
    assert record.memory_id not in {r.memory_id for r in recalled["records"]}
