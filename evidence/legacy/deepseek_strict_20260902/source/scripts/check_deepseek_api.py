#!/usr/bin/env python3
"""Small, non-destructive DeepSeek API connectivity check."""
from __future__ import annotations

import json
import os
import sys
import time

try:
    from openai import OpenAI
except ImportError:
    print('ERROR: 缺少 openai SDK。请运行：py -m pip install -e ".[deepseek]"', file=sys.stderr)
    raise SystemExit(2)

try:
    from dotenv import load_dotenv
except ImportError:
    print('ERROR: 缺少 python-dotenv。请运行：py -m pip install -e ".[deepseek]"', file=sys.stderr)
    raise SystemExit(2)


def main() -> int:
    load_dotenv()
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        print("ERROR: 当前 PowerShell 会话没有 DEEPSEEK_API_KEY", file=sys.stderr)
        return 2
    model = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash").strip() or "deepseek-v4-flash"
    base_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
    try:
        timeout_s = float(os.getenv("DEEPSEEK_TIMEOUT_S", "60"))
    except ValueError:
        print("ERROR: DEEPSEEK_TIMEOUT_S 必须是数字", file=sys.stderr)
        return 2

    client = OpenAI(
        api_key=api_key,
        base_url=base_url,
        timeout=timeout_s,
        max_retries=1,
    )
    started = time.perf_counter()
    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "只输出 API_OK"},
                {"role": "user", "content": "连通性测试"},
            ],
            max_tokens=16,
            temperature=0,
            extra_body={"thinking": {"type": "disabled"}},
        )
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 3

    usage = getattr(response, "usage", None)
    if hasattr(usage, "model_dump"):
        usage = usage.model_dump()
    elif usage is None:
        usage = {}
    elif not isinstance(usage, dict):
        usage = {
            key: getattr(usage, key)
            for key in ("prompt_tokens", "completion_tokens", "total_tokens")
            if getattr(usage, key, None) is not None
        }
    content = response.choices[0].message.content or ""
    report = {
        "ok": bool(content.strip()),
        "provider": "deepseek",
        "model": getattr(response, "model", None) or model,
        "request_id": getattr(response, "id", None),
        "latency_ms": round((time.perf_counter() - started) * 1000, 1),
        "usage": usage,
        "reply": content.strip()[:80],
        "api_key_logged": False,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 4


if __name__ == "__main__":
    raise SystemExit(main())
