# Console 屏幕数据血缘总表

原则：每个屏幕数字必须属于四类之一：**原始字段、明确公式、配置/先验、估算值**。缺少数据时显示 `— / UNMEASURED / 暂无样本`，不补假值。

## 总览

| 屏幕项 | 后端字段 | 计算/来源 |
|---|---|---|
| Run Status | `snapshot.run.status` | 任务 projection 聚合：全 SUCCEEDED→SUCCEEDED；任一 FAILED→FAILED；终态 PAUSED→PAUSED；否则存在事件→RUNNING；无事件→CREATED |
| 任务成功率 | `run.success_rate` | `count(task.state==SUCCEEDED) / count(tasks)` |
| 成功数/总数 | `run.succeeded / run.task_count` | 任务 projection 计数 |
| 端到端时延 | `run.e2e_latency_ms` | `(max(events.timestamp)-min(events.timestamp))*1000`；这是本 Run **事件跨度**，不是单次 API latency |
| Events | `run.event_count` | `len(EventStore events)` |
| Evidence | `run.evidence_count` | `sum(len(task.evidence))` |
| Messages | `run.message_count` | `len(communication_decisions)`，没有 decisions 时才用 envelopes |
| online registered execution units | `run.active_agent_count` | `kind=agent && online!=false` 的 Capability 数量；不等于真实 LLM 数量 |
| 进度环 | `run.succeeded/run.task_count` | 与成功率同一公式，无独立数据 |
| 各状态数量 | `task_graph.status_counts` | `Counter(tasks[].state)` |
| Phase | `snapshot.phase` | ObservabilityRuntime 写 snapshot 时传入的阶段名 |
| λ2/connectivity | `topology.lambda2_or_connectivity` | TopologySnapshot 后端输出，不由 UI 构造 |
| 通信动作 | `communication.action_counts` | `Counter(policy_action)` |
| Memory records | `memory.record_count` | MemoryService observability records 数量 |
| ContextPack | `memory.context_pack_count` | 当前 `context_packs` 数量 |
| Recovery events | `recovery.event_count` | 从 EventStore 过滤 recovery/replan/rollback/safe-stop/error/failed 相关事件 |

## TaskGraph

节点 ID、description、risk、priority、assignment、acceptance、evidence count、attempt 都来自 Task projection。

### 节点状态

当前状态：`task.state/status`。

进入该状态的时间/事件：`task_graph.nodes[].status_provenance`，算法是在 EventStore 中查该 task 最近的：

- `TASK_STATE_CHANGED` 且 `payload.to == current_status`；
- 或 `TASK_RECOVERED` / `TASK_REPLANNED` 且 `payload.to == current_status`；
- CREATED 则使用 `TASK_CREATED`。

因此可以回答“后端什么时候进入这个状态、由谁触发、原因是什么、对应 event_id 是什么”。

正常状态链：`CREATED → PLANNED → READY → RUNNING → VERIFYING → SUCCEEDED`。失败、暂停、恢复和重规划都通过 EventStore 显式事件完成。

## 动态拓扑

| 屏幕项 | 后端字段 | 公式/说明 |
|---|---|---|
| Nodes | `len(topology.nodes)` | TopologySnapshot 节点数量 |
| Edges | `len(topology.edges)` | TopologySnapshot 边数量 |
| λ2/Connected | `topology.lambda2_or_connectivity` | 后端连通性保护输出 |
| Rebuild P95 | `topology.telemetry.topology_recovery_ms.p95` | telemetry 聚合字段；不存在则 `—` |
| Edge width | `edge.score` 或 `edge_scores[source->target]` | UI width=`1+max(0,score)*3`；score 不存在则固定 1px，**不设默认 score** |
| Edge score | EdgeScorer | `0.40*dependency_strength + 0.30*information_value + 0.20*reliability + 0.10*latency_score`，round 6 |

注意：EdgeScorer 中部分信号可以来自配置值，因此 score 是**模型派生值**，不能宣传为网络实测 RTT。

## 低熵通信

消息数、SEND/MERGE/DEFER/DROP 都来自 policy decision 的 `policy_action`。

真实模型 Token 只认：

```text
TOOL_EXECUTED.payload.tool_call.arguments.api_provenance.usage.prompt_tokens
...completion_tokens
...total_tokens
```

页面显示 total = 对具有 numeric usage 的实际 API 调用求和。若没有 API usage，显示 `INSUFFICIENT DATA`，不会用字符数代替。

Queue Wait P95、MQTT RTT P95 只读 topology telemetry；无样本为 `—`。

## Scheduler

Assignment 卡片字段：`tasks[].assignment`，包括 `agent_id/model_id/tool_id/resource_id/policy/reason/total_cost/cost_breakdown/execution_tier/partition_policy`。

Avg Cost = `mean(assignments[].total_cost)`；它是**调度代价分数，不默认代表人民币/美元**。

单个 Assignment 总成本由 `execution_scheduler/cost_model.py`：

```text
fixed = Σ fixed_cost
latency = max(profile.latency_ms)/1000 * weight_latency
token = task.estimated_tokens * Σ cost_per_token * weight_token
energy = Σ energy_cost * weight_energy
reliability = mean(posterior reliability 或 configured reliability)
failure = (1-reliability) * weight_failure
migration = weight_migration（发生资源迁移时）
load = device.current_load * 2
placement_mismatch = 0.25（仅推断 tier 不匹配时）
total_cost = 上述 breakdown 求和
```

### Capability Online

`capability.online`；注册、heartbeat、offline 会更新：

```text
metadata.status_updated_at
metadata.status_source
```

### Reliability

没有 posterior 样本时明确标为 `configured prior`。有 posterior 时展示 posterior 信息，不能把 0.95/0.99 配置值称作实测成功率。

### Latency

只有 `metadata.latency_measurement_count > 0` 才展示毫秒值，否则 `UNMEASURED`。过去默认 `0 ms` 容易被误解为实测，现已封堵。

## Memory

Memory layer 数量来自记录类型计数；Working 由于是临时 ContextPack projection，显示值是 `max(persisted working count, active context pack count)`，属于明确 projection 而非持久化记录数。

Memory 四项率/延迟同时读取 sample denominator：

- Recall rate 有 `recall_expected > 0` 才显示；
- Recall latency 有 `recall_requests > 0` 才显示；
- Restore consistency 有 `snapshot_consistency_checks > 0` 才显示；
- Invalidation accuracy 有 `invalidation_checks > 0` 才显示。

无样本显示 `暂无样本`，不把默认 `0.0` 误报成 0% 性能。

ContextPack Token 是**估算**：`estimate_tokens(text)=max(1,(len(text)+1)//2)`（空字符串为0）。

```text
context_pack_token_estimate = Σ ContextPack.token_estimate
full_history_token_estimate = Σ full_history_token_estimate
estimated reduction = 1 - context_pack/full_history
```

不与 DeepSeek API 的真实 tokenizer usage 混为一谈。

## Recovery

- Recovery Events = EventStore recovery/error/replan 类事件数量；
- Affected Nodes = `payload.affected_task_ids` 的集合并集；
- Unaffected Nodes = `task_count - affected_nodes`，**仅在确实有 recovery event 时显示**；没有恢复事件显示 `—`，不再硬编码“保留”。
- Latest Action = recovery timeline 最后一条真实 event type。

## Evidence / Verification

Evidence 表字段直接来自 task evidence manifest：`evidence_id/node_id/producer/verification_status/hash`。

Verified/Rejected = 对 `verification_status` 的精确计数。

Verification 卡片来自 `TASK_VERIFIED.payload.verification`，不是前端推断。

## Events / Traces

事件时间、类型、节点、actor、trace、parent 全部是 EventStore 字段。

Trace duration = 对同一 `trace_id` 的 `max(timestamp)-min(timestamp)`。

## 顶栏

`SNAPSHOT FRESH/STALE`：浏览器计算 `now - snapshot.generated_at`，阈值 5 秒。它只描述 snapshot freshness，**不代表 Agent 是否在线/是否推理**。

真实性 Badge：`snapshot.authenticity.verdict`。比赛真实 API 展示必须为 `REAL_API_VERIFIED`。
