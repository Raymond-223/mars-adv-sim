"""Product settings stores for model providers, Agents, and remote execution endpoints.

Security invariants:
- provider secrets are isolated per provider ID;
- secrets are never returned by public APIs;
- Windows persists secrets with DPAPI; non-Windows fallback is chmod 0600 and labelled;
- local loopback URLs are allowed on the explicit Settings API (e.g. Ollama) but
  never leak into observability/judge DTOs;
- remote endpoint passwords are stored separately from endpoint metadata.
"""
from __future__ import annotations

import base64
import ctypes
import json
import os
import stat
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from mosaic_omega.providers import create_openai_compatible_client


@dataclass(frozen=True)
class ProviderDefinition:
    id: str
    name: str
    default_base_url: str
    default_model: str
    requires_key: bool
    runtime_mode: str


PROVIDERS: dict[str, ProviderDefinition] = {
    "deepseek": ProviderDefinition(
        id="deepseek", name="DeepSeek", default_base_url="https://api.deepseek.com",
        default_model="deepseek-v4-flash", requires_key=True, runtime_mode="openai_compatible",
    ),
    "openai_compatible": ProviderDefinition(
        id="openai_compatible", name="OpenAI-compatible API", default_base_url="",
        default_model="", requires_key=True, runtime_mode="openai_compatible",
    ),
    "ollama": ProviderDefinition(
        id="ollama", name="Ollama / local OpenAI-compatible",
        default_base_url="http://127.0.0.1:11434/v1", default_model="",
        requires_key=False, runtime_mode="openai_compatible",
    ),
}


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _blob(data: bytes) -> tuple[_DATA_BLOB, Any]:
    if not data:
        return _DATA_BLOB(0, None), None
    buf = ctypes.create_string_buffer(data, len(data))
    return _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))), buf


def _dpapi_protect(data: bytes) -> bytes:
    if os.name != "nt":
        raise RuntimeError("DPAPI is only available on Windows")
    in_blob, in_buf = _blob(data)
    out_blob = _DATA_BLOB()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    ok = crypt32.CryptProtectData(ctypes.byref(in_blob), None, None, None, None, 0, ctypes.byref(out_blob))
    _ = in_buf
    if not ok:
        raise OSError("CryptProtectData failed")
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)


def _dpapi_unprotect(data: bytes) -> bytes:
    if os.name != "nt":
        raise RuntimeError("DPAPI is only available on Windows")
    in_blob, in_buf = _blob(data)
    out_blob = _DATA_BLOB()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    ok = crypt32.CryptUnprotectData(ctypes.byref(in_blob), None, None, None, None, 0, ctypes.byref(out_blob))
    _ = in_buf
    if not ok:
        raise OSError("CryptUnprotectData failed")
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)


def _write_secret_file(path: Path, secret: str) -> None:
    raw = secret.encode("utf-8")
    if os.name == "nt":
        payload = b"DPAPI1:" + base64.b64encode(_dpapi_protect(raw))
    else:
        payload = b"PLAIN1:" + base64.b64encode(raw)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def _read_secret_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        payload = path.read_bytes()
        if payload.startswith(b"DPAPI1:"):
            raw = _dpapi_unprotect(base64.b64decode(payload.split(b":", 1)[1]))
        elif payload.startswith(b"PLAIN1:"):
            raw = base64.b64decode(payload.split(b":", 1)[1])
        else:
            return None
        return raw.decode("utf-8")
    except Exception:
        return None


class ProviderSettingsStore:
    """Per-provider settings + per-provider secret isolation."""

    def __init__(self, workspace: Path) -> None:
        self.directory = workspace / "config"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.settings_path = self.directory / "providers.json"
        self.legacy_settings_path = self.directory / "provider.json"
        self.secret_dir = self.directory / "provider_secrets"
        self.secret_dir.mkdir(parents=True, exist_ok=True)
        self.legacy_secret_path = self.directory / "provider.secret"
        self._session_keys: dict[str, str] = {}
        self._migrate_legacy_once()

    def _secret_mode(self) -> str:
        return "windows_dpapi" if os.name == "nt" else "permission_limited_file"

    @staticmethod
    def _safe_provider_id(provider_id: str) -> str:
        if provider_id not in PROVIDERS:
            raise ValueError("不支持的模型服务")
        return provider_id

    def _secret_path(self, provider_id: str) -> Path:
        provider_id = self._safe_provider_id(provider_id)
        return self.secret_dir / f"{provider_id}.secret"

    def _default_document(self) -> dict[str, Any]:
        return {
            "active_provider_id": "deepseek",
            "providers": {
                pid: {
                    "base_url": definition.default_base_url,
                    "model": definition.default_model,
                    "updated_at": None,
                }
                for pid, definition in PROVIDERS.items()
            },
        }

    def _migrate_legacy_once(self) -> None:
        if self.settings_path.exists() or not self.legacy_settings_path.is_file():
            return
        doc = self._default_document()
        try:
            old = json.loads(self.legacy_settings_path.read_text(encoding="utf-8"))
        except Exception:
            old = {}
        provider_id = str(old.get("provider_id") or "deepseek")
        if provider_id not in PROVIDERS:
            provider_id = "deepseek"
        doc["active_provider_id"] = provider_id
        doc["providers"][provider_id] = {
            "base_url": str(old.get("base_url") or PROVIDERS[provider_id].default_base_url),
            "model": str(old.get("model") or PROVIDERS[provider_id].default_model),
            "updated_at": old.get("updated_at"),
        }
        self._write_settings(doc)
        # Legacy secret can only be safely attributed to the legacy active provider.
        if self.legacy_secret_path.is_file() and not self._secret_path(provider_id).is_file():
            secret = _read_secret_file(self.legacy_secret_path)
            if secret:
                _write_secret_file(self._secret_path(provider_id), secret)
        try:
            self.legacy_secret_path.unlink(missing_ok=True)
        except OSError:
            pass

    def _read_settings(self) -> dict[str, Any]:
        doc = self._default_document()
        if self.settings_path.is_file():
            try:
                raw = json.loads(self.settings_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    active = str(raw.get("active_provider_id") or "deepseek")
                    if active in PROVIDERS:
                        doc["active_provider_id"] = active
                    rows = raw.get("providers")
                    if isinstance(rows, dict):
                        for pid, definition in PROVIDERS.items():
                            row = rows.get(pid)
                            if isinstance(row, dict):
                                doc["providers"][pid] = {
                                    "base_url": str(row.get("base_url") or definition.default_base_url),
                                    "model": str(row.get("model") or definition.default_model),
                                    "updated_at": row.get("updated_at"),
                                }
            except Exception:
                pass
        # Environment defaults only seed a provider that was never persisted.
        deep = doc["providers"]["deepseek"]
        if not deep.get("updated_at"):
            deep["base_url"] = os.getenv("DEEPSEEK_BASE_URL", deep["base_url"])
            deep["model"] = os.getenv("DEEPSEEK_MODEL", deep["model"])
        return doc

    def _write_settings(self, doc: dict[str, Any]) -> None:
        tmp = self.settings_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.settings_path)

    def _env_secret(self, provider_id: str) -> str | None:
        if provider_id == "deepseek":
            return os.getenv("DEEPSEEK_API_KEY")
        if provider_id == "openai_compatible":
            return os.getenv("OPENAI_API_KEY")
        return None

    def _write_secret(self, provider_id: str, secret: str) -> None:
        provider_id = self._safe_provider_id(provider_id)
        self._session_keys[provider_id] = secret
        _write_secret_file(self._secret_path(provider_id), secret)

    def _read_secret(self, provider_id: str) -> str | None:
        provider_id = self._safe_provider_id(provider_id)
        if provider_id == "ollama":
            return None
        if self._session_keys.get(provider_id):
            return self._session_keys[provider_id]
        env_key = self._env_secret(provider_id)
        if env_key:
            self._session_keys[provider_id] = env_key
            return env_key
        value = _read_secret_file(self._secret_path(provider_id))
        if value:
            self._session_keys[provider_id] = value
        return value

    def clear_secret(self, provider_id: str) -> dict[str, Any]:
        provider_id = self._safe_provider_id(provider_id)
        self._session_keys.pop(provider_id, None)
        try:
            self._secret_path(provider_id).unlink(missing_ok=True)
        except OSError as exc:
            raise RuntimeError(f"无法清除 API Key：{exc}") from exc
        return self.public(provider_id=provider_id)

    def public(self, *, provider_id: str | None = None) -> dict[str, Any]:
        doc = self._read_settings()
        active_id = str(provider_id or doc.get("active_provider_id") or "deepseek")
        if active_id not in PROVIDERS:
            active_id = "deepseek"
        definition = PROVIDERS[active_id]
        cfg = doc["providers"][active_id]
        base_url = str(cfg.get("base_url") or definition.default_base_url).rstrip("/")
        model = str(cfg.get("model") or definition.default_model)
        host = (urlsplit(base_url).hostname or "").casefold()
        key_present = bool(self._read_secret(active_id)) if definition.requires_key else False
        configured = bool(base_url and model and (key_present or not definition.requires_key))
        return {
            "providers": [asdict(item) for item in PROVIDERS.values()],
            "active": {
                "provider_id": active_id,
                "provider_name": definition.name,
                "base_url": base_url,
                "endpoint_host": host,
                "model": model,
                "api_key_present": key_present,
                "api_key_required": definition.requires_key,
                "api_key_mask": "••••••••••••" if key_present else "",
                "configured": configured,
                "secret_storage": self._secret_mode(),
                "updated_at": cfg.get("updated_at"),
            },
            "provider_key_presence": {
                pid: bool(self._read_secret(pid)) if PROVIDERS[pid].requires_key else False
                for pid in PROVIDERS
            },
            "provider_configs": {
                pid: {
                    "base_url": str(doc["providers"][pid].get("base_url") or PROVIDERS[pid].default_base_url).rstrip("/"),
                    "model": str(doc["providers"][pid].get("model") or PROVIDERS[pid].default_model),
                    "api_key_present": bool(self._read_secret(pid)) if PROVIDERS[pid].requires_key else False,
                    "updated_at": doc["providers"][pid].get("updated_at"),
                }
                for pid in PROVIDERS
            },
        }

    def save(self, payload: dict[str, Any]) -> dict[str, Any]:
        provider_id = self._safe_provider_id(str(payload.get("provider_id") or "").strip())
        definition = PROVIDERS[provider_id]
        base_url = str(payload.get("base_url") or definition.default_base_url).strip().rstrip("/")
        model = str(payload.get("model") or definition.default_model).strip()
        if not base_url:
            raise ValueError("服务地址不能为空")
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("服务地址必须是有效的 HTTP(S) 地址")
        if not model:
            raise ValueError("模型名称不能为空")
        api_key = payload.get("api_key")
        if api_key is not None and str(api_key).strip():
            if provider_id == "ollama":
                raise ValueError("Ollama 本地 Provider 不接受云 API Key")
            self._write_secret(provider_id, str(api_key).strip())
        if definition.requires_key and not self._read_secret(provider_id):
            raise ValueError("该模型服务需要当前 Provider 自己的 API Key")
        doc = self._read_settings()
        doc["active_provider_id"] = provider_id
        doc["providers"][provider_id] = {
            "base_url": base_url, "model": model, "updated_at": time.time(),
        }
        self._write_settings(doc)
        return self.public()

    def runtime_environment(self) -> dict[str, str]:
        active = self.public()["active"]
        provider_id = str(active["provider_id"])
        key = self._read_secret(provider_id) or ("ollama-local" if provider_id == "ollama" else "")
        # ``MOSAIC_API_KEY`` is the only provider-neutral runtime secret alias.
        # Provider-specific environment names are populated only for the matching
        # active provider, so a DeepSeek secret can never masquerade as an OpenAI
        # compatible secret (and vice versa).  Ollama receives only a non-secret
        # local compatibility token required by OpenAI-compatible client libraries.
        env = {
            "MOSAIC_PROVIDER": provider_id,
            "MOSAIC_API_KEY": key,
            "DEEPSEEK_BASE_URL": str(active["base_url"]),
            "OPENAI_BASE_URL": str(active["base_url"]),
            "DEEPSEEK_MODEL": str(active["model"]),
            "LLM_MODEL_NAME": str(active["model"]),
        }
        if provider_id == "deepseek":
            env["DEEPSEEK_API_KEY"] = key
        elif provider_id == "openai_compatible":
            env["OPENAI_API_KEY"] = key
        return env

    def test_connection(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        if payload:
            provider_id = self._safe_provider_id(str(payload.get("provider_id") or self.public()["active"]["provider_id"]))
            definition = PROVIDERS[provider_id]
            base_url = str(payload.get("base_url") or definition.default_base_url).strip().rstrip("/")
            model = str(payload.get("model") or definition.default_model).strip()
            supplied = str(payload.get("api_key") or "").strip()
            key = supplied or self._read_secret(provider_id) or ""
        else:
            active = self.public()["active"]
            provider_id = str(active["provider_id"])
            definition = PROVIDERS[provider_id]
            base_url = str(active["base_url"])
            model = str(active["model"])
            key = self._read_secret(provider_id) or ""
        if definition.requires_key and not key:
            raise ValueError("当前 Provider 的 API Key 尚未配置")
        if not base_url or not model:
            raise ValueError("服务地址或模型名称未配置")
        started = time.perf_counter()
        client = create_openai_compatible_client(
            api_key=key or "ollama-local", base_url=base_url, timeout=20.0, max_retries=0,
        )
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "Reply with OK only."}],
            temperature=0,
            max_tokens=4,
        )
        latency_ms = (time.perf_counter() - started) * 1000.0
        usage = getattr(response, "usage", {}) or {}
        if hasattr(usage, "model_dump"):
            usage = usage.model_dump()
        elif not isinstance(usage, dict):
            usage = {
                k: getattr(usage, k) for k in ("prompt_tokens", "completion_tokens", "total_tokens")
                if getattr(usage, k, None) is not None
            }
        return {
            "ok": True, "provider_id": provider_id,
            "model": getattr(response, "model", None) or model,
            "request_id": getattr(response, "id", None),
            "latency_ms": round(latency_ms, 1),
            "measurement_semantics": "measured_request_round_trip",
            "transport": str(getattr(client, "_mosaic_transport", "unknown")),
            "usage": usage,
        }


@dataclass(frozen=True)
class AgentTemplate:
    agent_id: str
    name: str
    role: str
    skills: tuple[str, ...]
    permissions: tuple[str, ...]
    tier: str
    model: str
    max_load: int = 1
    enabled: bool = True


class AgentSettingsStore:
    """Persistent user-facing Agent Studio templates consumed by new custom runs."""
    VALID_TIERS = {"device", "edge", "cloud"}

    def __init__(self, workspace: Path) -> None:
        self.directory = workspace / "config"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "agent_profiles.json"

    def _read(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return []
        return [dict(x) for x in raw if isinstance(x, dict)] if isinstance(raw, list) else []

    def _write(self, rows: list[dict[str, Any]]) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    @classmethod
    def _normalize(cls, payload: dict[str, Any]) -> dict[str, Any]:
        agent_id = str(payload.get("agent_id") or "").strip()
        name = str(payload.get("name") or agent_id).strip()
        role = str(payload.get("role") or "generalist").strip()
        tier = str(payload.get("tier") or "cloud").casefold().strip()
        model = str(payload.get("model") or "").strip()
        skills_raw = payload.get("skills", [])
        perms_raw = payload.get("permissions", ["*"])
        if isinstance(skills_raw, str):
            skills_raw = [x.strip() for x in skills_raw.split(",") if x.strip()]
        if isinstance(perms_raw, str):
            perms_raw = [x.strip() for x in perms_raw.split(",") if x.strip()]
        skills = [str(x).strip() for x in skills_raw if str(x).strip()]
        permissions = [str(x).strip() for x in perms_raw if str(x).strip()]
        if not agent_id or not all(ch.isalnum() or ch in "-_" for ch in agent_id):
            raise ValueError("Agent ID 只能包含字母、数字、-、_")
        if not name or not role:
            raise ValueError("Agent 名称和角色不能为空")
        if tier not in cls.VALID_TIERS:
            raise ValueError("Agent 层级必须是 device / edge / cloud")
        if not skills:
            raise ValueError("至少配置一个 Skill")
        try:
            max_load = max(1, int(payload.get("max_load", 1)))
        except (TypeError, ValueError) as exc:
            raise ValueError("max_load 必须是整数") from exc
        return {
            "agent_id": agent_id, "name": name, "role": role,
            "skills": skills, "permissions": permissions or ["*"],
            "tier": tier, "model": model, "max_load": max_load,
            "enabled": bool(payload.get("enabled", True)), "updated_at": time.time(),
        }

    def public(self) -> dict[str, Any]:
        return {"agents": self._read(), "tiers": sorted(self.VALID_TIERS)}

    def save(self, payload: dict[str, Any]) -> dict[str, Any]:
        row = self._normalize(payload)
        rows = self._read()
        for i, current in enumerate(rows):
            if current.get("agent_id") == row["agent_id"]:
                rows[i] = row
                break
        else:
            rows.append(row)
        self._write(rows)
        return {"agent": row, "agents": rows}

    def delete(self, agent_id: str) -> dict[str, Any]:
        rows = self._read()
        kept = [x for x in rows if str(x.get("agent_id")) != str(agent_id)]
        if len(kept) == len(rows):
            raise KeyError(agent_id)
        self._write(kept)
        return {"deleted": agent_id, "agents": kept}


@dataclass(frozen=True)
class ExecutionEndpoint:
    endpoint_id: str
    name: str
    tier: str
    transport: str
    host: str
    port: int
    agent_id: str
    topic_prefix: str = "mosaic/v3"
    enabled: bool = True
    username: str = ""
    tls: bool = False


class ExecutionEndpointStore:
    VALID_TIERS = {"device", "edge", "cloud"}
    VALID_TRANSPORTS = {"mqtt"}

    def __init__(self, workspace: Path) -> None:
        self.directory = workspace / "config"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "execution_endpoints.json"
        self.secret_dir = self.directory / "endpoint_secrets"
        self.secret_dir.mkdir(parents=True, exist_ok=True)

    def _password_path(self, endpoint_id: str) -> Path:
        safe = "".join(ch for ch in endpoint_id if ch.isalnum() or ch in "-_")
        return self.secret_dir / f"{safe}.secret"

    def _read_password(self, endpoint_id: str) -> str | None:
        return _read_secret_file(self._password_path(endpoint_id))

    def _read(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return []
        return [dict(item) for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []

    def _write(self, rows: list[dict[str, Any]]) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    @classmethod
    def _normalize(cls, payload: dict[str, Any]) -> dict[str, Any]:
        endpoint_id = str(payload.get("endpoint_id") or "").strip()
        name = str(payload.get("name") or "").strip()
        tier = str(payload.get("tier") or "").strip().casefold()
        transport = str(payload.get("transport") or "mqtt").strip().casefold()
        host = str(payload.get("host") or "").strip()
        agent_id = str(payload.get("agent_id") or "").strip()
        username = str(payload.get("username") or "").strip()
        topic_prefix = str(payload.get("topic_prefix") or "mosaic/v3").strip().strip("/")
        try:
            port = int(payload.get("port", 1883))
        except (TypeError, ValueError) as exc:
            raise ValueError("端口必须是整数") from exc
        if not endpoint_id or not all(ch.isalnum() or ch in "-_" for ch in endpoint_id):
            raise ValueError("Endpoint ID 只能包含字母、数字、-、_")
        if not name:
            raise ValueError("执行节点名称不能为空")
        if tier not in cls.VALID_TIERS:
            raise ValueError("执行层级必须是 device / edge / cloud")
        if transport not in cls.VALID_TRANSPORTS:
            raise ValueError("当前只支持真实 MQTT 远程 Agent transport")
        if not host or not agent_id or not topic_prefix:
            raise ValueError("MQTT Broker、Agent ID、Topic Prefix 均不能为空")
        if not (1 <= port <= 65535):
            raise ValueError("端口范围必须为 1-65535")
        return {
            "endpoint_id": endpoint_id, "name": name, "tier": tier,
            "transport": transport, "host": host, "port": port,
            "agent_id": agent_id, "topic_prefix": topic_prefix,
            "enabled": bool(payload.get("enabled", True)),
            "username": username, "tls": bool(payload.get("tls", False)),
            "updated_at": time.time(),
        }

    def public_settings(self) -> dict[str, Any]:
        rows = []
        for row in self._read():
            item = dict(row)
            item["password_present"] = bool(self._read_password(str(row.get("endpoint_id"))))
            rows.append(item)
        return {"endpoints": rows, "supported_transports": ["mqtt"]}

    def summary(self) -> dict[str, Any]:
        rows = [row for row in self._read() if bool(row.get("enabled", True))]
        by_tier = {tier: 0 for tier in sorted(self.VALID_TIERS)}
        for row in rows:
            tier = str(row.get("tier", "")).casefold()
            if tier in by_tier:
                by_tier[tier] += 1
        return {"enabled_count": len(rows), "by_tier": by_tier, "remote_transport_available": bool(rows)}

    def save(self, payload: dict[str, Any]) -> dict[str, Any]:
        row = self._normalize(payload)
        password = str(payload.get("password") or "").strip()
        if password:
            _write_secret_file(self._password_path(row["endpoint_id"]), password)
        rows = self._read()
        for i, current in enumerate(rows):
            if str(current.get("endpoint_id")) == row["endpoint_id"]:
                rows[i] = row
                break
        else:
            rows.append(row)
        self._write(rows)
        result = dict(row)
        result["password_present"] = bool(self._read_password(row["endpoint_id"]))
        return {"endpoint": result, "summary": self.summary()}

    def delete(self, endpoint_id: str) -> dict[str, Any]:
        endpoint_id = str(endpoint_id or "").strip()
        rows = self._read()
        kept = [row for row in rows if str(row.get("endpoint_id")) != endpoint_id]
        if len(kept) == len(rows):
            raise KeyError(endpoint_id)
        self._write(kept)
        try:
            self._password_path(endpoint_id).unlink(missing_ok=True)
        except OSError:
            pass
        return {"deleted": endpoint_id, "summary": self.summary()}

    def get(self, endpoint_id: str) -> dict[str, Any]:
        endpoint_id = str(endpoint_id or "").strip()
        for row in self._read():
            if str(row.get("endpoint_id")) == endpoint_id:
                return dict(row)
        raise KeyError(endpoint_id)

    def _rpc(self, row: dict[str, Any]):
        from mosaic_omega.execution_scheduler.adapters.mqtt_agent import PahoMqttRpcClient
        return PahoMqttRpcClient(
            host=str(row["host"]), port=int(row["port"]),
            topic_prefix=str(row.get("topic_prefix") or "mosaic/v3"),
            username=str(row.get("username") or "") or None,
            password=self._read_password(str(row["endpoint_id"])),
            tls=bool(row.get("tls", False)),
        )

    def test_connection(self, endpoint_id: str, *, timeout_s: float = 8.0) -> dict[str, Any]:
        row = self.get(endpoint_id)
        if not bool(row.get("enabled", True)):
            raise RuntimeError("执行节点已停用")
        if row.get("transport") != "mqtt":
            raise RuntimeError("当前只实现 MQTT 远程 Agent 探测")
        started = time.perf_counter()
        rpc = self._rpc(row)
        try:
            response = rpc.request(str(row["agent_id"]), {
                "type": "PLAN_REQUEST", "schema_version": "0.1",
                "run_id": "endpoint-probe", "task_id": "endpoint-probe", "trace_id": "endpoint-probe",
                "payload": {
                    "task": {
                        "task_id": "endpoint-probe", "description": "MOSAIC endpoint connectivity probe",
                        "required_permissions": [],
                        "metadata": {"tool": {"name": "task", "arguments": {"description": "endpoint connectivity probe"}}},
                    },
                    "assignment": {"tool_id": "task"},
                },
            }, float(timeout_s))
        finally:
            rpc.close()
        latency_ms = (time.perf_counter() - started) * 1000.0
        calls = response.get("tool_calls", response.get("payload", {}).get("tool_calls", []))
        if not isinstance(calls, list):
            raise RuntimeError("远程 Agent 已响应，但返回协议不合法")
        return {
            "ok": True, "endpoint_id": row["endpoint_id"], "name": row["name"], "tier": row["tier"],
            "transport": "mqtt_request_reply", "agent_id": row["agent_id"],
            "latency_ms": round(latency_ms, 1), "tool_call_count": len(calls), "verified": True,
            "measurement_semantics": "measured_mqtt_request_reply",
            "security": {"tls": bool(row.get("tls", False)), "username_configured": bool(row.get("username"))},
            "verification_basis": "real MQTT PLAN_REQUEST / PLAN_RESPONSE",
        }

    def runtime_environment(self, endpoint_id: str) -> dict[str, str]:
        row = self.get(endpoint_id)
        if not bool(row.get("enabled", True)):
            raise RuntimeError("执行节点已停用")
        return {
            "MQTT_HOST": str(row["host"]), "MQTT_PORT": str(row["port"]),
            "MQTT_TOPIC_PREFIX": str(row.get("topic_prefix") or "mosaic/v3"),
            "MQTT_USERNAME": str(row.get("username") or ""),
            "MQTT_PASSWORD": str(self._read_password(endpoint_id) or ""),
            "MQTT_TLS": "true" if bool(row.get("tls", False)) else "false",
            "MOSAIC_REMOTE_AGENT_ID": str(row["agent_id"]),
            "MOSAIC_REMOTE_ENDPOINT_ID": str(row["endpoint_id"]),
            "MOSAIC_REMOTE_TIER": str(row["tier"]),
        }
