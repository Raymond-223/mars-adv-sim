"""Minimal, dependency-free OpenAI-compatible chat-completions transport.

This is a real HTTP transport, not a deterministic/model fallback.  It exists so
competition deployments do not silently become "fake Agent" runs merely because
the optional ``openai`` Python SDK is unavailable.  When the official SDK is
available it remains the preferred transport; otherwise this module performs the
same network call through the Python standard library.
"""
from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
from types import SimpleNamespace
from typing import Any, Mapping


def _namespace_response(raw: Mapping[str, Any]) -> SimpleNamespace:
    """Normalize a JSON chat-completion response to the tiny SDK surface we use."""
    choices = []
    for choice in raw.get("choices", []) or []:
        message = choice.get("message", {}) if isinstance(choice, Mapping) else {}
        choices.append(
            SimpleNamespace(
                index=choice.get("index") if isinstance(choice, Mapping) else None,
                message=SimpleNamespace(
                    role=message.get("role") if isinstance(message, Mapping) else None,
                    content=message.get("content") if isinstance(message, Mapping) else None,
                ),
                finish_reason=choice.get("finish_reason") if isinstance(choice, Mapping) else None,
            )
        )
    usage = raw.get("usage", {})
    if not isinstance(usage, Mapping):
        usage = {}
    return SimpleNamespace(
        id=raw.get("id"),
        object=raw.get("object"),
        created=raw.get("created"),
        model=raw.get("model"),
        choices=choices,
        usage=dict(usage),
        _mosaic_raw=dict(raw),
        _mosaic_transport="stdlib_http",
    )


class _Completions:
    def __init__(self, owner: "StdlibOpenAICompatibleClient") -> None:
        self._owner = owner

    def create(self, **kwargs: Any) -> SimpleNamespace:
        return self._owner._create_chat_completion(kwargs)


class _Chat:
    def __init__(self, owner: "StdlibOpenAICompatibleClient") -> None:
        self.completions = _Completions(owner)


class StdlibOpenAICompatibleClient:
    """Small OpenAI-compatible client backed by ``urllib.request``.

    The public shape intentionally matches only ``client.chat.completions.create``
    because that is the only API surface MOSAIC-Ω consumes.
    """

    _mosaic_transport = "stdlib_http"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        timeout: float = 60.0,
        max_retries: int = 2,
    ) -> None:
        if not str(api_key).strip():
            raise ValueError("api_key must not be empty")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        self.api_key = str(api_key)
        self.base_url = str(base_url).rstrip("/")
        self.timeout = float(timeout)
        self.max_retries = int(max_retries)
        self.chat = _Chat(self)

    @property
    def endpoint(self) -> str:
        return f"{self.base_url}/chat/completions"

    def _create_chat_completion(self, kwargs: Mapping[str, Any]) -> SimpleNamespace:
        body = dict(kwargs)
        extra_body = body.pop("extra_body", None)
        if isinstance(extra_body, Mapping):
            # Match OpenAI SDK semantics: provider-specific body fields are merged
            # into the top-level JSON request.
            body.update(dict(extra_body))
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint,
            data=payload,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "MOSAIC-Omega/OpenAI-Compatible-Stdlib",
            },
        )
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310
                    raw_bytes = response.read()
                    status = int(getattr(response, "status", 200))
                if status < 200 or status >= 300:
                    raise RuntimeError(f"HTTP {status} from OpenAI-compatible API")
                try:
                    raw = json.loads(raw_bytes.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise RuntimeError("OpenAI-compatible API returned invalid JSON") from exc
                if not isinstance(raw, Mapping):
                    raise RuntimeError("OpenAI-compatible API response must be a JSON object")
                normalized = _namespace_response(raw)
                if not normalized.choices:
                    raise RuntimeError("OpenAI-compatible API response contains no choices")
                return normalized
            except urllib.error.HTTPError as exc:
                last_error = exc
                # Authentication / validation errors are deterministic and should
                # fail closed immediately rather than being hidden by retries.
                if 400 <= exc.code < 500 and exc.code != 429:
                    break
            except (urllib.error.URLError, TimeoutError, socket.timeout, RuntimeError) as exc:
                last_error = exc
            if attempt < self.max_retries:
                time.sleep(min(0.25 * (2**attempt), 1.0))
        raise RuntimeError(
            f"OpenAI-compatible HTTP request failed after {self.max_retries + 1} attempt(s): {last_error}"
        ) from last_error


def create_openai_compatible_client(
    *,
    api_key: str,
    base_url: str,
    timeout: float = 60.0,
    max_retries: int = 2,
    prefer_sdk: bool = True,
) -> Any:
    """Return a real network client and mark which transport is in use.

    No mock/deterministic fallback is performed here.  If the SDK cannot be
    imported, the returned stdlib transport still performs an actual HTTP call.
    """
    if prefer_sdk:
        try:
            from openai import OpenAI  # type: ignore

            client = OpenAI(
                api_key=api_key,
                base_url=base_url,
                timeout=timeout,
                max_retries=max_retries,
            )
            try:
                setattr(client, "_mosaic_transport", "openai_sdk")
            except Exception:
                pass
            return client
        except (ImportError, AttributeError):
            pass
    return StdlibOpenAICompatibleClient(
        api_key=api_key,
        base_url=base_url,
        timeout=timeout,
        max_retries=max_retries,
    )
