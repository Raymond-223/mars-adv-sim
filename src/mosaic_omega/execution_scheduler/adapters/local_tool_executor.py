"""Workspace-scoped subprocess/asyncio tool executor."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from ..config import Settings
from ..models import ExecutionResult, ToolCall


class LocalToolExecutor:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.workspace = settings.workspace.resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)

    def _path(self, raw: Any) -> Path:
        # Normalize Windows separators too so ``..\file`` cannot bypass
        # the workspace guard when the runtime itself is running on Linux.
        normalized = str(raw).replace("\\", "/")
        path = (self.workspace / normalized).resolve()
        if path != self.workspace and self.workspace not in path.parents:
            raise PermissionError("tool path escapes workspace")
        return path

    def _command(self, raw: Any) -> list[str]:
        if not isinstance(raw, list) or not raw or not all(isinstance(item, str) for item in raw):
            raise ValueError("command must be a non-empty string list")
        executable = Path(raw[0]).name.casefold()
        allowed = {item.casefold() for item in self.settings.allowed_commands}
        if executable not in allowed:
            raise PermissionError(f"command is not allowed: {executable}")
        return list(raw)

    def execute(self, call: ToolCall, timeout_s: float) -> ExecutionResult:
        started = time.time()
        try:
            if call.tool_name == "task":
                # A reasoning-only Agent result is persisted as a real deliverable.
                # Crucially, acceptance conditions are NEVER appended to the output;
                # the old behavior allowed a task to pass by echoing its own rubric.
                content = str(call.arguments["description"])
                deliverable = self.workspace / ".mosaic_deliverables" / call.run_id / f"{call.task_id}.md"
                deliverable.parent.mkdir(parents=True, exist_ok=True)
                deliverable.write_text(content, encoding="utf-8")
                relative = deliverable.relative_to(self.workspace).as_posix()
                metadata = {
                    "execution_semantics": "reasoning_output_persisted_as_deliverable",
                    "deliverable_relative": relative,
                    "deliverable_bytes": deliverable.stat().st_size,
                    "acceptance_conditions_injected": False,
                }
                if bool(call.arguments.get("test_fixture_verifier", False)):
                    metadata["test_fixture_verifier"] = True
                return ExecutionResult(
                    call.call_id, True, f"deliverable_written:{relative}",
                    started_at=started, finished_at=time.time(), metadata=metadata,
                )
            if call.tool_name == "read_file":
                output = self._path(call.arguments["path"]).read_text(encoding="utf-8")
                return ExecutionResult(call.call_id, True, output, started_at=started, finished_at=time.time())
            if call.tool_name == "write_file":
                path = self._path(call.arguments["path"])
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(str(call.arguments["content"]), encoding="utf-8")
                return ExecutionResult(call.call_id, True, f"wrote {path.name}", started_at=started, finished_at=time.time())
            if call.tool_name == "build":
                command = call.arguments.get("command", [sys.executable, "-m", "compileall", "-q", "."])
            elif call.tool_name == "test":
                command = call.arguments.get("command", [sys.executable, "-m", "unittest", "discover", "-v"])
            else:
                command = call.arguments["command"]
            command = self._command(command)
            completed = subprocess.run(
                command, cwd=self.workspace, capture_output=True, text=True,
                timeout=timeout_s, shell=False, check=False,
            )
            output = (completed.stdout or "") + (completed.stderr or "")
            return ExecutionResult(
                call.call_id, completed.returncode == 0, output,
                None if completed.returncode == 0 else f"exit code {completed.returncode}",
                completed.returncode, started, time.time(),
            )
        except Exception as exc:
            return ExecutionResult(call.call_id, False, error=f"{type(exc).__name__}: {exc}",
                                   started_at=started, finished_at=time.time())

    async def execute_async(self, call: ToolCall, timeout_s: float) -> ExecutionResult:
        if call.tool_name not in {"shell", "build", "test"}:
            return await asyncio.to_thread(self.execute, call, timeout_s)
        command = call.arguments.get("command")
        if call.tool_name == "build" and command is None:
            command = [sys.executable, "-m", "compileall", "-q", "."]
        if call.tool_name == "test" and command is None:
            command = [sys.executable, "-m", "unittest", "discover", "-v"]
        started = time.time()
        try:
            command = self._command(command)
            process = await asyncio.create_subprocess_exec(
                *command, cwd=self.workspace,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_s)
            output = stdout.decode(errors="replace") + stderr.decode(errors="replace")
            return ExecutionResult(call.call_id, process.returncode == 0, output,
                                   None if process.returncode == 0 else f"exit code {process.returncode}",
                                   process.returncode, started, time.time())
        except Exception as exc:
            return ExecutionResult(call.call_id, False, error=f"{type(exc).__name__}: {exc}",
                                   started_at=started, finished_at=time.time())
