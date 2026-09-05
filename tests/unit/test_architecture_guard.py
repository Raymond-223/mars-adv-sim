from __future__ import annotations

import ast
from pathlib import Path


SRC = Path("src/mosaic_omega")


def _class_locations() -> dict[str, list[Path]]:
    locations: dict[str, list[Path]] = {}
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                locations.setdefault(node.name, []).append(path)
    return locations


def test_single_authority_classes_have_one_definition() -> None:
    locations = _class_locations()
    for name in {
        "Scheduler",
        "EventStore",
        "ToolRuntime",
        "VerifierService",
        "RecoveryEngine",
        "MemoryService",
        "MosaicMainChain",
    }:
        assert len(locations.get(name, [])) == 1, (name, locations.get(name, []))


def test_removed_duplicate_runtime_paths_do_not_return() -> None:
    forbidden = [
        SRC / "scheduler",
        SRC / "goal_planner",
        SRC / "runtime",
        SRC / "storage.py",
        SRC / "integration" / "pipeline.py",
        SRC / "integration" / "planner_runtime_bridge.py",
        SRC / "runtime" / "coordinator.py",
        SRC / "runtime" / "task_store.py",
    ]
    assert all(not path.exists() for path in forbidden), forbidden



def test_main_modules_are_top_level_packages() -> None:
    required = {
        "goalspec",
        "todag",
        "execution_scheduler",
        "message_topology",
        "memory_recovery",
        "agent_runtime",
        "verifier",
        "recovery",
        "observability",
        "integration",
        "console_api",
        "schemas",
        "storage",
    }
    assert required <= {path.name for path in SRC.iterdir() if path.is_dir()}


def test_scenarios_do_not_define_runtime_authorities() -> None:
    forbidden_names = {"Orchestrator", "Scheduler", "EventStore", "VerifierService", "RecoveryEngine"}
    for path in Path("scenarios").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        declared = {node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)}
        assert not (declared & forbidden_names), (path, declared & forbidden_names)


def test_console_has_no_runtime_mutation_imports() -> None:
    """The operator console consumes snapshots only; it never owns runtime state."""
    forbidden_modules = {
        "mosaic_omega.execution_scheduler.event_store",
        "mosaic_omega.execution_scheduler.scheduler",
        "mosaic_omega.execution_scheduler.tool_runtime",
        "mosaic_omega.recovery.engine",
        "mosaic_omega.verifier.service",
    }
    roots = [Path("src/mosaic_omega/console_api"), Path("apps/console/backend")]
    for root in roots:
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            imported: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module)
            assert not (imported & forbidden_modules), (path, imported & forbidden_modules)
