# MOSAIC-Ω V3 v1.9.0 当前验收报告

本文件只描述 **v1.9.0 当前代码与当前重新生成证据**，不继承旧版本验收数字。

## 1. 产品形态

主导航统一为 **工作台 / 任务 / 结果 / 智能体 / 技术透视 / 设置**。工作主线强调目标、执行进度和交付物；技术透视按真实 Run 下钻运行全景、Dynamic DAG、优化调度、协作拓扑、低熵通信、四层记忆、独立审核、故障恢复、验证实验和数据血缘 10 个深层面板，不再使用比赛临时入口式的信息架构。

UI 使用浅灰背景、白色工作区和单一蓝色强调色；绿/黄/红仅承担成功、警告、失败语义。

## 2. 关键工程修复

- SSE/增量刷新替代旧的全页面高频轮询；设置类数据不进入热刷新路径。
- READY DAG 节点支持并发执行，受 Agent capacity、Provider concurrency、DEVICE/EDGE/CLOUD capacity 与依赖关系共同约束。
- 观测快照节流检查改为线程安全原子门禁，避免并发 Agent 同时重建多份大型 Snapshot。
- SQLite 本地权威库复用进程内连接，WAL 默认 `synchronous=NORMAL`；需要更强掉电持久性时仍可选择 FULL/EXTRA。
- 确定性验收条件优先使用 predicate；只有真正语义型条件才调用独立语义 Verifier。
- Recovery 重执行结束后强制发布最终 authoritative snapshot，避免 UI 停留在中间恢复百分比。

## 3. v1.9.0 当前本地验收

| 项目 | 当前结果 |
|---|---|
| Exhaustive process-isolated pytest | **102 PASS / 1 SKIP / 0 FAIL（103 collected）** |
| Unit | **94 PASS / 1 SKIP / 0 FAIL** |
| Integration | **8 PASS / 0 FAIL** |
| UI Layout Audit | **42/42 surfaces PASS**；0 横向溢出；0 runtime error |
| UI Interaction Audit | **16 editable fields + 47 visible user-view actions + 10 technical panels**；0 failure |
| 长程 authority | **1153 measured EventStore events；64/64 tasks succeeded；67 measured messages** |
| Evidence invalidation | **1 injected / 1 recovered by re-execution** |
| 长程耗时 | **19.653 s measured inside benchmark** |
| Estimated token-equivalent | **48035 ESTIMATED**；不是 API billing token |
| 低熵通信 replay | Sparse 相对 Full Mesh：**transmissions -50.0%，bytes -50.0%** |
| Memory 消融 | Full History **7808 EST. tokens** → ContextPack **721 EST. tokens**；关键事实召回 **100% → 100%**；估算缩减 **90.77%** |
| Scheduler 消融 | Round-Robin / Greedy 已实跑；当前环境 OR-Tools 缺失时明确 `available=false`，不伪装 fallback |
| Reference split | 跨进程数值等价；`max_abs_error=0`；activation payload **232 bytes**；`REFERENCE_MLP_NOT_LLM_SPLIT` |

长程 Benchmark 使用 `DETERMINISTIC_TOOL_EXECUTOR` 验证 EventStore / Scheduler / Memory / Topology / Verifier / Recovery 的单 Run 稳定性。**1153 Events ≠ 1153 LLM Calls；48035 也不是真实 Provider token。**

## 4. 必须在比赛机器 fresh run 的外部能力

当前构建环境没有比赛机器真实 Provider 密钥，且 OR-Tools 在本环境不可导入，因此下列内容不写成“已验证”：

1. 当前代码 + 公网 DeepSeek 的 fresh run；
2. 当前代码 + strict OR-Tools 的 fresh run；
3. 物理远程 MQTT Agent；
4. 真实 LLM layer split。

比赛机器配置 Provider 后运行：`python scripts/final_runtime_acceptance.py`。该门禁 Fail-Closed。

## 5. 发布结论

v1.9.0 的本地 artifact、deterministic runtime、UI 和真实性边界以 `FINAL_ACCEPTANCE.json`、`FUSION_VALIDATION.txt` 与 `evidence/` 中同版本证据为准。
