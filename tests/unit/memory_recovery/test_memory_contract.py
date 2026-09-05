from dataclasses import replace

from mosaic_omega.memory_recovery import MemoryService, MemoryType
from mosaic_omega.memory_recovery.config import load_config


def make_goal_spec():
    return {
        "goal_id": "goal_mock_001",
        "objective": "Repair a ROS package and preserve the requested interface.",
        "hard_constraints": [
            "Do not change the public ROS interface.",
            "All patches must be backed by build/test evidence.",
        ],
        "prohibitions": ["Do not fabricate deployment results."],
        "soft_preferences": ["Prefer local tools before cloud tools."],
        "acceptance_predicates": ["colcon build succeeds", "pytest succeeds"],
        "budgets": {"token": 12000},
        "privacy_level": "internal",
        "source_spans": [{"start": 0, "end": 36}],
    }


def test_goalspec_core_is_noncompressible_and_schema_compatible(tmp_path):
    cfg = replace(load_config(), object_store_dir=str(tmp_path))
    svc = MemoryService(config=cfg)
    records = svc.ingest_goal_spec("run1", "task1", "node0", make_goal_spec())
    assert records
    assert all(r.memory_type == MemoryType.SEMANTIC for r in records)
    core = [r for r in records if set(r.tags) & {"goal", "hard_constraint", "prohibition"}]
    assert len(core) >= 4
    assert all(r.compressible is False for r in core)


def test_memory_record_enforces_noncompressible_core(tmp_path):
    cfg = replace(load_config(), object_store_dir=str(tmp_path))
    svc = MemoryService(config=cfg)
    record = svc.add_fact(
        run_id="r", task_id="t", node_id="n", fact="never drop", tags=["hard_constraint"], compressible=True
    )
    assert record.compressible is False
