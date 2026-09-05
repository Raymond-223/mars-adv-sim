from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from mosaic_omega.integration import MosaicMainChain


class _DeepSeekStubHandler(BaseHTTPRequestHandler):
    request_count = 0

    def log_message(self, *_args):
        return

    def do_POST(self):  # noqa: N802 - stdlib handler API
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        type(self).request_count += 1
        request_id = f"stub-request-{type(self).request_count}"
        messages = body.get("messages") or []
        is_verifier = any(
            "independent acceptance verifier" in str(item.get("content", "")).casefold()
            for item in messages if isinstance(item, dict)
        )
        if is_verifier:
            content = json.dumps(
                {"passed": True, "rationale": "stub independently verified the persisted deliverable"},
                ensure_ascii=False,
            )
        elif body.get("response_format") == {"type": "json_object"}:
            content = json.dumps(
                {
                    "main_goal": {
                        "goal_text": "生成可验证的真实 API 主链测试成果",
                        "goal_type": "analysis",
                        "domain": "多智能体任务编排",
                    },
                    "hard_constraints": [
                        {
                            "constraint": "必须保留执行证据",
                            "checkable": True,
                            "check_method": "content_check",
                        }
                    ],
                    "soft_preferences": [],
                    "acceptance_conditions": [
                        {
                            "condition": "输出包含执行证据",
                            "check_type": "content_check",
                            "expected_result": "存在执行证据",
                        }
                    ],
                    "budget": {},
                    "prohibitions": [],
                },
                ensure_ascii=False,
            )
        else:
            content = "已完成当前节点，并生成执行证据。"
        response = {
            "id": request_id,
            "object": "chat.completion",
            "created": int(time.time()),
            "model": body.get("model", "deepseek-chat"),
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 30, "completion_tokens": 12, "total_tokens": 42},
        }
        raw = json.dumps(response, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def test_real_openai_compatible_http_path_for_goal_and_agents(tmp_path, monkeypatch):
    _DeepSeekStubHandler.request_count = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _DeepSeekStubHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setenv("DEEPSEEK_API_KEY", "stub-key")
        monkeypatch.setenv("DEEPSEEK_BASE_URL", f"http://127.0.0.1:{server.server_port}/v1")
        monkeypatch.setenv("DEEPSEEK_TIMEOUT_S", "5")
        monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
        monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
        chain = MosaicMainChain(workspace=tmp_path / "workspace", scheduler_policy="greedy")
        result = chain.run(
            "调用真实兼容 API 完成多智能体任务并保留执行证据。",
            run_id="deepseek-http-e2e",
            goalspec_mode="deepseek",
            agent_mode="deepseek",
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert result.all_succeeded
    assert result.runtime_metadata == {
        "goalspec_mode": "deepseek",
        "agent_mode": "deepseek",
        "api_provider": "deepseek",
    }
    tool_events = [item for item in result.events if item["type"] == "TOOL_EXECUTED"]
    verification_events = [item for item in result.events if item["type"] == "TASK_VERIFIED"]
    semantic_checks = sum(
        int(item["payload"]["verification"].get("metadata", {}).get("semantic_check_count", 0))
        for item in verification_events
    )
    assert _DeepSeekStubHandler.request_count == len(tool_events) + 1 + semantic_checks
    assert all(
        item["payload"]["tool_call"]["arguments"]["api_provenance"]["request_id"]
        for item in tool_events
    )

    # A localhost-compatible endpoint proves the HTTP path but must never be
    # presented to competition judges as an official DeepSeek production run.
    snapshot = json.loads(
        (tmp_path / "workspace" / "observability" / "latest.json").read_text(encoding="utf-8")
    )
    assert snapshot["authenticity"]["verdict"] == "API_TEST_ENDPOINT_NOT_COMPETITION_STRICT"
    assert snapshot["authenticity"]["competition_strict_real_agent"] is False
