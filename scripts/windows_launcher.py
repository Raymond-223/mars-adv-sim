#!/usr/bin/env python3
"""Zero-install Windows launcher for MOSAIC-Ω v1.9.0.

Normal startup deliberately avoids venv creation, pip, PyPI and build isolation.
The desktop control plane runs directly from the bundled source tree using the
selected local Python 3.10-3.13 interpreter. Optional capabilities such as
OR-Tools and MQTT are detected, never installed automatically.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / "src"
for path in (SOURCE_ROOT, ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

LOG_DIR = ROOT / ".mosaic_logs"
LAUNCHER_LOG = LOG_DIR / "launcher.log"
SERVER_OUT = LOG_DIR / "server-stdout.log"
SERVER_ERR = LOG_DIR / "server-stderr.log"
RUNTIME_MARKER = LOG_DIR / "runtime-capabilities.json"
SUPPORTED_MIN = (3, 10)
SUPPORTED_MAX = (3, 13)


def log(message: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] {message}"
    print(message, flush=True)
    with LAUNCHER_LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def supported_python() -> bool:
    version = sys.version_info[:2]
    return SUPPORTED_MIN <= version <= SUPPORTED_MAX


def importable(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ModuleNotFoundError, AttributeError, ValueError):
        return False


def runtime_capabilities() -> dict[str, object]:
    return {
        "release": "1.9.0",
        "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "python_executable": sys.executable,
        "architecture": platform.machine(),
        "runtime_mode": "ZERO_INSTALL_STDLIB",
        "core_ready": importable("mosaic_omega"),
        "jsonschema_reference_validator": importable("jsonschema"),
        "openai_sdk_ready": importable("openai"),
        "mqtt_ready": importable("paho.mqtt.client"),
        "ortools_ready": importable("ortools"),
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }


def ensure_runtime_ready() -> None:
    log("[1/3] Checking local zero-install runtime...")
    if not supported_python():
        raise RuntimeError(
            f"Python {sys.version_info.major}.{sys.version_info.minor} is not supported. "
            "Use 64-bit Python 3.10, 3.11, 3.12, or 3.13."
        )
    if not SOURCE_ROOT.is_dir() or not (SOURCE_ROOT / "mosaic_omega").is_dir():
        raise RuntimeError(
            "Bundled MOSAIC source tree is missing. Re-extract the complete release ZIP before starting."
        )

    caps = runtime_capabilities()
    RUNTIME_MARKER.write_text(json.dumps(caps, ensure_ascii=False, indent=2), encoding="utf-8")
    if not caps["core_ready"]:
        raise RuntimeError("Bundled MOSAIC core could not be imported from src/. The release is incomplete.")

    log("Core runtime: READY (no venv, no pip, no PyPI)")
    if caps["openai_sdk_ready"]:
        log("DeepSeek/OpenAI-compatible transport: optional OpenAI SDK available")
    else:
        log("DeepSeek/OpenAI-compatible transport: built-in stdlib HTTP available")
    log("MQTT: READY" if caps["mqtt_ready"] else "MQTT: optional package not installed; desktop UI is unaffected")
    log(
        "OR-Tools: READY"
        if caps["ortools_ready"]
        else "OR-Tools: not installed; UI opens normally and strict OR-Tools tasks remain fail-closed"
    )


def free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def browser_candidates() -> list[Path]:
    candidates: list[Path] = []
    bases = [os.environ.get("PROGRAMFILES(X86)"), os.environ.get("PROGRAMFILES"), os.environ.get("LOCALAPPDATA")]
    rels = [
        Path("Microsoft/Edge/Application/msedge.exe"),
        Path("Google/Chrome/Application/chrome.exe"),
    ]
    for base in bases:
        if not base:
            continue
        for rel in rels:
            candidate = Path(base) / rel
            if candidate.is_file() and candidate not in candidates:
                candidates.append(candidate)
    for name in ("msedge.exe", "chrome.exe"):
        resolved = shutil.which(name)
        if resolved:
            candidate = Path(resolved)
            if candidate not in candidates:
                candidates.append(candidate)
    return candidates


def wait_for_health(url: str, server: subprocess.Popen[bytes], timeout: float = 25.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if server.poll() is not None:
            tail = SERVER_ERR.read_text(encoding="utf-8", errors="replace")[-8000:] if SERVER_ERR.exists() else ""
            raise RuntimeError(f"Backend exited early with code {server.returncode}.\n{tail}")
        try:
            with urllib.request.urlopen(url + "api/health", timeout=0.8) as response:
                if response.status == 200:
                    return
        except Exception:
            time.sleep(0.2)
    tail = SERVER_ERR.read_text(encoding="utf-8", errors="replace")[-8000:] if SERVER_ERR.exists() else ""
    raise RuntimeError(f"Backend health check timed out.\n{tail}")


def open_app_window(url: str) -> tuple[subprocess.Popen[bytes] | None, Path | None]:
    for browser in browser_candidates():
        profile = Path(tempfile.mkdtemp(prefix="mosaic-browser-", dir=str(LOG_DIR)))
        args = [
            str(browser),
            f"--app={url}",
            "--start-maximized",
            "--no-first-run",
            "--disable-session-crashed-bubble",
            f"--user-data-dir={profile}",
        ]
        if browser.name.casefold() == "msedge.exe":
            args.append("--disable-features=msEdgeSidebarV2")
        try:
            proc = subprocess.Popen(args, cwd=ROOT)
            time.sleep(0.8)
            if proc.poll() is None:
                log(f"Opened MOSAIC app window with {browser.name}.")
                return proc, profile
        except OSError as exc:
            log(f"Browser launch attempt failed for {browser}: {exc}")
        shutil.rmtree(profile, ignore_errors=True)

    log("Edge/Chrome app mode unavailable; opening the system default browser.")
    if not webbrowser.open(url, new=1):
        raise RuntimeError(f"Could not open a browser automatically. Open this URL manually: {url}")
    return None, None


def diagnose() -> int:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    caps = runtime_capabilities()
    print(json.dumps(caps, ensure_ascii=False, indent=2))
    return 0 if supported_python() and bool(caps["core_ready"]) else 2


def launch() -> int:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    LAUNCHER_LOG.write_text("MOSAIC-Omega v1.9.0 zero-install Windows launcher\n", encoding="utf-8")
    SERVER_OUT.write_text("", encoding="utf-8")
    SERVER_ERR.write_text("", encoding="utf-8")

    ensure_runtime_ready()

    log("[2/3] Starting local backend...")
    port = free_loopback_port()
    url = f"http://127.0.0.1:{port}/"
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    source_paths = [str(ROOT), str(SOURCE_ROOT)]
    if env.get("PYTHONPATH"):
        source_paths.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(source_paths)

    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    with SERVER_OUT.open("wb") as out, SERVER_ERR.open("wb") as err:
        server = subprocess.Popen(
            [sys.executable, "-X", "utf8", "apps/console/main.py", "--host", "127.0.0.1", "--port", str(port)],
            cwd=ROOT,
            stdout=out,
            stderr=err,
            env=env,
            creationflags=creationflags,
        )

    browser_proc: subprocess.Popen[bytes] | None = None
    profile: Path | None = None
    try:
        wait_for_health(url, server)
        log("[3/3] Backend READY; opening MOSAIC...")
        browser_proc, profile = open_app_window(url)
        if browser_proc is not None:
            log("Close the MOSAIC app window to stop the local backend.")
            browser_proc.wait()
        else:
            log("Default-browser mode is active. Keep this launcher window open; press Ctrl+C to stop MOSAIC.")
            while server.poll() is None:
                time.sleep(0.5)
        return 0
    except KeyboardInterrupt:
        log("Stopping MOSAIC...")
        return 0
    finally:
        if server.poll() is None:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
        if profile:
            shutil.rmtree(profile, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--diagnose", action="store_true", help="print local runtime capability detection and exit")
    args = parser.parse_args()
    try:
        return diagnose() if args.diagnose else launch()
    except Exception as exc:
        try:
            log(f"ERROR: {type(exc).__name__}: {exc}")
            log(f"See logs under: {LOG_DIR}")
        except Exception:
            print(f"MOSAIC startup error: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
