"""HTTP server for the MOSAIC-Ω competition console.

Read APIs project authoritative snapshots. Explicit /api/control/* POST routes
launch real backend jobs; they never directly mutate visual state.
"""
from __future__ import annotations

import json
import mimetypes
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from mosaic_omega.console_api import ConsoleDataSource
from .control import CompetitionControlPlane


class ConsoleHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        data_source: ConsoleDataSource,
        frontend_dir: Path,
        control: CompetitionControlPlane,
    ) -> None:
        super().__init__(address, ConsoleRequestHandler)
        self.data_source = data_source
        self.frontend_dir = frontend_dir.resolve()
        self.control = control


class ConsoleRequestHandler(BaseHTTPRequestHandler):
    server: ConsoleHTTPServer

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[console] {self.address_string()} {fmt % args}")

    def _json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("请求体必须是 UTF-8 JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("请求体必须是 JSON object")
        return payload

    def _file(self, path: Path) -> None:
        try:
            resolved = path.resolve()
            resolved.relative_to(self.server.frontend_dir)
        except (ValueError, OSError):
            self.send_error(404)
            return
        if not resolved.is_file():
            self.send_error(404)
            return
        body = resolved.read_bytes()
        content_type = mimetypes.guess_type(str(resolved))[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _sse_runtime(self, run_id: str | None) -> None:
        """Emit lightweight runtime invalidation events.

        The stream does not duplicate the snapshot payload.  It only tells the
        browser that authoritative runtime state changed; the active view then
        fetches the relevant DTO.  This keeps provider/endpoint/agent settings
        out of the hot refresh path and avoids full-page polling.
        """
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        last_signature: tuple[Any, ...] | None = None
        started = time.monotonic()
        try:
            while time.monotonic() - started < 25.0:
                snapshot = self.server.data_source.snapshot(run_id)
                status = self.server.control.status()
                active = status.get("active_job") if isinstance(status, dict) else None
                signature = (
                    snapshot.get("generated_at") if isinstance(snapshot, dict) else None,
                    active.get("job_id") if isinstance(active, dict) else None,
                    active.get("status") if isinstance(active, dict) else None,
                )
                if signature != last_signature:
                    body = json.dumps({
                        "generated_at": signature[0],
                        "job_id": signature[1],
                        "job_status": signature[2],
                    }, ensure_ascii=False).encode("utf-8")
                    self.wfile.write(b"event: runtime\n")
                    self.wfile.write(b"data: " + body + b"\n\n")
                    self.wfile.flush()
                    last_signature = signature
                else:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                time.sleep(0.8)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    def _download_file(self, path: Path, filename: str) -> None:
        body = path.read_bytes()
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        safe_name = filename.replace('"', "_").replace("\r", "_").replace("\n", "_")
        encoded = urllib.parse.quote(filename, safe="")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Disposition", f"attachment; filename=\"{safe_name}\"; filename*=UTF-8''{encoded}")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    @staticmethod
    def _query(path: str) -> tuple[str, dict[str, list[str]]]:
        parsed = urllib.parse.urlsplit(path)
        return parsed.path, urllib.parse.parse_qs(parsed.query)

    def _public(self, payload: Any) -> Any:
        return self.server.control.redact_public_payload(payload)

    def do_GET(self) -> None:  # noqa: N802
        path, query = self._query(self.path)
        run_id = query.get("run_id", [None])[0]
        if path == "/api/stream":
            self._sse_runtime(run_id)
            return
        if path == "/api/health":
            snapshot = self.server.data_source.snapshot(run_id)
            self._json({
                "ready": snapshot is not None,
                "interactive": True,
                "control_plane": True,
                "environment": self._public(self.server.control.environment()),
            })
            return
        if path == "/api/control/status":
            self._json(self._public(self.server.control.status()))
            return
        if path == "/api/settings/providers":
            # Explicit settings API: local Ollama URLs must remain editable.
            # ProviderSettingsStore never returns secret plaintext.
            self._json(self.server.control.provider_settings.public())
            return
        if path == "/api/settings/agents":
            self._json(self.server.control.agent_settings.public())
            return
        if path == "/api/settings/endpoints":
            # Addresses belong only to the explicit Settings screen. They are
            # intentionally absent from observability/judge/control DTOs.
            self._json(self.server.control.endpoint_settings.public_settings())
            return
        if path == "/api/control/artifact-preview":
            artifact_id = query.get("artifact_id", [None])[0]
            if not artifact_id:
                self._json({"error": "artifact_id_required"}, 400)
                return
            try:
                self._json(self._public(self.server.control.artifact_preview(artifact_id)))
            except (KeyError, ValueError, PermissionError) as exc:
                self._json({"error": type(exc).__name__, "message": str(exc)}, 404 if isinstance(exc, KeyError) else 400)
            return
        if path == "/api/control/artifact-download":
            artifact_id = query.get("artifact_id", [None])[0]
            if not artifact_id:
                self._json({"error": "artifact_id_required"}, 400)
                return
            try:
                file_path, filename = self.server.control.artifact_download(artifact_id)
                self._download_file(file_path, filename)
            except (KeyError, PermissionError):
                self.send_error(404)
            return
        if path == "/api/control/artifacts":
            self._json(self._public(self.server.control.artifacts(run_id=run_id)))
            return
        if path == "/api/control/log":
            job_id = query.get("job_id", [None])[0]
            if not job_id:
                self._json({"error": "job_id_required"}, 400)
                return
            try:
                self._json(self._public(self.server.control.log_tail(job_id)))
            except KeyError:
                self._json({"error": "job_not_found", "job_id": job_id}, 404)
            return
        if path == "/api/runs":
            self._json(self._public(self.server.data_source.runs()))
            return
        if path == "/api/snapshot":
            snapshot = self.server.data_source.snapshot(run_id)
            if snapshot is None:
                self._json({"status": "waiting_for_snapshot"}, 404)
            else:
                self._json(self._public(snapshot))
            return
        if path == "/api/events":
            self._json(self._public(self.server.data_source.events(
                run_id=run_id,
                event_type=query.get("type", [None])[0],
                task_id=query.get("task_id", [None])[0],
                trace_id=query.get("trace_id", [None])[0],
            )))
            return
        if path.startswith("/api/"):
            section = path.removeprefix("/api/").replace("-", "_")
            try:
                payload = self.server.data_source.section(section, run_id)
            except KeyError:
                self._json({"error": "unknown_section", "section": section}, 404)
                return
            if payload is None:
                self._json({"status": "waiting_for_snapshot"}, 404)
            else:
                self._json(self._public(payload))
            return

        if path in {"", "/"}:
            self._file(self.server.frontend_dir / "index.html")
            return
        relative = path.lstrip("/")
        target_file = self.server.frontend_dir / relative
        if not target_file.is_file() and not path.startswith("/api/"):
            self._file(self.server.frontend_dir / "index.html")
            return
        self._file(target_file)

    def do_POST(self) -> None:  # noqa: N802
        path, _ = self._query(self.path)
        if not (path.startswith("/api/control/") or path.startswith("/api/settings/")):
            self._json({"error": "mutation_not_allowed_here", "message": "Use explicit control/settings endpoints."}, 405)
            return
        try:
            body = self._read_json()
            if path == "/api/settings/provider":
                result = self.server.control.provider_settings.save(body)
                self._json({"ok": True, "result": result}, 200)
                return
            if path == "/api/settings/test-provider":
                result = self.server.control.provider_settings.test_connection(body)
                self._json({"ok": True, "result": result}, 200)
                return
            if path == "/api/settings/clear-provider-key":
                result = self.server.control.provider_settings.clear_secret(str(body.get("provider_id") or ""))
                self._json({"ok": True, "result": result}, 200)
                return
            if path == "/api/settings/agent":
                result = self.server.control.agent_settings.save(body)
                self._json({"ok": True, "result": result}, 200)
                return
            if path == "/api/settings/delete-agent":
                result = self.server.control.agent_settings.delete(str(body.get("agent_id") or ""))
                self._json({"ok": True, "result": result}, 200)
                return
            if path == "/api/settings/endpoint":
                result = self.server.control.endpoint_settings.save(body)
                self._json({"ok": True, "result": result}, 200)
                return
            if path == "/api/settings/test-endpoint":
                result = self.server.control.endpoint_settings.test_connection(str(body.get("endpoint_id") or ""))
                self._json({"ok": True, "result": result}, 200)
                return
            if path == "/api/settings/delete-endpoint":
                result = self.server.control.endpoint_settings.delete(str(body.get("endpoint_id") or ""))
                self._json({"ok": True, "result": result}, 200)
                return
            if path == "/api/control/start-custom":
                result = self.server.control.start_custom(body.get("goal", ""))
            elif path == "/api/control/start-scenario":
                result = self.server.control.start_scenario(body.get("scenario", ""))
            elif path == "/api/control/start-fault":
                result = self.server.control.start_fault(body.get("fault", ""), body.get("requirement"))
            elif path == "/api/control/start-benchmark":
                result = self.server.control.start_benchmark(body.get("benchmark", ""))
            elif path == "/api/control/start-remote-acceptance":
                result = self.server.control.start_remote_acceptance(str(body.get("endpoint_id") or ""))
            elif path == "/api/control/stop":
                result = self.server.control.stop(body.get("job_id"))
            else:
                self._json({"error": "unknown_control_action", "path": path}, 404)
                return
            self._json({"ok": True, "result": self._public(result)}, 202)
        except KeyError as exc:
            self._json({"ok": False, "error": "not_found", "message": str(exc)}, 404)
        except (ValueError, RuntimeError) as exc:
            self._json({"ok": False, "error": type(exc).__name__, "message": str(exc)}, 409 if isinstance(exc, RuntimeError) else 400)
        except Exception as exc:
            self._json({"ok": False, "error": type(exc).__name__, "message": str(exc)}, 500)

    def do_PUT(self) -> None:  # noqa: N802
        self._json({"error": "unsupported_method"}, 405)

    do_DELETE = do_PUT  # type: ignore[assignment]
    do_PATCH = do_PUT  # type: ignore[assignment]


def serve(
    host: str,
    port: int,
    *,
    snapshot_dir: str | Path,
    frontend_dir: str | Path,
    project_root: str | Path,
    workspace: str | Path,
) -> None:
    source = ConsoleDataSource(snapshot_dir)
    control = CompetitionControlPlane(Path(project_root), Path(workspace))
    server = ConsoleHTTPServer((host, port), source, Path(frontend_dir), control)
    print("MOSAIC-Ω application service started")
    print("Mode: PRODUCT-FIRST + INTERACTIVE + AUTHORITATIVE SNAPSHOT PROJECTION")
    print("Local transport and workspace paths are intentionally hidden from the user-facing output.")
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
