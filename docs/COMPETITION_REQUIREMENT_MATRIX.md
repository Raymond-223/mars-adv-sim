# XH-202631 比赛要求—v1.9.0 实现—证据矩阵

> 原则：**实现、当前 Run 证据、外部金标实测三者分开。** 代码存在不等于已在比赛电脑完成实跑。

| 赛题能力 | v1.9.0 实现 | 当前证据入口 | 真实性边界 |
|---|---|---|---|
| 无人工干预长程闭环 | GoalSpec → ToDAG → Scheduler → Agent → ToolRuntime → Evidence → Verifier → Recovery | EventStore / Snapshot / result JSON | 通用自然语言 acceptance 不再允许自证；缺独立验证时 Fail-Closed |
| 长程记忆 | MemoryService / ContextPack / 压缩与唤醒 | Memory projection / ContextPack | context token 与 reduction 都是 ESTIMATED；API usage 另算 |
| 动态异构拓扑 | MessageTopologyService 动态 rebuild | runtime topology snapshots | UI 只画 snapshot 中真实 edge |
| 低熵通信 | sparse routing + SEND/MERGE/DROP/DEFER | communication decision log | Full Mesh/Star 对照为 replay fan-out，不冒充物理网络独立实测 |
| 端边云调度 | ExecutionTier + CostModel + privacy/latency filters | Assignment `recommended_tier` + `actual_execution_tier` | 推荐层级与实际执行层级分离；fallback 单独记录 |
| 模型切分机制 | 跨进程 Reference MLP Pipeline Split | split benchmark JSON：stage latency / activation bytes / equivalence | `REFERENCE_MLP_NOT_LLM_SPLIT`；不宣称 DeepSeek/LLM layer split |
| 真实远程端/边/云节点 | MQTT Agent Adapter + Execution Endpoint Registry | request/reply probe + Remote Acceptance Run | 配置本身不是实跑；只有远程 RPC + authoritative chain 成功才是证据 |
| 动态异常 | LiveFaultMailbox → Registry/ToolRuntime/Recovery | Agent offline / tool failure / requirement change / evidence invalidation | UI 不直接改任务状态；只有后端确认才 APPLIED |
| 需求变更 | changed GoalSpec → recompile DAG → new execution | EventStore / fault result | 不支持的组合 Fail-Closed |
| 节点失效 | Registry offline + Scheduler 排除 + alternate Agent | registry/topology/assignment events | RecoveryEngine 未发生就不显示 recovery |
| 2+ 跨领域任务 | ROS Repair + Financial Research | 两个独立 scenario runner | Financial 使用可复现 fixture 数据，不冒充实时金融源 |
| 1000+ 规模 | 单一权威长程 benchmark | benchmark result | deterministic executor；Events ≠ LLM Calls |
| 多模型兼容 | DeepSeek / OpenAI-compatible / Ollama Provider | Settings API + real connection test | 接口支持与各 Provider 金标实跑严格分开；secret 按 Provider 隔离 |
| 灵活增删 Agent | Agent Studio 持久化模板 → 新 Run Capability Registry | `/api/settings/agents` + runtime capabilities | 当前运行中 Run 不被 UI 热改写 |
| 中间过程可视化 | DAG / collaboration / memory / actual tier flow | authoritative snapshot | 动画由新事件/状态变化触发；Task DAG 箭头只来自真实 edges |
| 用户易用性 | 工作台 / 任务 / 结果 / 智能体 + 技术透视 + 设置；模板先填充再由用户确认启动 | frontend + control plane | 工作主线保持简洁；数学、算法与 provenance 在技术透视中按需下钻 |
| 最终交付 | 受控 artifact roots + preview/download | `/api/control/artifacts` / preview / download | 前端不接收服务器绝对路径；Evidence hash 文件以语义标题呈现 |
| 数据可追溯 | data lineage / state contract / interaction contract | 技术透视 + Lineage views | 未测量显示未测量；Estimated 明确标记 |
| 长周期恢复 | SQLite durable EventStore + resume_run | restart/resume tests | 未知副作用执行结果进入 PAUSED/SAFE STOP，不盲目重复 |
| 隐私与部署安全 | DTO redaction + per-provider secret + MQTT auth | settings/control tests + Docker config | 明确设置页可看到用户主动填写的 endpoint；技术透视/Observatory 不展示 |

## 仍需比赛电脑完成的外部金标证据

1. 配置真实 Provider 后完成 fresh public-model run，保存 request id / usage / ToolRuntime / Verifier / OR-Tools provenance。
2. 若主张物理端—边—云跨设备协同，至少连接真实远程 MQTT Agent，并完成联合验收。
3. 若主张“LLM 层级模型切分”，必须另接真正可拆分模型。Reference MLP 只能证明 split execution mechanism。
4. ROS Repair 与 Financial Research 各完成一次高完成度真实运行并保留 Evidence / Verifier / Final Deliverable。

## 禁止的概念偷换

- `1153 Events` 不得写成 `1153 LLM Calls`；当前长程基准使用 deterministic tool executor。
- `ESTIMATED token reduction` 不得写成真实 API token 节省。
- `recommended_tier` 不得写成 `actual_execution_tier`。
- MQTT `PLAN_RESPONSE` 不得写成远端工具已经执行；工具是否远端执行必须看执行 provenance。
- `REFERENCE_MLP_NOT_LLM_SPLIT` 不得写成真实大语言模型 Split Inference。
