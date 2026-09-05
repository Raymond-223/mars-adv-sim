from __future__ import annotations

import unittest
from mosaic_omega.todag.engine import ToDAGEngine
from mosaic_omega.todag.models import INPUT_FIELDS, DAGNode, LongTaskInput
from mosaic_omega.todag.graph import critical_path, descendants, topological_sort


def sample_input() -> dict:
    return {
        "main_goal": "build a validated report",
        "hard_constraints": ["use evidence"],
        "soft_preferences": ["keep it concise"],
        "acceptance_conditions": [
            "explain the method",
            "compare risks",
            "publish the result",
        ],
        "budget": {"time_hours": 5, "cost_limit": 10},
        "prohibitions": ["do not invent sources"],
    }


def enriched_input() -> dict:
    return {
        "main_goal": {
            "goal_text": "repair a ROS package and prove the repair",
            "goal_type": "software_engineering",
            "domain": "robotics",
            "sub_goals": [
                {
                    "name": "diagnose build failure",
                    "required_skills": ["code", "robotics"],
                    "outputs": ["diagnosis", "evidence:diagnosis"],
                    "priority": 9,
                },
                {
                    "name": "apply minimal patch",
                    "required_skill": "code",
                    "depends_on": ["diagnose build failure"],
                    "rollback_checkpoint": True,
                    "risk": "high",
                },
            ],
        },
        "hard_constraints": [
            {
                "constraint": "user data must stay local",
                "type": "privacy",
                "predicate": "data_local_only",
                "source_span": "user data must stay local",
                "confidence": 0.99,
            }
        ],
        "soft_preferences": [
            {"preference": "minimize latency", "objective": "latency_minimize", "weight": 0.8}
        ],
        "acceptance_conditions": [
            {
                "condition": "colcon build succeeds",
                "predicate": "build_success",
                "check_type": "execution",
                "target_sub_goals": ["apply minimal patch"],
                "args": {"command": "colcon build"},
                "confidence": 0.98,
            }
        ],
        "budget": {"time_hours": 1, "max_latency_ms": 100},
        "prohibitions": [
            {"prohibition": "do not upload source code", "type": "privacy"}
        ],
    }


def nodes_by_type(snapshot: dict, node_type: str) -> list[dict]:
    return [node for node in snapshot["nodes"] if node["node_type"] == node_type]


class GoalSpecContractTests(unittest.TestCase):
    def test_top_level_contract_is_exactly_six_fields(self) -> None:
        spec = LongTaskInput.from_dict(enriched_input())
        self.assertEqual(tuple(spec.to_dict()), INPUT_FIELDS)
        self.assertEqual(set(spec.to_dict()), set(INPUT_FIELDS))
        invalid = enriched_input()
        invalid["metadata"] = {"forbidden": True}
        with self.assertRaises(ValueError):
            LongTaskInput.from_dict(invalid)

    def test_enriched_values_are_preserved_inside_six_fields(self) -> None:
        spec = LongTaskInput.from_dict(enriched_input())
        exported = spec.to_dict()
        self.assertIsInstance(exported["main_goal"], dict)
        self.assertIsInstance(exported["hard_constraints"][0], dict)
        self.assertEqual(spec.main_goal_text, "repair a ROS package and prove the repair")
        self.assertEqual(len(spec.sub_goals), 2)


class ToDAGEngineTests(unittest.TestCase):
    def test_build_has_typed_taskgraph_and_execution_projection(self) -> None:
        engine = ToDAGEngine(planning_horizon=5)
        result = engine.build(sample_input())
        self.assertEqual(result["schema_version"], "2.0")
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["final_task_id"], "task_final_review")
        self.assertGreaterEqual(len(result["nodes"]), 5)
        self.assertEqual(set(result["input"]), set(INPUT_FIELDS))
        self.assertTrue(result["ready_task_ids"])
        self.assertLessEqual(len(result["rolling_window_task_ids"]), 5)
        self.assertIn("critical_path", result)
        self.assertTrue(all("type" in edge for edge in result["edges"]))
        plan = engine.execution_plan()
        self.assertEqual(len(plan), len(result["nodes"]))

    def test_enriched_goalspec_drives_dependencies_predicates_and_placement(self) -> None:
        engine = ToDAGEngine()
        result = engine.build(enriched_input())
        work = nodes_by_type(result, "work")
        verify = nodes_by_type(result, "verification")
        self.assertEqual(len(work), 2)
        self.assertEqual(len(verify), 1)
        patch = next(node for node in work if "minimal patch" in node["title"])
        diagnose = next(node for node in work if "diagnose" in node["title"])
        self.assertIn(diagnose["task_id"], patch["depends_on"])
        self.assertEqual(patch["dependency_types"][diagnose["task_id"]], "exec")
        self.assertEqual(verify[0]["acceptance_predicates"][0]["predicate"], "build_success")
        self.assertEqual(verify[0]["evidence_dependencies"], [patch["task_id"]])
        self.assertTrue(patch["resource_requirements"]["require_local_data"])
        plan_node = next(item for item in engine.execution_plan() if item["task_id"] == patch["task_id"])
        self.assertTrue(plan_node["placement"]["require_local_data"])
        self.assertEqual(plan_node["placement"]["data_sensitivity"], "restricted")

    def test_update_recomputes_only_changed_branch_and_final_gate(self) -> None:
        engine = ToDAGEngine()
        before = engine.build(sample_input())
        requirement_id = before["entry_task_ids"][0]
        work = nodes_by_type(before, "work")
        verify = nodes_by_type(before, "verification")
        work_by_condition = {node["acceptance_conditions"][0] if node["acceptance_conditions"] else node["description"]: node for node in work}
        # Fallback work nodes do not copy acceptance_conditions; use matching verifier dependency.
        first_verify = verify[0]
        first_work_id = first_verify["depends_on"][0]
        second_work_id = verify[1]["depends_on"][0]

        engine.set_node_result(requirement_id, {"ok": True}, evidence=[{"evidence_id": "req-evi"}])
        engine.set_node_result(first_work_id, {"artifact": "keep"}, evidence=[{"evidence_id": "work-evi"}])
        engine.set_node_result(first_verify["task_id"], {"verified": True}, evidence=[{"evidence_id": "verify-evi"}])
        preserved_before = engine.snapshot()
        preserved_verify = next(n for n in preserved_before["nodes"] if n["task_id"] == first_verify["task_id"])
        preserved_fingerprint = preserved_verify["fingerprint"]

        result = engine.update_node(second_work_id, {"description": "changed independent work package"})
        self.assertIn(second_work_id, result["change_set"]["changed_node_ids"])
        self.assertIn("task_final_review", result["change_set"]["invalidated_node_ids"])
        preserved_after = next(n for n in result["nodes"] if n["task_id"] == first_verify["task_id"])
        self.assertEqual(preserved_after["result"], {"verified": True})
        self.assertEqual(preserved_after["status"], "completed")
        self.assertEqual(preserved_after["fingerprint"], preserved_fingerprint)
        second_verify = next(
            n for n in result["nodes"]
            if n["node_type"] == "verification" and second_work_id in n["depends_on"]
        )
        after_recompute = engine.set_node_result(second_work_id, {"artifact": "new"}, evidence=[{"evidence_id": "new-evi"}])
        self.assertIn(second_verify["task_id"], after_recompute["ready_task_ids"])

    def test_goalspec_change_adds_branch_without_restarting_unchanged_branch(self) -> None:
        engine = ToDAGEngine()
        before = engine.build(sample_input())
        requirement_id = before["entry_task_ids"][0]
        first_verify = nodes_by_type(before, "verification")[0]
        first_work_id = first_verify["depends_on"][0]
        engine.set_node_result(requirement_id, {"ok": True}, evidence=[{"evidence_id": "req-evi"}])
        engine.set_node_result(first_work_id, {"artifact": "keep"}, evidence=[{"evidence_id": "work-evi"}])
        engine.set_node_result(first_verify["task_id"], {"verified": True}, evidence=[{"evidence_id": "verify-evi"}])

        changed = sample_input()
        changed["acceptance_conditions"].append("include a reproducibility appendix")
        result = engine.update_specification(changed)
        self.assertEqual(result["revision"], 2)
        self.assertTrue(result["change_set"]["added_node_ids"])
        preserved = next(node for node in result["nodes"] if node["task_id"] == first_verify["task_id"])
        self.assertEqual(preserved["result"], {"verified": True})
        self.assertEqual(preserved["status"], "completed")
        self.assertIn("task_final_review", result["change_set"]["recomputed_node_ids"])

    def test_missing_acceptance_conditions_blocks_execution_export(self) -> None:
        raw = sample_input()
        raw["acceptance_conditions"] = []
        engine = ToDAGEngine()
        result = engine.build(raw)
        self.assertEqual(result["status"], "needs_clarification")
        with self.assertRaises(RuntimeError):
            engine.execution_plan()

    def test_dependency_completion_order_is_guarded(self) -> None:
        engine = ToDAGEngine()
        result = engine.build(sample_input())
        work_id = nodes_by_type(result, "work")[0]["task_id"]
        with self.assertRaises(ValueError):
            engine.set_node_result(work_id, {"too_early": True})

    def test_invalid_cycle_update_is_atomic(self) -> None:
        engine = ToDAGEngine()
        before = engine.build(sample_input())
        requirement_id = before["entry_task_ids"][0]
        with self.assertRaises(ValueError):
            engine.update_node(requirement_id, {"depends_on": ["task_final_review"]})
        after = engine.snapshot()
        self.assertEqual(after["revision"], before["revision"])
        self.assertEqual(after["topological_order"], before["topological_order"])

    def test_input_contract_rejects_missing_fields(self) -> None:
        invalid = sample_input()
        del invalid["budget"]
        with self.assertRaises(ValueError):
            ToDAGEngine().build(invalid)

    def test_graph_queries_scale_to_1000_nodes(self) -> None:
        nodes: dict[str, DAGNode] = {}
        for i in range(1000):
            task_id = f"n{i:04d}"
            parent = [] if i == 0 else [f"n{i - 1:04d}"]
            nodes[task_id] = DAGNode(
                task_id=task_id,
                title=task_id,
                description=task_id,
                required_skill="analysis",
                agent_role="executor",
                depends_on=parent,
                priority=5,
                estimated_cost={"duration_s": 1},
            )
        order = topological_sort(nodes)
        self.assertEqual(len(order), 1000)
        self.assertEqual(len(descendants(nodes, "n0000")), 999)
        path = critical_path(nodes)
        self.assertEqual(len(path["task_ids"]), 1000)


if __name__ == "__main__":
    unittest.main()
