# MOSAIC-Ω V3 — XH-202631 Product-First v1.9.0

MOSAIC-Ω v1.9.0 的产品定位是：**先作为可直接使用的多智能体复杂任务软件，再提供可下钻的技术透视、验证实验与真实性证据。**

## 1. Windows 最快启动

双击项目根目录：

```text
START_MOSAIC.bat
```

现在**只有一个 Windows 启动文件**，不再要求先运行第二个 BAT，也不再依赖 PowerShell。`START_MOSAIC.bat` 只负责寻找 Python 3.10–3.13，然后把所有环境创建、依赖检查、随机 loopback 端口、后端健康检查和浏览器启动交给 `scripts/windows_launcher.py`。

首次启动**不会创建 `.venv`、不会执行 pip、不会访问 PyPI、不会联网安装依赖**。MOSAIC 桌面核心运行时直接从本地 `src/` 加载，并提供无第三方依赖的 GoalSpec 校验后备实现。DeepSeek/OpenAI-compatible API 在未安装 OpenAI SDK 时自动使用内置 stdlib HTTP 传输；OR-Tools 未安装时应用仍可打开，只有要求严格 OR-Tools 的任务保持 Fail-Closed。Edge / Chrome 存在时优先使用独立 App Window；找不到时自动回退到系统默认浏览器。

启动失败不会闪退：窗口会保留错误码，并写入 `.mosaic_logs/launcher.log` 与 `.mosaic_logs/server-stderr.log`。如确实需要比赛离线 wheelhouse，可在已经成功启动过一次后手工执行 `py scripts\windows_launcher.py --prepare-offline`；这只是高级准备功能，不是普通用户启动前置步骤。Docker **不是产品运行的必要条件**。

## 2. 软件层级

v1.9.0 采用“工作主线 + 技术下钻”的产品信息架构，不再使用比赛临时入口式的信息架构。

### 工作

1. **工作台**：输入复杂目标、选择模板、启动/停止任务。
2. **任务**：查看当前 Run 的真实 Task DAG、依赖、Assignment、执行层级、Verifier 与 Evidence。
3. **结果**：按所选 Run 预览/下载真实交付物与 Evidence，不展示绝对路径。
4. **智能体**：管理角色、Skills、Tool Permissions、DEVICE/EDGE/CLOUD、模型覆盖与负载。

### 更多

5. **技术透视**：按真实 Run 逐层下钻 **运行全景 → Dynamic DAG → 优化调度 → 协作拓扑 → 低熵通信 → 四层记忆 → 独立审核 → 故障恢复 → 验证实验 → 数据血缘**。
6. **设置**：配置 Provider/API 与远程执行端点。

技术透视是产品本身的可解释与可追溯能力：普通任务页面保持易用，深入分析时再展开数学、算法和 provenance。

## 3. 通用任务真实性边界

v1.9.0 修复了旧版最严重的“文本自证成功”漏洞：

- Agent 的自然语言成果可以落盘为真实 deliverable，但 `task` executor **不会**把验收条件自动拼进输出。
- `Verifier` 只对明确 DSL 做确定性验证：`exit_code==0`、`contains:`、`file_exists:`、`file_contains:`。
- 普通自然语言验收必须走**独立 Provider 语义验证**；没有独立验证能力时 Fail-Closed，不允许因为结果里重复了验收文字就通过。
- Evidence 仍需存在且 SHA256 完整性通过。

因此“模型说完成了”不等于“系统验证完成了”。

## 4. Provider 与 API Key

支持：

- DeepSeek
- OpenAI-compatible API
- Ollama / local OpenAI-compatible

安全规则：

- 每个云 Provider 使用**独立 secret 文件**；DeepSeek Key 不会被 OpenAI-compatible 继承，反之亦然。
- Ollama 永远不读取云 API Key。
- 浏览器只拿到 `api_key_present`，拿不到明文。
- Windows 持久化使用 DPAPI；非 Windows 开发环境使用 0600 权限文件，并明确标注为权限受限存储。
- 设置页允许显式编辑本地 Ollama 地址；技术透视/Observatory/Control DTO 继续脱敏 loopback 和本机路径。
- “测试连接”执行真实 chat completion，只有真实响应才显示 request id、model、latency 与 provider usage。

## 5. Agent Studio

Agent Studio 配置不是装饰 UI。新建/修改/停用模板会写入后端 `AgentSettingsStore`，并由下一次自定义 Run 读取进入 Capability Registry / Scheduler。

可配置：

- Agent ID / 名称 / 角色
- Skills
- Tool Permissions
- DEVICE / EDGE / CLOUD
- Provider 模型覆盖
- 最大负载
- Enabled / Disabled

正在运行的 Run 不会被前端热改写；变更仅影响后续新 Run。

## 6. 端—边—云与真实 Assignment

v1.9.0 把两个概念分开：

- `recommended_tier`：CostModel / Placement Engine 推荐层级。
- `actual_execution_tier`：最终选中的 Agent 实际层级。

如果发生 fallback，会单独记录 `placement_fallback` 与原因；UI 动画和分配统计只使用实际层级，不再把推荐层级冒充执行位置。

远程节点可通过 MQTT 注册 DEVICE / EDGE / CLOUD Agent。联合验收链路：

```text
Scheduler Assignment
→ Remote MQTT PLAN_REQUEST / PLAN_RESPONSE
→ ToolRuntime
→ Evidence
→ Verifier
```

MQTT 设置支持 username/password 与 TLS。Docker 示例默认禁止匿名访问。

## 7. 模型切分的真实边界

v1.9.0 新增一个可执行的**跨进程 Reference MLP Pipeline Split**：前半段和后半段在两个独立 Python 进程执行，记录阶段延迟、Activation Bytes、总耗时，并与单体执行做数值等价检查。

其证据明确标记：

```text
REFERENCE_MLP_NOT_LLM_SPLIT
```

它证明“切分执行机制与测量链是真实可运行的”，**不宣称 DeepSeek/LLM 层级 Split Inference 已经完成**。如果最终要对比赛主张“大模型层级切分”，仍需接入真正可拆分的本地模型并保存实际 split provenance。

## 8. 技术观测真实性

- Task DAG 箭头只来自 `task_graph.edges`，不再用排序后的相邻节点伪造依赖。
- 新 `SEND message_id` 被观察到时才播放一次通信粒子。
- Task authoritative state 真变化时才高亮节点。
- `context_pack_count` 增加时才播放 Memory 流动。
- Agent 延迟没有 `latency_measurement_count` 就显示“未测量”，不再把配置默认值 `0 ms` 冒充实测。
- Memory token 与 reduction 都直接标记 `ESTIMATED`；真实 API usage 单独展示。

## 9. 数据持久化与恢复

产品主链默认使用 stdlib SQLite EventStore，而不是进程内 Memory EventStore，因此 Run / Task / Event / Idempotency authority 可跨进程重启保留。

恢复原则：

- 可安全重试的 READY 边界可继续执行。
- 如果崩溃发生在未知结果的副作用工具调用中，系统进入 `PAUSED / SAFE STOP`，不会盲目重复执行副作用。
- 服务器部署仍可注入 PostgreSQL-backed service。

## 10. Docker

Docker 是**可选部署工具**，不是比赛现场使用软件的前置条件。

`deploy/docker-compose.yml` 当前：

- workspace 为可写 volume；
- PostgreSQL / Redis / MQTT 不对宿主机公开端口；
- Redis/PostgreSQL/MQTT 密码由环境变量强制提供；
- MQTT `allow_anonymous false`；
- Console 仅绑定 `127.0.0.1:${CONSOLE_PORT}`。

普通比赛演示建议优先使用 Windows App Window。

## 11. 跨领域场景

内置两个可运行模板：

- ROS 软件仓库自主诊断、修复、构建、测试、报告。
- 金融研究与风险分析（使用可复现 fixture 数据集；不冒充实时金融数据源）。

Benchmark 的 1000+ 规模仍严格标注：`Events != LLM Calls`。离线确定性执行用于长程稳定性测试，不冒充公网模型调用。

## 12. 发布验收

当前包的最终测试结果写入：

- `FINAL_ACCEPTANCE.json`
- `evidence/test_matrix_v1.9.0.json`
- `evidence/final_truth_gate_v1.9.0.json`
- `evidence/ui_layout_audit_v1.9.0.json`
- `evidence/long_horizon_v1.9.0.json`
- `evidence/split_inference_reference_v1.9.0.json`
- `evidence/release_package_audit_v1.9.0.json`
- `SHA256_MANIFEST.txt`

外部事实（真实公网 Provider、真实远程 MQTT 设备、真实 LLM Split）必须在比赛电脑 fresh run 后再形成对应金标证据；构建脚本不会伪造这些结果。

详见 `docs/COMPETITION_REQUIREMENT_MATRIX.md` 与 `docs/FINAL_TRUTH_GATE.md`。
