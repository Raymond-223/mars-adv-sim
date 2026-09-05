# 面向超长程复杂任务的动态异构群体智能架构与深度协同推理技术
## 竞赛技术方案与核心算法伪代码申报书 (题目编号：XH-202631)

**发榜单位**：荣耀终端股份有限公司  
**作品名称**：MOSAIC-Ω 全域自治多智能体协同基座  

---

## 目录
1. [系统总体架构设计](#一-系统总体架构设计)
2. [算法一：基于语义瓶颈的动态稀疏拓扑路由算法](#二-算法一基于语义瓶颈的动态稀疏拓扑路由算法)
3. [算法二：触发式长程记忆保持与上下文唤醒算法](#三-算法二触发式长程记忆保持与上下文唤醒算法)
4. [算法三：端-边-云异构资源自适应调度与模型切分算法](#四-算法三端-边-云异构资源自适应调度与模型切分算法)
5. [算法四：基于因果 TaskGraph 的局部影响子图恢复算法](#五-算法四基于因果-taskgraph-的局部影响子图恢复算法)
6. [作品性能与评分标准对照分析](#六-作品性能与评分标准对照分析)

---

## 一、 系统总体架构设计

MOSAIC-Ω 专为解算数千步超长程非确定性任务而设计，彻底摒弃了传统多智能体系统中的“静态流水线”与“全连接无序广播通信”范式。系统采用单事实源（EventStore）驱动的图拓扑自治架构，分为六层体系：

```text
+-----------------------------------------------------------------------+
|  User Goal / High-level Intent (自然语言高阶意图输入)                 |
+-----------------------------------------------------------------------+
                                   |
                                   v
+-----------------------------------------------------------------------+
| 1. GoalSpec 编译层 (目标与硬/软约束解构，隐私等级/预算计算)           |
+-----------------------------------------------------------------------+
                                   |
                                   v
+-----------------------------------------------------------------------+
| 2. Dynamic ToDAG 因果生成层 (TaskGraph / 动态依赖计算)                |
+-----------------------------------------------------------------------+
                                   |
                                   v
+-----------------------------------------------------------------------+
| 3. 端-边-云自适应调度引擎 (Edge-Cloud Placement & Model Partitioning)  |
+-----------------------------------------------------------------------+
        |                                                 |
        v                                                 v
+-----------------------------------+   +-------------------------------+
| 4. 动态稀疏拓扑与低熵通信网络      |   | 5. 触发式长程记忆与唤醒服务    |
| (Topology Routing & Deduplication)|   | (Episodic/Procedural Memory)  |
+-----------------------------------+   +-------------------------------+
        |                                                 |
        +-------------------------+-----------------------+
                                  |
                                  v
+-----------------------------------------------------------------------+
| 6. EventStore 状态中心 & 局部因果恢复引擎 (Recovery Engine)           |
+-----------------------------------------------------------------------+
```

---

## 二、 算法一：基于语义瓶颈的动态稀疏拓扑路由算法

### 1. 数学建模
定义 Agent 节点集合为 $V = \{A_1, A_2, \dots, A_n\}$，在时刻 $t$ 的候选通信边集合为 $E_t \subseteq V \times V$。
通信边的边权由语义相关度 $S_{ij}$、任务依赖相关度 $D_{ij}$ 及历史通信信噪比 $R_{ij}$ 共同决定：
$$Score(A_i, A_j) = \alpha \cdot \cos(\vec{e}_i, \vec{e}_j) + \beta \cdot D_{ij} + \gamma \cdot R_{ij}$$
基于信息瓶颈（Information Bottleneck）限制，系统过滤掉低于阈值 $\tau$ 的噪声边，输出稀疏拓扑 $G_{sparse} = (V, E_{active})$。

### 2. 算法伪代码 (Pseudocode)

```python
Algorithm 1: DynamicSparseTopologyRouting
Input: AgentProfiles A, TaskMessage M, EnergyThreshold tau, Weights (alpha, beta, gamma)
Output: SparseRoutingGraph G_sparse, MessageActionMap ActionMap

1:  Function DynamicTopologyRoute(A, M, tau):
2:      G_sparse = InitializeGraph(nodes=A)
3:      ActionMap = {}
4:      For each (A_i, A_j) in CartesianProduct(A, A) do:
5:          If A_i == A_j: Continue
6:          sim_semantic = CosineSimilarity(A_i.embedding, M.semantic_vector)
7:          dep_factor = TaskDependencyScore(A_i.current_task, A_j.current_task)
8:          snr_history = HistoricalSNR(A_i.id, A_j.id)
9:          score = alpha * sim_semantic + beta * dep_factor + gamma * snr_history
10:         
11:         If score >= tau:
12:             AddEdge(G_sparse, A_i, A_j, weight=score)
13:             If IsDuplicateMessage(M, A_j.inbox):
14:                 ActionMap[A_j.id] = MESSAGE_MERGE
15:             Else:
16:                 ActionMap[A_j.id] = MESSAGE_SEND
17:         Else:
18:             ActionMap[A_j.id] = MESSAGE_DROP
19:     End For
20:     Return G_sparse, ActionMap
```

### 3. 复杂度分析
* **时间复杂度**：若 Agent 节点数为 $N$，寻找最优先路由的遍历开销为 $O(N^2)$。结合 Top-K 优先队列筛选后，时间复杂度优化为 $O(N \log K)$。
* **空间复杂度**：存储稀疏拓扑边及关联矩阵开销为 $O(|E_{active}|) \ll O(N^2)$。

---

## 三、 算法二：触发式长程记忆保持与上下文唤醒算法

### 1. 算法背景
在大模型处理数千步长链任务时，全局 Prompt 极易因滑动窗口挤压而产生“注意力稀释”与“记忆坍缩”。本算法通过定义“触发事件（Trigger Events）”（如意图变更、异常抛出、关键证据缺失），动态唤醒底层 Episodic / Procedural Memory。

### 2. 算法伪代码 (Pseudocode)

```python
Algorithm 2: TriggeredMemoryAwakening
Input: RunID, NodeID, TriggerEvent E_trig, WorkingMemory WM, MemoryRepo Repo, TokenBudget B_max
Output: ContextPack Context

1:  Function BuildContextWithAwakening(RunID, NodeID, E_trig, WM, Repo, B_max):
2:      Context = CreateEmptyContextPack(RunID, NodeID)
3:      
4:      # 1. 强制提取全局硬约束与主目标 (Un-truncatable core)
5:      Context.goal = WM.current_goal
6:      Context.hard_constraints = WM.active_constraints
7:      
8:      # 2. 触发式高权向量检索
9:      query = FormatAwakeningQuery(E_trig, WM.recent_failed_conditions)
10:     recalled_records = Repo.VectorSearch(query, limit=Repo.recall_limit)
11:     
12:     # 3. 分级归档与唤醒填充
13:     For record in recalled_records do:
14:         If record.is_prohibition or "hard_constraint" in record.tags:
15:             AppendUnique(Context.prohibitions, record.content)
16:         Else if record.type == PROCEDURAL and E_trig.is_failure:
17:             AppendUnique(Context.procedures, record.summary + "\n" + record.content)
18:         Else if record.type == EPISODIC:
19:             AppendUnique(Context.relevant_experiences, record.summary)
20:     End For
21:     
22:     # 4. 严格预算截断 (不删除目标与硬约束)
23:     Context = TruncateOptionalContent(Context, B_max)
24:     Context.token_estimate = EstimateTokens(Context)
25:     Return Context
```

### 3. 复杂度分析
* **时间复杂度**：向量检索为 $O(D \cdot \log M)$（其中 $D$ 为向量维度，$M$ 为记忆条目总数），格式化截断为 $O(C)$（其中 $C$ 为 ContextPack 元素个数）。总体运行时间为 $O(D \log M)$。
* **空间复杂度**：存储生成的 ContextPack 上下文空间开销为 $O(B_{max})$，被配置硬上界严格约束。

---

## 四、 算法三：端-边-云异构资源自适应调度与模型切分决策契约

### 1. 算法背景
工业级长程任务要求算力调度兼顾“数据敏感度等级（Privacy Level）”与“实时性要求（Latency Constraint）”。v1.9.0 当前代码会生成 `ExecutionTier + ModelPartitionDescriptor` 作为**可审计的调度/切分决策契约**。需要特别区分：在未配置真实端/边/云执行适配器时，该 descriptor 只证明“如何决策”，**不等价于已经完成物理模型层切分或跨三台设备的 Split Inference**；比赛现场若接入真实异构 endpoint，应由执行适配器额外写入实际 partition execution provenance 后再宣称物理切分执行。

v1.9.0 已实现 Execution Endpoint Registry 与 MQTT Remote Endpoint Acceptance：可将真实远程 Agent 绑定为 DEVICE / EDGE / CLOUD endpoint，并通过真实 MQTT request/reply 以及 OR-Tools Assignment → ToolRuntime → Verifier 联合链路生成 `REMOTE_RPC_SCHEDULER_VERIFIED`。这一 verdict 证明远程 Agent 真正被调度，但仍不等价于模型层级 Split Inference。

### 2. 算法伪代码 (Pseudocode)

```python
Algorithm 3: EdgeCloudAdaptivePlacementAndPartitioning
Input: TaskNode T, AgentPool Agent, DevicePool Device, ModelPool Model
Output: ExecutionTier Tier, ModelPartitionDescriptor PartitionDesc, Assignment BestAssign

1:  Function SelectPlacementAndPartition(T, Device):
2:      privacy = Lowercase(T.privacy_level)
3:      latency = T.max_latency_ms
4:      tokens = T.estimated_tokens
5:      
6:      # 1. 硬性隐私与延时约束过滤
7:      If privacy in ["secret", "restricted"]:
8:          Tier = (privacy == "secret") ? DEVICE : EDGE
9:          PartitionDesc = ModelPartitionDescriptor(policy=NONE, device_ratio=1.0)
10:         Return Tier, PartitionDesc
11:     
12:     If latency is not None and latency <= 200:
13:         Tier = Device.has_gpu ? DEVICE : EDGE
14:         PartitionDesc = ModelPartitionDescriptor(policy=FEATURE_OFFLOAD, split_layer=12)
15:         Return Tier, PartitionDesc
16:     
17:     # 2. 超高复杂度任务: 云端切分 Pipeline 拆层
18:     If tokens > 4000:
19:         Tier = CLOUD
20:         PartitionDesc = ModelPartitionDescriptor(policy=PIPELINE_SPLIT, split_layer=24, cloud_ratio=0.8)
21:         Return Tier, PartitionDesc
22:         
23:     Return EDGE, ModelPartitionDescriptor(policy=NONE)
```

### 3. 复杂度分析
* **时间复杂度**：假设候选 Agent、Model、Tool、Device 数量分别为 $N_A, N_M, N_T, N_D$，搜索最优组合的卡特兰积复杂度为 $O(N_A \cdot N_M \cdot N_T \cdot N_D)$。调度引擎引入按 Device 分组剪枝后降至 $O(N_D \cdot \max(N_A, N_M, N_T))$。
* **空间复杂度**：开销为 $O(N_D)$。

### 4. v1.9.0 真实性边界

- `execution_tier`、`partition_policy`、`partition_descriptor` 来自 Scheduler/CostModel 的决策结果，可追溯但属于**placement/partition planning**。
- 当前单机比赛包不把这些字段包装成“真实三机执行”或“真实模型层拆分已经发生”。
- Financial Research 场景用于验证 DEVICE/EDGE/CLOUD 的约束选择与 Agent 分配逻辑；如果比赛现场部署真实端、边、云 endpoint，应在 Adapter 层记录 endpoint/transport/partition execution provenance，再升级为物理异构执行证据。
- v1.9.0 另外提供真实跨进程 Reference MLP Pipeline Split：前段/后段由两个独立 Python 进程执行，记录 `activation_payload_bytes_measured`、stage latency、总耗时和数值等价误差；证据强制标记 `REFERENCE_MLP_NOT_LLM_SPLIT`，仅证明 split execution mechanism，**不等价于 LLM layer split**。

---

## 五、 算法四：基于因果 TaskGraph 的局部影响子图恢复算法

### 1. 算法伪代码 (Pseudocode)

```python
Algorithm 4: CausalSubgraphLocalRecovery
Input: RunID, FailedTaskID, ErrorClass Err, EventStore Events, RecoveryEngine Engine
Output: RecoveryPlan Plan

1:  Function ExecuteLocalRecovery(RunID, FailedTaskID, Err, Events):
2:      # 1. 沿着因果 TaskGraph 计算受影响后继节点子图 (Causal Subgraph)
3:      affected_nodes = []
4:      queue = Queue([FailedTaskID])
5:      visited = Set()
6:      
7:      While queue is not empty do:
8:          curr = queue.PopLeft()
9:          If curr in visited: Continue
10:         visited.Add(curr)
11:         affected_nodes.Append(curr)
12:         
13:         # 获取直系依赖子节点与 Evidence 依赖子节点
14:         children = Events.GetTaskDependents(RunID, curr)
15:         For child in children do:
16:             queue.Push(child)
17:     End While
18:     
19:     # 2. 映射恢复动作 (Retry / Replace / Rollback / Replan / SafeStop)
20:     action = MapErrorToAction(Err)
21:     Plan = RecoveryPlan(RunID, FailedTaskID, action, affected_nodes)
22:     
23:     # 3. 授权 EventStore 原子执行受影响子图的局部状态回滚与重执行
24:     Events.MarkNodesForReplan(RunID, affected_nodes)
25:     Return Plan
```

### 2. 复杂度分析
* **时间复杂度**：拓扑影响子图遍历采用广度优先搜索 (BFS)，遍历节点数 $|V_{sub}|$ 及边数 $|E_{sub}|$，复杂度为 $O(|V_{sub}| + |E_{sub}|) \ll O(|V_{total}| + |E_{total}|)$，实现高效局部恢复。
* **空间复杂度**：队列与 Access 集合开销为 $O(|V_{sub}|)$。

---

## 六、 作品性能与评分标准对照分析

| 评选维度的关键指标 | 比赛要求高分标准 | MOSAIC-Ω 实现支撑 |
| :--- | :--- | :--- |
| **系统基础与闭环 (15分)** | 数千步复杂任务自主闭环，无过程遗忘与规划幻觉 | 依赖 EventStore 权威状态源与 `TriggeredMemoryAwakening` 算法，无损保持全局目标与约束。 |
| **组织架构与协作 (15分)** | 摒弃静态组队与无序自由通信，建立明确分工 | 引入动态 AgentRole 注册与 `DynamicSparseTopologyRouting` 低熵通信网路。 |
| **应用创新性 (25分)** | 通用数字劳动力平台潜力，用户体验友好 | 部署交互式 `apps/console` 比赛控制台：任务启动、场景实验、故障注入、Benchmark 与交付物操作进入独立控制面；运行事实仍由只读 Snapshot 投影展示推理、拓扑、证据与恢复轨迹。 |
| **技术创新性 (20分)** | 高信噪比通信，核心算法具备底层标准潜力 | 提出信息瓶颈拓扑路由与端-边-云 Model Partitioning 切分决策算法。 |
| **系统性能与效率 (15分)** | 具备自主纠错能力，高 Token 与计算资源利用率 | `CausalSubgraphLocalRecovery` 实现局部重规划；自适应调度优化计算成本与 Latency。 |

---

## 七、 v1.9.0 当前量化评测与消融实验数据报告

本节只列出 **v1.9.0 当前包中可追溯的测量口径**。离线确定性 Benchmark 用于验证 EventStore、Memory、Topology、Recovery 与长程调度链的规模稳定性；它不被包装成真实 LLM Agent。公网 DeepSeek + OR-Tools 的当前代码联合实跑，必须在比赛电脑执行 `scripts/final_runtime_acceptance.py` 并达到 `CURRENT_STRICT_VERIFIED` 后才能对外宣称。

### 1. 1000+ Event 单一权威 Run 长程 Benchmark

权威结果文件：`experiments/results/benchmark_1000_events_v3_monolithic.json`。

| 评测维度 | 当前实测 | 口径 |
| :--- | :--- | :--- |
| **Run ID** | **bench-long-monolithic-001** | 所有计数来自同一个 EventStore `run_id` |
| **Event 总数** | **1,070** | 单一权威 Run 的 EventStore 实测；故障前已有 897 Event |
| **任务节点总数** | **64** | 16 stage × 4 lane 的单一 DAG |
| **完成任务数** | **64/64** | 最终 Task 状态实测，`all_succeeded_measured=true` |
| **Evidence 失效注入** | **1 次** | 对同一 Run 的已生成 Evidence 执行真实 invalidation |
| **受影响闭包** | **8 个任务** | RecoveryEngine 计算的 `affected_task_ids` |
| **恢复成功** | **1/1** | 受影响节点经重新执行回到 SUCCEEDED |
| **消息数** | **67** | 同一 Run 的 runtime communication records 实测 |
| **消息体积** | **109.07 KB** | 同一 Run 消息序列化体积实测 |
| **Token equivalent** | **45,130（估算）** | `token_metric_is_estimate=true`，**不是 DeepSeek API billing token** |
| **总运行耗时** | **28.002 s** | wall-clock 实测 |
| **平均 Event 延迟** | **27.95 ms（派生）** | `total_duration / achieved_event_count` |
| **存储落盘** | **23.69 MB** | Benchmark workspace 文件实测 |

该 Benchmark 的 `measurement_mode=single_authoritative_eventstore_run_with_deterministic_executor_and_evidence_recovery`，并且显式记录 `executor_truth_class=DETERMINISTIC_TOOL_EXECUTOR`、`competition_real_api=false`。因此它用于证明 **EventStore / Scheduler / Memory / Topology / Verifier / Recovery 的单 Run 长程稳定性**，不能被表述成 1,070 次公网 DeepSeek 调用。

### 2. 动态拓扑通信 Replay 对照

权威结果文件：`experiments/results/topology_ablation_v2_replay.json`。对照使用同一实际捕获的 sparse message set，再计算不同拓扑 fan-out 成本；因此 Full Mesh / Static Star 是 **replay-derived**，不是三个物理网络分别实跑。

| 拓扑模式 | Transmission | Message Bytes | Token equivalent | 相对 Full Mesh Transmission 降低 | 相对 Full Mesh Bytes 降低 |
| :--- | ---: | ---: | ---: | ---: | ---: |
| Full Mesh replay | 20 | 39,614 | 9,904（估算） | 0% | 0% |
| Static Star replay | 20 | 39,614 | 9,904（估算） | 0% | 0% |
| **MOSAIC Sparse runtime set** | **10** | **19,807** | **4,952（估算）** | **50.0%** | **50.0%** |

源 runtime：3 个独立采样 Run，成功率 100%，捕获 6 条实际消息，活跃 Agent 数 3。这里的 token equivalent 仍为估算，不得与 DeepSeek `response.usage.total_tokens` 混用。

### 3. 故障实验口径

v1.9.0 提供四个可操作故障入口：Agent Offline、Tool Failure、Requirement Change、Evidence Invalidation。**当自定义任务/场景正在运行时**，Console 不再启动第二个“假故障任务”，而是原子写入该 Run 的 `LiveFaultMailbox`；运行中的 MainChain 只在下一 execution round 边界消费请求，并把真实注入动作写入 EventStore。**没有活动 Run 时**才启动独立可复现实验 subprocess。前端从不直接改任务状态。

- Tool Failure：在 `ToolRuntime` 边界武装“下一次工具调用失败”，下一次真实 ToolCall 返回 `RETRYABLE` failed `ExecutionResult`，随后由未修改的 Orchestrator 正常进入 `RecoveryEngine`。控制审计已实际得到 `FAULT_INJECTED → RECOVERY_PLANNED → TASK_RECOVERED → 最终成功`。
- Agent Offline：真实修改 Registry online 状态，后续 Scheduler 必须排除离线 Agent，并在有备用注册 Agent 时重新分配；**不会为了好看伪造 `TASK_RECOVERED`**。
- Requirement Change：自定义任务在 round 边界停止旧 Run，保留旧 EventStore，再基于新增约束重新编译新 GoalSpec/DAG 为新 Run；预置场景运行中若不能安全完成该语义则 Fail-Closed 拒绝。
- Evidence Invalidation：只使用已经真实产生的 Evidence ID，通过 `invalidate_evidence` 计算影响闭包并局部重规划。
- 离线长程 Benchmark 当前另有 **Evidence Invalidation 1/1 恢复成功**的规模实验。涉及公网 DeepSeek 的最终故障成绩仍须在比赛电脑 fresh run 后再填写。

### 4. 当前公网 DeepSeek + OR-Tools 对外宣称门槛

只有当前代码 fresh run 同时满足：

```text
REAL_API_VERIFIED
ORTOOLS_VERIFIED
```

并且 `evidence/final_runtime_acceptance.json` 为 `CURRENT_STRICT_VERIFIED`，才允许把该次运行作为 v1.9.0 公网 DeepSeek + OR-Tools 联合实跑证据。

---

*(本申报技术文档由 MOSAIC-Ω 研发团队整理，对应开源代码库完整实现。)*

