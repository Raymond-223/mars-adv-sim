from __future__ import annotations

from types import SimpleNamespace

from mosaic_omega.execution_scheduler.adapters import deepseek_agent as deepseek_module
from mosaic_omega.verifier import semantic as semantic_module
from mosaic_omega.integration import MosaicMainChain


class _FakeCompletions:
    def __init__(self) -> None:
        self.count = 0

    def create(self, **kwargs):
        self.count += 1
        messages = kwargs.get("messages") or []
        is_verifier = any("independent acceptance verifier" in str(item.get("content", "")).casefold() for item in messages if isinstance(item, dict))
        content = (
            '{"passed": true, "rationale": "independent test fixture verified deliverable"}'
            if is_verifier else "真实 API 适配器测试成果"
        )
        return SimpleNamespace(
            id=f"fake-request-{self.count}",
            model=kwargs["model"],
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
            usage=SimpleNamespace(
                prompt_tokens=20,
                completion_tokens=8,
                total_tokens=28,
            ),
        )


class _FakeClient:
    _mosaic_transport = "test_fixture"

    def __init__(self) -> None:
        self.chat = SimpleNamespace(completions=_FakeCompletions())


def test_deepseek_agent_mode_has_provenance_and_no_mock(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key-not-a-secret")
    shared_client = _FakeClient()
    monkeypatch.setattr(deepseek_module, "create_openai_compatible_client", lambda **_: shared_client)
    monkeypatch.setattr(semantic_module, "create_openai_compatible_client", lambda **_: shared_client)

    chain = MosaicMainChain(workspace=tmp_path / "workspace", scheduler_policy="greedy")
    result = chain.run(
        "生成可验证的 API 接入测试结果，必须包含测试证据。",
        run_id="deepseek-adapter-test",
        goalspec_mode="rule",
        agent_mode="deepseek",
    )

    raw = result.to_dict()
    assert result.all_succeeded
    assert raw["runtime_metadata"]["agent_mode"] == "deepseek"
    actor_ids = [item["actor_id"] for item in raw["capability_profiles"]]
    assert any(value.startswith("agent-deepseek-") for value in actor_ids)
    assert "mock-model" not in actor_ids
    tool_events = [item for item in raw["events"] if item["type"] == "TOOL_EXECUTED"]
    assert tool_events
    assert all(
        item["payload"]["tool_call"]["arguments"]["api_provenance"]["provider"]
        == "deepseek"
        for item in tool_events
    )

    import json
    snapshot = json.loads((tmp_path / "workspace" / "observability" / "latest.json").read_text(encoding="utf-8"))
    assert snapshot["authenticity"]["verdict"] == "TEST_FIXTURE_NOT_COMPETITION_STRICT"
    assert snapshot["authenticity"]["competition_strict_real_agent"] is False
    usage = snapshot["authenticity"]["api_usage"]
    # Runtime authenticity usage tracks Agent calls; verifier provenance is
    # recorded independently under TASK_VERIFIED events.
    assert usage["request_count"] == len(tool_events)
    assert usage["total_tokens"] == 28 * len(tool_events)
    verification_events = [item for item in raw["events"] if item["type"] == "TASK_VERIFIED"]
    assert verification_events
    assert all(item["payload"]["verification"]["metadata"]["self_echo_acceptance_disabled"] for item in verification_events)
    assert snapshot["communication"]["token_usage"]["status"] == "measured_api_usage"
