from dataclasses import replace

from mosaic_omega.memory_recovery import MemoryEvent, MemoryEventType, MemoryService
from mosaic_omega.memory_recovery.config import load_config


def test_1000_step_context_keeps_goal_constraints_and_is_bounded(tmp_path):
    cfg = replace(
        load_config(),
        object_store_dir=str(tmp_path),
        recall_limit=10,
        context_pack_max_tokens=1800,
        context_pack_max_chars=7000,
    )
    svc = MemoryService(config=cfg)
    run_id, task_id = "run_long", "task_long"
    svc.ingest_goal_spec(run_id, task_id, "node_000", {
        "objective": "Keep the original long-running task goal intact.",
        "hard_constraints": ["Never drop the immutable safety constraint."],
        "prohibitions": ["Never fabricate completion evidence."],
    })
    for i in range(1000):
        svc.ingest_event(MemoryEvent(
            event_type=MemoryEventType.TASK_SUCCEEDED,
            run_id=run_id,
            task_id=task_id,
            node_id=f"node_{i:03d}",
            content=f"Noise history payload {i:03d} should not flood final context.",
        ))
    svc.set_working_state(
        run_id, "node_999", current_goal="", recent_results=["node_998 handed off"], current_agent="agent_new"
    )
    pack = svc.build_context_pack(
        run_id=run_id,
        node_id="node_999",
        task_id=task_id,
        taskgraph_nodes=["node_998", "node_999"],
        query="original goal safety completion evidence",
    )
    d = pack.to_dict()
    assert "Keep the original" in d["goal"]
    assert "Never drop the immutable safety constraint." in d["hard_constraints"]
    assert "Never fabricate completion evidence." in d["prohibitions"]
    assert d["token_estimate"] <= cfg.context_pack_max_tokens
    assert len(d["memory_ids"]) <= cfg.recall_limit
    assert all("Noise history payload 500" not in x for x in d["relevant_experiences"])
    assert d["full_history_token_estimate"] > d["token_estimate"]
    assert d["compression_ratio"] < 1.0
