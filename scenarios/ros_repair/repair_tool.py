from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Sequence


def artifact_dir(repo: Path) -> Path:
    path = repo / "artifacts"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _run(
    command: Sequence[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def package_root(repo: Path) -> Path:
    return repo / "src" / "demo_ros_pkg"


def package_python_dir(repo: Path) -> Path:
    return package_root(repo) / "demo_ros_pkg"


def controller_path(repo: Path) -> Path:
    return package_python_dir(repo) / "controller.py"


def _pytest_env(repo: Path) -> dict[str, str]:
    env = dict(os.environ)
    package_dir = str(package_root(repo))
    previous = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = package_dir if not previous else package_dir + os.pathsep + previous
    # The fixture test must be isolated from globally installed pytest plugins.
    # Otherwise third-party shutdown hooks can make a completed ROS-repair run
    # appear to hang after pytest has already reported PASS.
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    env["PYTEST_ADDOPTS"] = "-p no:xonsh"
    return env


def inventory(repo: Path) -> int:
    files = sorted(str(path.relative_to(repo)) for path in repo.rglob("*") if path.is_file())
    package_xml = package_root(repo) / "package.xml"
    result = {
        "files": files,
        "ros_packages": ["demo_ros_pkg"] if package_xml.is_file() else [],
        "package_xml": str(package_xml.relative_to(repo)) if package_xml.is_file() else None,
    }
    out = artifact_dir(repo) / "inventory.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"inventory written: {out.relative_to(repo)} ({len(files)} files)")
    return 0 if package_xml.is_file() else 2


def run_tests(repo: Path) -> subprocess.CompletedProcess[str]:
    return _run(
        [sys.executable, "-m", "pytest", "-q", "src/demo_ros_pkg/test"],
        cwd=repo,
        env=_pytest_env(repo),
    )


def diagnose(repo: Path) -> int:
    result = run_tests(repo)
    output = (result.stdout or "") + (result.stderr or "")
    source = controller_path(repo).read_text(encoding="utf-8")
    candidate = None
    if result.returncode != 0 and "return left - right" in source:
        candidate = {
            "file": "src/demo_ros_pkg/demo_ros_pkg/controller.py",
            "root_cause": "wheel command aggregation subtracts right from left",
            "proposed_change": "replace `return left - right` with `return left + right`",
        }
    report = {
        "test_command": [sys.executable, "-m", "pytest", "-q", "src/demo_ros_pkg/test"],
        "test_returncode": result.returncode,
        "test_output": output,
        "candidate": candidate,
    }
    out = artifact_dir(repo) / "diagnosis.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"diagnosis written: {out.relative_to(repo)}; failing={result.returncode != 0}; candidate={bool(candidate)}")
    return 0 if candidate else 2


def patch(repo: Path) -> int:
    diagnosis_path = artifact_dir(repo) / "diagnosis.json"
    if not diagnosis_path.is_file():
        print("diagnosis missing", file=sys.stderr)
        return 2
    diagnosis = json.loads(diagnosis_path.read_text(encoding="utf-8"))
    candidate = diagnosis.get("candidate")
    if not candidate:
        print("no repair candidate", file=sys.stderr)
        return 3
    path = repo / candidate["file"]
    source = path.read_text(encoding="utf-8")
    old = "return left - right"
    new = "return left + right"
    if old not in source:
        print("expected defect not found", file=sys.stderr)
        return 4
    path.write_text(source.replace(old, new, 1), encoding="utf-8")
    # The patch keeps the same file size, so timestamp-based bytecode
    # invalidation may retain stale code when both writes occur in one second.
    shutil.rmtree(path.parent / "__pycache__", ignore_errors=True)
    patch_log = artifact_dir(repo) / "patch.json"
    patch_log.write_text(
        json.dumps({"file": candidate["file"], "old": old, "new": new}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"patch applied: {candidate['file']}")
    return 0


def build(repo: Path) -> int:
    """Build the ROS workspace.

    Real ROS 2 environments use colcon.  Generic CI can use an explicitly
    recorded compile-only fallback unless MOSAIC_ROS_REQUIRE_COLCON=1 is set.
    The fallback is never labelled as a successful colcon build.
    """
    colcon = shutil.which("colcon")
    strict = os.getenv("MOSAIC_ROS_REQUIRE_COLCON", "0").strip().lower() in {"1", "true", "yes", "on"}
    if colcon:
        command = [colcon, "build", "--packages-select", "demo_ros_pkg", "--event-handlers", "console_direct+"]
        mode = "colcon"
        result = _run(command, cwd=repo)
    elif strict:
        command = ["colcon", "build", "--packages-select", "demo_ros_pkg"]
        mode = "colcon_required_unavailable"
        result = subprocess.CompletedProcess(command, 127, "", "colcon is required but unavailable")
    else:
        command = [sys.executable, "-m", "compileall", "-q", str(package_python_dir(repo))]
        mode = "compileall_ci_fallback"
        result = _run(command, cwd=repo)

    record = {
        "mode": mode,
        "command": list(command),
        "returncode": result.returncode,
        "stdout": result.stdout or "",
        "stderr": result.stderr or "",
        "colcon_available": bool(colcon),
        "strict_colcon": strict,
    }
    out = artifact_dir(repo) / "build.json"
    out.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"build mode={mode}; returncode={result.returncode}; evidence={out.relative_to(repo)}")
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    return result.returncode


def test(repo: Path) -> int:
    result = run_tests(repo)
    output = (result.stdout or "") + (result.stderr or "")
    record = {
        "command": [sys.executable, "-m", "pytest", "-q", "src/demo_ros_pkg/test"],
        "returncode": result.returncode,
        "output": output,
    }
    (artifact_dir(repo) / "test.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(output, end="")
    return result.returncode


def report(repo: Path) -> int:
    result = run_tests(repo)
    diagnosis = json.loads((artifact_dir(repo) / "diagnosis.json").read_text(encoding="utf-8"))
    patch_info = json.loads((artifact_dir(repo) / "patch.json").read_text(encoding="utf-8"))
    build_info = json.loads((artifact_dir(repo) / "build.json").read_text(encoding="utf-8"))
    report_path = artifact_dir(repo) / "repair_report.md"
    report_path.write_text(
        "# ROS Repair Report\n\n"
        f"Root cause: {diagnosis['candidate']['root_cause']}\n\n"
        f"Patch: `{patch_info['old']}` → `{patch_info['new']}` in `{patch_info['file']}`.\n\n"
        f"Build mode: `{build_info['mode']}`; return code: `{build_info['returncode']}`.\n\n"
        f"Final pytest: {'PASS' if result.returncode == 0 else 'FAIL'}\n\n"
        "Reproduce: `python repair_tool.py inventory ros_repo`, then `diagnose`, `patch`, `build`, `test`, `report`.\n",
        encoding="utf-8",
    )
    print(f"repair report written: {report_path.relative_to(repo)}; tests={'PASS' if result.returncode == 0 else 'FAIL'}")
    return result.returncode


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["inventory", "diagnose", "patch", "build", "test", "report"])
    parser.add_argument("repo")
    args = parser.parse_args()
    repo = Path(args.repo).resolve()
    return globals()[args.action](repo)


if __name__ == "__main__":
    raise SystemExit(main())
