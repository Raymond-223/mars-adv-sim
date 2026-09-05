# Agent 真实性审计（v1.1）

## 结论

比赛展示中，只有 `authenticity.verdict == REAL_API_VERIFIED` 才允许称为“真实 API Agent 运行”。其余模式必须按实际类型展示：

| mode | 含义 | 可否作为真实 LLM Agent 证据 |
|---|---|---|
| `real_api` | 绑定 DeepSeek/OpenAI-compatible API adapter，失败直接抛错，不降级 | 只有每次实际执行都有 `api_provenance` 时可以 |
| `remote_rpc` | 绑定远程 MQTT Agent RPC | 可证明远程 Agent 绑定，但当前 UI 不把它等同 API 调用证明 |
| `deterministic_tool_executor` | 根据 TaskSpec 生成 ToolCall 的确定性执行器 | 不可以 |
| `mock` | 单元测试/开发 Mock | 不可以 |
| `api_with_explicit_fallback` | 开发模式允许显式 deterministic fallback | 不可以作为 competition-strict |
| `test_fixture` | 单元测试 fixture | 不可以 |
| `unbound` / `unclassified` | 无 adapter 或未分类 | 不可以 |

## 已封堵的假 Agent 路径

1. `MosaicMainChain.add_agent()` 不再自动绑定 `MemoryTopologyMockAgent`。没有显式 `adapter=` 时直接 `ValueError`。
2. `LLMAgentAdapter` 默认 `allow_fallback=False`。API client 缺失、网络/API 报错、返回非法 JSON都会失败并进入主链错误/恢复事件，不能静默生成 ToolCall。
3. 只有显式 `allow_fallback=True` 的开发 fixture 才允许 fallback，而且 ToolCall 中写入 `execution_provenance.mode=explicit_deterministic_fallback`，真实性判定为非严格真实 Agent。
4. `replace_model()` 不再只改 UI/Capability metadata。它必须调用当前真实 adapter 的 `set_model()`；不支持真实切模的 adapter 会直接拒绝。
5. `scripts/benchmark_real_api.py` 的 Chaos 路径不再注册 `MockAgent`；严格真实 API benchmark 使用 `agent_mode="deepseek"`。
6. 离线 Benchmark 明确标记为 `DETERMINISTIC_TOOL_EXECUTOR`，不再称作 real LLM Agent；生产 Console 无 `--demo` 启动模式。

## REAL_API_VERIFIED 判定条件

`src/mosaic_omega/observability/projections.py::_authenticity()` 同时检查：

- 当前任务实际 `assignment.agent_id`；
- 对应 Capability 的 `metadata.adapter_bound == true`；
- `metadata.authenticity_mode == real_api`；
- 本 Run 至少存在一个 `TOOL_EXECUTED`；
- 每个由 `real_api` Agent 执行的 `TOOL_EXECUTED` 都能在 `payload.tool_call.arguments.api_provenance` 找到真实调用 provenance；
- 不得出现 `mock`、`unbound` 或显式 fallback Agent。

真实调用证据字段：

```text
TOOL_EXECUTED.payload.tool_call.arguments.api_provenance.provider
TOOL_EXECUTED.payload.tool_call.arguments.api_provenance.model
TOOL_EXECUTED.payload.tool_call.arguments.api_provenance.request_id
TOOL_EXECUTED.payload.tool_call.arguments.api_provenance.usage.prompt_tokens
TOOL_EXECUTED.payload.tool_call.arguments.api_provenance.usage.completion_tokens
TOOL_EXECUTED.payload.tool_call.arguments.api_provenance.usage.total_tokens
TOOL_EXECUTED.payload.tool_call.arguments.api_provenance.base_url
```

API Key 不写入 provenance。

## 比赛现场规则

若控制台真实性状态不是 `REAL_API_VERIFIED`，答辩时不得描述为“真实大模型 Agent 已执行”。`MOCK_EXECUTION`、`DETERMINISTIC_TOOL_EXECUTOR`、`MIXED_OR_UNCLASSIFIED` 均是显式否定性状态，不允许用 UI 文案掩盖。
