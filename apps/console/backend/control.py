"""Write-side competition control plane for the MOSAIC-Ω console.

Every mutation is an explicit subprocess-backed experiment. The control plane
never fabricates runtime state: progress/status derives from the child process,
while task/agent/topology data continues to come from authoritative snapshots.
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from urllib.parse import urlsplit
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from mosaic_omega.integration.live_faults import LiveFaultMailbox
from .settings import AgentSettingsStore, ExecutionEndpointStore, ProviderSettingsStore


@dataclass
class Job:
    job_id: str
    kind: str
    label: str
    run_id: str | None
    status: str
    created_at: float
    started_at: float | None = None
    finished_at: float | None = None
    pid: int | None = None
    return_code: int | None = None
    log_path: str | None = None
    output_path: str | None = None
    command: list[str] | None = None
    error: str | None = None


class CompetitionControlPlane:
    def __init__(self, project_root: Path, workspace: Path) -> None:
        self.project_root = project_root.resolve()
        self.workspace = workspace.resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.control_dir = self.workspace / "control"
        self.control_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir = self.control_dir / "logs"
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.results_dir = self.control_dir / "results"
        self.results_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._jobs: dict[str, Job] = {}
        self._processes: dict[str, subprocess.Popen[str]] = {}
        self.injections = LiveFaultMailbox(self.workspace)
        self.provider_settings = ProviderSettingsStore(self.workspace)
        self.endpoint_settings = ExecutionEndpointStore(self.workspace)
        self.agent_settings = AgentSettingsStore(self.workspace)
        self._load_history()

    def _history_path(self) -> Path:
        return self.control_dir / "jobs.json"

    def _load_history(self) -> None:
        path = self._history_path()
        if not path.is_file():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return
        for item in raw if isinstance(raw, list) else []:
            try:
                job = Job(**item)
            except TypeError:
                continue
            if job.status in {"QUEUED", "RUNNING", "STOPPING"}:
                job.status = "INTERRUPTED"
                job.finished_at = time.time()
            self._jobs[job.job_id] = job

    def _save(self) -> None:
        tmp = self._history_path().with_suffix(".tmp")
        data = [asdict(j) for j in sorted(self._jobs.values(), key=lambda x: x.created_at)]
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self._history_path())

    @staticmethod
    def _safe_id(prefix: str) -> str:
        return f"{prefix}-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"

    def environment(self) -> dict[str, Any]:
        provider = self.provider_settings.public()["active"]
        host = str(provider.get("endpoint_host") or "")
        return {
            "provider_id": provider.get("provider_id"),
            "provider_name": provider.get("provider_name"),
            "api_key_present": bool(provider.get("api_key_present")),
            "api_key_required": bool(provider.get("api_key_required")),
            "model": provider.get("model"),
            "endpoint_host": host,
            "provider_configured": bool(provider.get("configured")),
            "deepseek_official_endpoint": provider.get("provider_id") == "deepseek" and host == "api.deepseek.com",
            "ortools_importable": self._importable("ortools"),
            "secret_storage": provider.get("secret_storage"),
            "execution_endpoints": self.endpoint_settings.summary(),
        }

    @staticmethod
    def _public_job(job: Job | dict[str, Any] | None) -> dict[str, Any] | None:
        if job is None:
            return None
        raw = asdict(job) if isinstance(job, Job) else dict(job)
        return {
            "job_id": raw.get("job_id"),
            "kind": raw.get("kind"),
            "label": raw.get("label"),
            "run_id": raw.get("run_id"),
            "status": raw.get("status"),
            "created_at": raw.get("created_at"),
            "started_at": raw.get("started_at"),
            "finished_at": raw.get("finished_at"),
            "return_code": raw.get("return_code"),
            "error": raw.get("error"),
            "has_log": bool(raw.get("log_path")),
            "has_output": bool(raw.get("output_path")),
        }

    def redact_public_payload(self, value: Any) -> Any:
        """Remove machine-local addresses/paths from non-Settings DTOs.

        This function is intentionally NOT applied to explicit Settings endpoints,
        where the user must be able to edit a local Ollama or MQTT address.
        """
        if isinstance(value, dict):
            return {str(k): self.redact_public_payload(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.redact_public_payload(v) for v in value]
        if isinstance(value, tuple):
            return [self.redact_public_payload(v) for v in value]
        if not isinstance(value, str):
            return value
        text = value
        project = str(self.project_root)
        workspace = str(self.workspace)
        for prefix, label in ((workspace, "workspace://"), (project, "project://")):
            if prefix:
                text = text.replace(prefix, label.rstrip("/"))
                text = text.replace(prefix.replace("\\", "/"), label.rstrip("/"))
        if text.casefold().startswith("file://"):
            parsed = urlsplit(text)
            name = Path(parsed.path).name
            return f"artifact://{name or 'redacted'}"

        # Replace loopback host fragments in-place instead of destroying the rest
        # of a log/message string.
        text = re.sub(r"(?i)(?:https?://)?(?:127\.0\.0\.1|localhost|0\.0\.0\.0|\[?::1\]?)(?::\d+)?",
                      "local-service://redacted", text)

        # Windows absolute paths, including paths with spaces. Quoted paths are
        # handled first; unquoted paths stop at common log delimiters.
        text = re.sub(r'(?i)(["\'])?[A-Z]:\\[^\r\n"\'<>|]+\1?', '<local-path>', text)
        # POSIX machine-local roots used by user profiles, temp/build/runtime data.
        # macOS resolves its per-user temp root to /var/folders/... and, once
        # symlinks are resolved, /private/var/folders/..., so both spellings are
        # listed explicitly. A bare "/private" segment stays intact on purpose:
        # it is a legitimate URL path, not a machine path.
        text = re.sub(
            r'(?<![A-Za-z0-9])/(?:home|tmp|var/tmp|var/folders|Users|mnt|opt|srv'
            r'|private/tmp|private/var|usr/local)/(?:[^\s"\'<>]+(?:\s+(?=[^,;|]\S)[^\s"\'<>]+)*)',
            '<local-path>', text, flags=re.IGNORECASE,
        )
        return text

    @staticmethod
    def _importable(name: str) -> bool:
        try:
            __import__(name)
            return True
        except Exception:
            return False

    def scenarios(self) -> list[dict[str, Any]]:
        return [
            {
                "id": "ros_repair",
                "name": "软件工程：ROS 仓库自主修复",
                "description": "诊断故障、生成修复、构建回归并形成可验证交付。",
                "template_goal": "分析指定 ROS2 软件仓库中的故障，定位根因并制定修复方案；在满足安全与可回滚约束的前提下执行必要修改、构建与回归测试，最终输出修复结果、验证证据、风险说明和回滚建议。",
                "requirements": ["定位可复现根因", "生成并执行修复方案", "完成构建与回归测试", "每个关键结论绑定 Evidence", "交付风险说明与回滚方案"],
                "deliverables": ["根因诊断", "修复补丁/变更说明", "构建与测试证据", "最终修复报告"],
                "real_api": True,
                "cross_domain": "software_engineering",
            },
            {
                "id": "financial_research",
                "name": "跨域研究：端边云金融分析",
                "description": "隐私约束下完成数据处理、风险建模、合规审核与研究交付。",
                "template_goal": "在数据敏感性和实时性约束下，对给定多源金融信息完成端侧安全处理、边缘预处理与云端深度分析；形成风险模型、合规审核和投资研究结论，并为关键判断提供可追溯证据。",
                "requirements": ["敏感数据不得越权外发", "动态选择 DEVICE/EDGE/CLOUD 执行位置", "完成风险与合规双重审核", "关键结论必须可追溯", "输出结构化研究报告"],
                "deliverables": ["数据处理记录", "风险模型结果", "合规审核结果", "投资研究报告"],
                "real_api": True,
                "cross_domain": "research_and_risk",
            },
        ]

    def experiments(self) -> dict[str, list[dict[str, Any]]]:
        return {
            "faults": [
                {"id": "agent_offline", "name": "Agent 下线", "meaning": "Registry offline → next scheduler round excludes offline Agent → alternate registered Agent"},
                {"id": "tool_failure", "name": "工具失败", "meaning": "Arm next ToolRuntime call failure → normal Orchestrator failure path → RecoveryEngine retry → re-execution"},
                {"id": "requirement_change", "name": "需求变更", "meaning": "GoalSpec 重新编译 → 新 DAG → autonomous execution"},
                {"id": "evidence_invalidation", "name": "证据失效", "meaning": "Evidence invalidation → impact closure → local replan → re-execution"},
            ],
            "benchmarks": [
                {"id": "long_horizon", "name": "1000+ Event 单 Run 长程 Benchmark", "truth": "one authoritative EventStore run; deterministic executor; events are not LLM calls"},
                {"id": "topology_replay", "name": "动态拓扑低熵对照", "truth": "Sparse runtime messages measured; Full Mesh/Star are replay-derived fan-out costs"},
                {"id": "scheduler_ablation", "name": "调度策略受控对照", "truth": "Same Task set + same Capability Registry + same CostModel; Round-Robin / Greedy / OR-Tools are compared; missing OR-Tools is explicitly unavailable, never relabeled fallback"},
                {"id": "memory_ablation", "name": "长程记忆受控对照", "truth": "Same final task + same history; No Memory / Full History / MOSAIC ContextPack; token values are explicit estimates unless a provider reports real usage"},
                {"id": "split_inference_reference", "name": "真实 Pipeline Split Inference", "truth": "reference MLP layers execute across two Python processes; stage latency and activation bytes are measured; this is not claimed as LLM layer split"},
            ],
        }

    def traceability(self) -> dict[str, Any]:
        return {
            "fields": [
                {"ui": "API Key Configured", "api": "/api/control/status", "field": "environment.api_key_present", "source": "ProviderSettingsStore secret presence", "calculation": "Boolean presence only; secret value is never returned"},
                {"ui": "OR-Tools", "api": "/api/control/status", "field": "environment.ortools_importable", "source": "Python import ortools", "calculation": "True iff import succeeds"},
                {"ui": "Job Return Code", "api": "/api/control/status", "field": "jobs[].return_code", "source": "subprocess.Popen.poll()", "calculation": "Native child-process return code"},
                {"ui": "Artifacts", "api": "/api/control/artifacts", "field": "array.length", "source": "filesystem enumeration of control/results + .mosaic_evidence", "calculation": "count(files)"},
                {"ui": "JSON Results", "api": "/api/control/artifacts", "field": "[].name", "source": "artifact filename", "calculation": "count(name endsWith .json)"},
                {"ui": "Evidence Files", "api": "/api/control/artifacts", "field": "[].category", "source": "server-side artifact classification", "calculation": "count(category == evidence)"},
                {"ui": "Artifact Size KB", "api": "/api/control/artifacts", "field": "[].bytes", "source": "Path.stat().st_size", "calculation": "bytes / 1024"},
                {"ui": "Live Injection Count", "api": "/api/control/status", "field": "injections.length", "source": "LiveFaultMailbox.status() file enumeration", "calculation": "count(pending/applied/failed/rejected request records)"},
            ],
            "states": [
                {"family": "ControlJob", "state": "QUEUED", "entered_when": "Job record is persisted before subprocess creation", "source": "CompetitionControlPlane._start"},
                {"family": "ControlJob", "state": "RUNNING", "entered_when": "subprocess.Popen returns successfully", "source": "CompetitionControlPlane._start"},
                {"family": "ControlJob", "state": "SUCCEEDED", "entered_when": "subprocess.poll() returns 0", "source": "CompetitionControlPlane._refresh_processes"},
                {"family": "ControlJob", "state": "FAILED", "entered_when": "subprocess.poll() returns non-zero and job was not operator-stopped", "source": "CompetitionControlPlane._refresh_processes"},
                {"family": "ControlJob", "state": "FAILED_TO_START", "entered_when": "subprocess.Popen raises before a PID is assigned", "source": "CompetitionControlPlane._start"},
                {"family": "ControlJob", "state": "STOPPING", "entered_when": "operator requests /api/control/stop before terminate()", "source": "CompetitionControlPlane.stop"},
                {"family": "ControlJob", "state": "STOPPED", "entered_when": "terminated child process exits after STOPPING", "source": "CompetitionControlPlane._refresh_processes"},
                {"family": "ControlJob", "state": "INTERRUPTED", "entered_when": "server restarts while persisted job was QUEUED/RUNNING/STOPPING", "source": "CompetitionControlPlane._load_history"},
                {"family": "LiveInjection", "state": "PENDING", "entered_when": "Console atomically writes a .pending.json request for the active run", "source": "LiveFaultMailbox.enqueue.requested_at"},
                {"family": "LiveInjection", "state": "APPLIED", "entered_when": "running MainChain round hook consumes the request and applies the authoritative injection; any subsequent recovery is evidenced separately by EventStore", "source": "LiveFaultMailbox.acknowledge.finished_at"},
                {"family": "LiveInjection", "state": "FAILED", "entered_when": "round hook consumes the request but authoritative fault application raises", "source": "LiveFaultMailbox.acknowledge.finished_at"},
                {"family": "LiveInjection", "state": "REJECTED", "entered_when": "control plane/runtime rejects an unsupported injection before claiming it was applied", "source": "LiveFaultMailbox acknowledgement/control response"},
            ],
            "actions": [
                {"ui": "开始真实任务", "endpoint": "POST /api/control/start-custom", "effect": "Popen scripts/demo_main_chain.py with provider-backed GoalSpec/Agent mode, scheduler-policy=ortools, shared workspace"},
                {"ui": "停止当前任务", "endpoint": "POST /api/control/stop", "effect": "terminate the active child process; job status changes from actual process lifecycle"},
                {"ui": "启动场景", "endpoint": "POST /api/control/start-scenario", "effect": "Popen scripts/run_competition_scenario.py with the currently configured provider-backed Agent mode"},
                {"ui": "注入并验证恢复", "endpoint": "POST /api/control/start-fault", "effect": "If a live custom/scenario run exists, enqueue an atomic mailbox request consumed by that run at the next round boundary; if no run exists, Popen a standalone reproducible fault experiment. UI never mutates task state directly."},
                {"ui": "开始实验", "endpoint": "POST /api/control/start-benchmark", "effect": "Popen measured benchmark/replay script; measurement mode is retained in result JSON"},
                {"ui": "远程节点联合验收", "endpoint": "POST /api/control/start-remote-acceptance", "effect": "Popen authoritative MQTT acceptance: configured remote Agent -> OR-Tools assignment -> ToolRuntime -> Evidence/Verifier; does not claim model-layer split inference"},
                {"ui": "刷新列表", "endpoint": "GET /api/control/artifacts", "effect": "re-enumerate files; no runtime mutation"},
            ],
            "animation": [
                {"ui": "message particle", "trigger": "new communication.items[].message_id with policy_action=SEND", "meaning": "one newly observed delivered/sent message traverses an actual topology edge; animation expires and is never looped as fake traffic"},
                {"ui": "task status flash", "trigger": "authoritative task status differs from previous snapshot", "meaning": "a real task-state transition was observed"},
                {"ui": "memory transfer", "trigger": "context_pack_count increases", "meaning": "a newly observed ContextPack was created"},
                {"ui": "waiting pulse", "trigger": "snapshot unavailable while frontend is waiting for runtime state", "meaning": "frontend has not received an authoritative snapshot yet; SSE is preferred and a light timed refresh is only fallback", "does_not_mean": ["Agent running", "LLM inference", "network activity"]},
            ],
        }

    def jobs(self) -> list[dict[str, Any]]:
        self._refresh_processes()
        with self._lock:
            return [self._public_job(j) for j in sorted(self._jobs.values(), key=lambda x: x.created_at, reverse=True)]

    def experiment_results(self, limit: int = 12) -> list[dict[str, Any]]:
        """Return parsed completed experiment outputs without exposing local paths.

        Benchmark/fault jobs are control-plane experiments, not authoritative Task
        Runs. Their JSON result is safe to surface only after the subprocess has
        succeeded and the output file exists.
        """
        self._refresh_processes()
        rows: list[dict[str, Any]] = []
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda x: x.created_at, reverse=True)
        for job in jobs:
            if job.kind not in {"benchmark", "fault"} or job.status != "SUCCEEDED" or not job.output_path:
                continue
            path = Path(job.output_path)
            if not path.is_file() or path.stat().st_size > 5_000_000:
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                continue
            if not isinstance(data, dict):
                continue
            rows.append({
                "job_id": job.job_id,
                "kind": job.kind,
                "label": job.label,
                "run_id": job.run_id,
                "finished_at": job.finished_at,
                "data": data,
            })
            if len(rows) >= max(1, int(limit)):
                break
        return rows

    def release_benchmarks(self) -> dict[str, dict[str, Any]]:
        """Load current-release controlled benchmark evidence.

        These files are immutable packaged evidence for the current release, not
        the selected Task Run and not live control jobs.  The DTO exposes only a
        logical source filename plus parsed JSON, never an absolute path.
        """
        names = {
            "long_horizon": "benchmark_1000_events_v3_monolithic.json",
            "topology_replay": "topology_ablation_v1.9.0.json",
            "scheduler_ablation": "scheduler_ablation_v1.9.0.json",
            "memory_ablation": "memory_ablation_v1.9.0.json",
            "split_inference_reference": "split_inference_reference_v1.9.0.json",
        }
        root = self.project_root / "experiments" / "results"
        out: dict[str, dict[str, Any]] = {}
        for key, name in names.items():
            path = root / name
            if not path.is_file() or path.stat().st_size > 5_000_000:
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                continue
            if isinstance(data, dict):
                out[key] = {"source": f"experiments/results/{name}", "data": data}
        return out

    def status(self) -> dict[str, Any]:
        jobs = self.jobs()
        active = next((j for j in jobs if j["status"] in {"QUEUED", "RUNNING", "STOPPING"}), None)
        task_kinds = {"custom_task", "scenario", "remote_endpoint_acceptance"}
        experiment_kinds = {"benchmark", "fault"}
        return {
            "environment": self.environment(),
            "active_job": active,
            "jobs": jobs[:30],
            "task_jobs": [j for j in jobs if j.get("kind") in task_kinds][:30],
            "experiment_jobs": [j for j in jobs if j.get("kind") in experiment_kinds][:30],
            "experiment_results": self.experiment_results(),
            "release_benchmarks": self.release_benchmarks(),
            "scenarios": self.scenarios(),
            "experiments": self.experiments(),
            "injections": self.injections.status()[:50],
            "traceability": self.traceability(),
        }

    def _refresh_processes(self) -> None:
        with self._lock:
            for job_id, process in list(self._processes.items()):
                rc = process.poll()
                if rc is None:
                    continue
                job = self._jobs[job_id]
                job.return_code = rc
                job.finished_at = time.time()
                job.status = "SUCCEEDED" if rc == 0 else ("STOPPED" if rc in {-15, 1} and job.status == "STOPPING" else "FAILED")
                self._processes.pop(job_id, None)
            self._save()

    def _child_env(self) -> dict[str, str]:
        env = dict(os.environ)
        roots = [str(self.project_root / "src"), str(self.project_root)]
        if env.get("PYTHONPATH"):
            roots.append(env["PYTHONPATH"])
        env["PYTHONPATH"] = os.pathsep.join(roots)
        env["PYTHONUTF8"] = "1"
        env.update(self.provider_settings.runtime_environment())
        env["MOSAIC_AGENT_CONFIG_PATH"] = str(self.agent_settings.path)
        env.setdefault("MOSAIC_PERSISTENCE", "sqlite")
        return env

    def _start(self, *, kind: str, label: str, run_id: str | None, command: list[str], output_path: Path | None, extra_env: dict[str, str] | None = None) -> dict[str, Any]:
        self._refresh_processes()
        with self._lock:
            running = [j for j in self._jobs.values() if j.status in {"QUEUED", "RUNNING", "STOPPING"}]
            if running:
                raise RuntimeError(f"已有任务正在运行：{running[-1].job_id}")
            job_id = self._safe_id("job")
            log_path = self.logs_dir / f"{job_id}.log"
            job = Job(
                job_id=job_id,
                kind=kind,
                label=label,
                run_id=run_id,
                status="QUEUED",
                created_at=time.time(),
                log_path=str(log_path),
                output_path=str(output_path) if output_path else None,
                command=command,
            )
            self._jobs[job_id] = job
            self._save()
            log = log_path.open("w", encoding="utf-8", buffering=1)
            try:
                child_env = self._child_env()
                if extra_env:
                    child_env.update(extra_env)
                process = subprocess.Popen(
                    command,
                    cwd=self.project_root,
                    env=child_env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    shell=False,
                )
            except Exception as exc:
                log.close()
                job.status = "FAILED_TO_START"
                job.error = f"{type(exc).__name__}: {exc}"
                job.finished_at = time.time()
                self._save()
                raise
            job.status = "RUNNING"
            job.started_at = time.time()
            job.pid = process.pid
            self._processes[job_id] = process
            self._save()
            return self._public_job(job)

    def _require_strict_env(self) -> None:
        env = self.environment()
        missing = []
        if not env["provider_configured"]:
            missing.append("AI model provider configuration")
        if not env["ortools_importable"]:
            missing.append("ortools")
        if missing:
            raise RuntimeError("真实运行被阻止，缺少：" + ", ".join(missing))

    def start_custom(self, goal: str) -> dict[str, Any]:
        self._require_strict_env()
        goal = str(goal or "").strip()
        if len(goal) < 8:
            raise ValueError("任务目标至少需要 8 个字符")
        run_id = self._safe_id("interactive")
        output = self.results_dir / f"{run_id}.json"
        command = [
            sys.executable, "-X", "utf8", "scripts/demo_main_chain.py", goal,
            "--run-id", run_id,
            "--workspace", str(self.workspace),
            "--output", str(output),
            "--goalspec-mode", "deepseek",
            "--agent-mode", "deepseek",
            "--scheduler-policy", "ortools",
            "--no-clean",
            "--live-control",
        ]
        return self._start(kind="custom_task", label="自定义复杂任务", run_id=run_id, command=command, output_path=output)

    def start_scenario(self, scenario: str) -> dict[str, Any]:
        self._require_strict_env()
        scenario = str(scenario)
        valid = {x["id"] for x in self.scenarios()}
        if scenario not in valid:
            raise ValueError(f"未知场景：{scenario}")
        run_id = self._safe_id(scenario)
        output = self.results_dir / f"{run_id}.json"
        command = [
            sys.executable, "-X", "utf8", "scripts/run_competition_scenario.py",
            "--scenario", scenario,
            "--workspace", str(self.workspace),
            "--run-id", run_id,
            "--output", str(output),
            "--agent-mode", "deepseek",
            "--live-control",
        ]
        label = next(x["name"] for x in self.scenarios() if x["id"] == scenario)
        return self._start(kind="scenario", label=label, run_id=run_id, command=command, output_path=output)

    def start_fault(self, fault: str, requirement: str | None = None) -> dict[str, Any]:
        self._require_strict_env()
        valid = {x["id"] for x in self.experiments()["faults"]}
        if fault not in valid:
            raise ValueError(f"未知故障类型：{fault}")

        self._refresh_processes()
        with self._lock:
            active = next((
                job for job in sorted(self._jobs.values(), key=lambda x: x.created_at, reverse=True)
                if job.status == "RUNNING"
            ), None)

        if active is not None and active.kind in {"custom_task", "scenario"}:
            if not active.run_id:
                raise RuntimeError("活动任务缺少 run_id，拒绝伪造实时故障注入")
            # A scenario runner cannot currently recompile an arbitrary changed GoalSpec
            # in place. Reject that combination instead of pretending it happened.
            if fault == "requirement_change" and active.kind == "scenario":
                raise RuntimeError("预置场景运行中暂不支持需求变更重编译；请在自定义任务运行中注入，或停止后启动独立需求变更实验")
            request = self.injections.enqueue(
                active.run_id, fault, requirement=requirement, requested_by="competition-console"
            )
            return {
                "mode": "LIVE_INJECTION_QUEUED",
                "job_id": active.job_id,
                "run_id": active.run_id,
                "request": request,
                "state_source": "LiveFaultMailbox.enqueue",
                "applied_when": "running MainChain consumes the request at the next round boundary",
            }

        # With no live custom/scenario run, execute the dedicated reproducible
        # fault experiment as its own authoritative subprocess.
        run_id = self._safe_id(f"fault-{fault}")
        output = self.results_dir / f"{run_id}.json"
        experiment_workspace = self.control_dir / "experiments" / run_id
        experiment_workspace.mkdir(parents=True, exist_ok=True)
        command = [
            sys.executable, "-X", "utf8", "scripts/run_fault_experiment.py",
            "--fault", fault,
            "--workspace", str(experiment_workspace),
            "--run-id", run_id,
            "--output", str(output),
        ]
        if requirement:
            command.extend(["--requirement", str(requirement)])
        return self._start(kind="fault", label=f"故障注入：{fault}", run_id=run_id, command=command, output_path=output)

    def start_remote_acceptance(self, endpoint_id: str) -> dict[str, Any]:
        """Run a real MQTT remote-Agent assignment through the authoritative chain."""
        if not self._importable("ortools"):
            raise RuntimeError("远程联合验收需要 OR-Tools")
        endpoint_env = self.endpoint_settings.runtime_environment(endpoint_id)
        run_id = self._safe_id("remote-endpoint")
        output = self.results_dir / f"{run_id}.json"
        command = [
            sys.executable, "-X", "utf8", "scripts/mqtt_remote_acceptance.py",
            "--workspace", str(self.workspace),
            "--run-id", run_id,
            "--output", str(output),
        ]
        endpoint = self.endpoint_settings.get(endpoint_id)
        return self._start(
            kind="remote_endpoint_acceptance",
            label=f"远程节点联合验收：{endpoint.get('name') or endpoint_id}",
            run_id=run_id,
            command=command,
            output_path=output,
            extra_env=endpoint_env,
        )

    def start_benchmark(self, benchmark: str) -> dict[str, Any]:
        valid = {x["id"] for x in self.experiments()["benchmarks"]}
        if benchmark not in valid:
            raise ValueError(f"未知 Benchmark：{benchmark}")
        run_id = self._safe_id(f"bench-{benchmark}")
        output = self.results_dir / f"{run_id}.json"
        experiment_workspace = self.control_dir / "experiments" / run_id
        experiment_workspace.mkdir(parents=True, exist_ok=True)
        command = [
            sys.executable, "-X", "utf8", "scripts/run_competition_benchmark.py",
            "--benchmark", benchmark,
            "--workspace", str(experiment_workspace),
            "--output", str(output),
        ]
        return self._start(kind="benchmark", label=f"Benchmark：{benchmark}", run_id=run_id, command=command, output_path=output)

    def stop(self, job_id: str | None = None) -> dict[str, Any]:
        self._refresh_processes()
        with self._lock:
            if job_id is None:
                job = next((j for j in sorted(self._jobs.values(), key=lambda x: x.created_at, reverse=True) if j.status == "RUNNING"), None)
            else:
                job = self._jobs.get(job_id)
            if job is None:
                raise KeyError("没有可停止的运行任务")
            process = self._processes.get(job.job_id)
            if process is None or process.poll() is not None:
                raise RuntimeError("该任务当前没有活动进程")
            job.status = "STOPPING"
            self._save()
            try:
                if os.name == "nt":
                    process.terminate()
                else:
                    os.kill(process.pid, signal.SIGTERM)
            except Exception as exc:
                job.error = f"stop_failed:{type(exc).__name__}:{exc}"
                self._save()
                raise
            return self._public_job(job)

    def log_tail(self, job_id: str, max_chars: int = 16000) -> dict[str, Any]:
        self._refresh_processes()
        job = self._jobs.get(job_id)
        if job is None:
            raise KeyError(job_id)
        path = Path(job.log_path or "")
        text = ""
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="replace")[-max_chars:]
        return {"job": self._public_job(job), "log_tail": text}

    def _artifact_roots(self) -> dict[str, Path]:
        return {
            "results": self.results_dir,
            "evidence": self.workspace / ".mosaic_evidence",
            "deliverables": self.workspace / ".mosaic_deliverables",
        }

    def _resolve_artifact(self, artifact_id: str) -> tuple[str, Path, str]:
        raw = str(artifact_id or "")
        if ":" not in raw:
            raise KeyError("invalid artifact id")
        category, logical = raw.split(":", 1)
        root = self._artifact_roots().get(category)
        if root is None:
            raise KeyError("unknown artifact category")
        target = (root / logical.replace("\\", "/")).resolve()
        root_resolved = root.resolve()
        if target != root_resolved and root_resolved not in target.parents:
            raise PermissionError("artifact path escapes approved root")
        if not target.is_file():
            raise KeyError("artifact not found")
        return category, target, target.relative_to(root_resolved).as_posix()

    @staticmethod
    def _artifact_title(category: str, path: Path, logical: str) -> str:
        name = path.name
        lower = name.casefold()
        parts = Path(logical).parts
        if "repair_report" in lower:
            return "修复报告"
        if "investment_research_report" in lower:
            return "投研评估报告"
        if lower.endswith((".md", ".markdown")) and "report" in lower:
            return "最终报告" if category in {"results", "deliverables"} else "证据报告"
        if lower.endswith(".json") and category == "results":
            return "运行结果"
        if category == "evidence":
            task_id = parts[-2] if len(parts) >= 2 else "task"
            labels = {
                "inventory": "仓库清单", "diagnose": "故障诊断", "patch": "修复补丁",
                "build": "构建结果", "test": "测试结果", "report": "最终报告",
                "collect": "数据采集", "analyze": "分析结果", "risk": "风险评估",
            }
            return f"{labels.get(task_id, task_id)} · 执行证据"
        if category == "deliverables":
            labels = {
                "inventory": "仓库清单", "diagnosis": "故障诊断", "patch": "修复补丁",
                "build": "构建结果", "test": "回归测试结果", "repair_report": "最终修复报告",
                "ingest": "数据采集结果", "decrypted_financials": "端侧解密结果",
                "sentiment_analysis": "情绪分析", "risk_model": "风险模型",
                "compliance_audit": "合规审计", "investment_research_report": "投研评估报告",
            }
            return labels.get(path.stem, f"{path.stem} · 任务交付物")
        return name

    @staticmethod
    def _artifact_display_name(category: str, path: Path, logical: str) -> str:
        """Human-facing name that never forces users to parse content hashes."""
        parts = Path(logical).parts
        if category == "evidence" and len(parts) >= 2:
            task_id = parts[-2]
            labels = {
                "inventory": "仓库清单", "diagnose": "故障诊断", "patch": "修复补丁",
                "build": "构建结果", "test": "测试结果", "report": "最终报告",
                "collect": "数据采集", "analyze": "分析结果", "risk": "风险评估",
            }
            return f"{labels.get(task_id, task_id)}{path.suffix or '.txt'}"
        return path.name

    @staticmethod
    def _artifact_context(category: str, path: Path, logical: str) -> str:
        parts = Path(logical).parts
        if category == "evidence":
            run_id = parts[-3] if len(parts) >= 3 else "run"
            digest = path.stem
            digest_short = (digest[:12] + "…") if len(digest) > 12 else digest
            return f"Run {run_id} · SHA256 {digest_short}"
        if category == "deliverables" and len(parts) >= 2:
            scenario = parts[1] if len(parts) >= 3 else "deliverable"
            return f"Run {parts[0]} · {scenario}"
        if category == "results":
            return f"Run {path.stem}"
        return category

    @staticmethod
    def _artifact_matches_run(category: str, logical: str, run_id: str | None) -> bool:
        if not run_id:
            return True
        parts = Path(logical).parts
        if category == "results":
            return Path(logical).stem == run_id
        if category in {"evidence", "deliverables"}:
            return bool(parts) and parts[0] == run_id
        return False

    def artifacts(self, run_id: str | None = None) -> list[dict[str, Any]]:
        files: list[dict[str, Any]] = []
        for category, root in self._artifact_roots().items():
            if not root.exists():
                continue
            for path in root.rglob("*"):
                if not path.is_file():
                    continue
                logical = path.relative_to(root).as_posix()
                if not self._artifact_matches_run(category, logical, run_id):
                    continue
                stat_result = path.stat()
                files.append({
                    "artifact_id": f"{category}:{logical}",
                    "name": path.name,
                    "display_name": self._artifact_display_name(category, path, logical),
                    "title": self._artifact_title(category, path, logical),
                    "context": self._artifact_context(category, path, logical),
                    "logical_path": logical,
                    "category": category,
                    "bytes": stat_result.st_size,
                    "modified_at": stat_result.st_mtime,
                    "previewable": path.suffix.casefold() in {".txt", ".md", ".json", ".csv", ".log", ".py"} and stat_result.st_size <= 2_000_000,
                })
        return sorted(files, key=lambda x: x["modified_at"], reverse=True)[:300]

    def artifact_preview(self, artifact_id: str, *, max_chars: int = 120_000) -> dict[str, Any]:
        category, path, logical = self._resolve_artifact(artifact_id)
        if path.stat().st_size > 2_000_000:
            raise ValueError("文件过大，不提供内嵌预览；请下载")
        if path.suffix.casefold() not in {".txt", ".md", ".json", ".csv", ".log", ".py"}:
            raise ValueError("该文件类型不支持文本预览")
        text = path.read_text(encoding="utf-8", errors="replace")
        return {
            "artifact_id": artifact_id, "category": category, "name": path.name,
            "display_name": self._artifact_display_name(category, path, logical),
            "title": self._artifact_title(category, path, logical),
            "context": self._artifact_context(category, path, logical),
            "logical_path": logical, "bytes": path.stat().st_size,
            "content": text[:max_chars], "truncated": len(text) > max_chars,
        }

    def artifact_download(self, artifact_id: str) -> tuple[Path, str]:
        _category, path, _logical = self._resolve_artifact(artifact_id)
        return path, path.name

