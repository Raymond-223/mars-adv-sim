from dataclasses import replace

from mosaic_omega.memory_recovery import MemoryEvent, MemoryEventType, MemoryService
from mosaic_omega.memory_recovery.config import load_config


def test_retrieval_explicitly_joins_taskgraph_and_evidence(tmp_path):
    cfg = replace(load_config(), object_store_dir=str(tmp_path))
    svc = MemoryService(config=cfg)
    svc.ingest_event(MemoryEvent(
        event_type=MemoryEventType.BUILD_FAILED, run_id="r", task_id="t", node_id="pred",
        content="predecessor build failed", evidence_refs=["ev1"],
    ))
    svc.add_fact(
        run_id="r", task_id="other", node_id="elsewhere", fact="evidence-linked root cause",
        title="root cause", evidence_refs=["ev1"],
    )
    result = svc.retrieve(
        run_id="r", node_id="cur", task_id="t", taskgraph_nodes=["pred", "cur"], evidence_ids=["ev1"], limit=10
    )
    text = " ".join(r.content for r in result["records"])
    assert "predecessor build failed" in text
    assert "evidence-linked root cause" in text


def test_permission_is_hard_gate_even_for_hard_constraints(tmp_path):
    cfg = replace(load_config(), object_store_dir=str(tmp_path))
    svc = MemoryService(config=cfg)
    svc.add_fact(
        run_id="r", task_id="t", node_id="n", fact="private secret constraint",
        tags=["hard_constraint"], compressible=False, access_scope=["private"],
    )
    public = svc.retrieve(run_id="r", node_id="n", task_id="t", allowed_scopes=["public"], limit=10)
    assert all("private secret" not in r.content for r in public["records"])
