from __future__ import annotations

from types import SimpleNamespace

from mosaic_omega.execution_scheduler.adapters import deepseek_agent as deepseek_module
from mosaic_omega.integration import MosaicMainChain
from mosaic_omega.verifier import semantic as semantic_module


class _FakeCompletions:
    def __init__(self) -> None:
        self.count = 0

    def create(self, **kwargs):
        self.count += 1
        messages = kwargs.get("messages") or []
        is_verifier = any("independent acceptance verifier" in str(item.get("content", "")).casefold() for item in messages if isinstance(item, dict))
        if is_verifier:
            # The verifier now judges every acceptance condition of a task in one
            # batched request, so the stub must answer with one entry per id.
            import json as _json
            payload = _json.loads(messages[-1]["content"])
            content = _json.dumps({
                "judgments": [
                    {"id": item["id"], "passed": True, "rationale": "independent test fixture verified deliverable"}
                    for item in payload["acceptance_conditions"]
                ]
            }, ensure_ascii=False)
        else:
            content = "真实 API 适配器测试成果"
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
    provenances = [item["payload"]["tool_call"]["arguments"]["api_provenance"] for item in tool_events]
    # Every execution carries provenance. Device-tier deterministic Agents record
    # provider "local" with no network egress; everything else must be the
    # configured provider. There is no unlabeled third category.
    assert all(item["provider"] in {"deepseek", "local"} for item in provenances)
    provider_calls = [item for item in provenances if item["provider"] != "local"]
    local_calls = [item for item in provenances if item["provider"] == "local"]
    assert provider_calls
    # The requirement baseline is compiled on-device, so it must not consume a
    # provider round trip.
    assert local_calls
    assert all(item["network_egress"] is False for item in local_calls)

    from mosaic_omega.observability.snapshots import SnapshotStore
    snapshot = SnapshotStore(tmp_path / "workspace" / "observability").read_latest()
    assert snapshot["authenticity"]["verdict"] == "TEST_FIXTURE_NOT_COMPETITION_STRICT"
    assert snapshot["authenticity"]["competition_strict_real_agent"] is False
    usage = snapshot["authenticity"]["api_usage"]
    # Runtime authenticity usage tracks provider Agent calls only; local
    # deterministic executions are reported separately and never inflate token
    # or request counts. Verifier provenance stays under TASK_VERIFIED events.
    assert usage["request_count"] == len(provider_calls)
    assert usage["total_tokens"] == 28 * len(provider_calls)
    assert snapshot["authenticity"]["local_execution_count"] == len(local_calls)
    verification_events = [item for item in raw["events"] if item["type"] == "TASK_VERIFIED"]
    assert verification_events
    assert all(item["payload"]["verification"]["metadata"]["self_echo_acceptance_disabled"] for item in verification_events)
    assert snapshot["communication"]["token_usage"]["status"] == "measured_api_usage"
