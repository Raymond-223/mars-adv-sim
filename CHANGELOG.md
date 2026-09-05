# Changelog

## 1.9.0 — Productized Workflow + Truthful Technical Transparency — 2026-09-05

- Reworked templates into editable task templates; selecting a template no longer starts a hidden scenario.
- Productized navigation: 工作台 / 任务 / 结果 / 智能体 / 技术透视 / 设置.
- Added live workflow telemetry and authoritative Task/DAG execution detail instead of a terminal SUCCESS-only experience.
- Separated task runs from benchmark/fault experiment jobs and bound deliverables strictly to the selected run.
- Fixed persistent Capability Registry vs process-local Execution Adapter rebinding across repeated runs.
- Added authoritative topology-rebuild events for visible Agent-offline adaptation without mislabeling reroute as retry/rollback recovery.
- Expanded technical transparency with scheduler objective/cost constraints, Beta reliability posterior, topology edge-score and λ₂, memory ranking, low-entropy message policy, and verifier/evidence provenance.
- Removed the duplicate ToDAG HTTP/static UI and duplicate legacy experiment result copies.
- Current deterministic validation is regenerated for v1.9.0. Public-provider + strict OR-Tools evidence remains an external final-machine gate and is not synthesized in this build environment.
- Current deterministic validation: 102 PASS / 1 SKIP / 0 FAIL (103 collected); UI layout 42/42 surfaces and interaction audit 0 failures.
- Current long-horizon authority benchmark: 1153 measured EventStore events, 64/64 tasks, 67 messages, Evidence invalidation 1/1 recovered, 19.653 s; deterministic executor, Events != LLM Calls.

## 1.8.0 — User/Judge Split + Deep Observability + Runtime Performance — 2026-09-05

- 普通用户主导航精简为工作台、任务进度、结果与文件、设置；Agent Studio 移入高级设置。
- 新增独立 Judge DEEP 技术透视台：运行全景、Dynamic DAG、Scheduler、协作拓扑、低熵通信、四层记忆、独立审核、故障恢复、验证实验、数据血缘。
- UI 统一浅灰/白/单一蓝色强调色，状态色仅承担成功/警告/失败语义。
- 前端热路径改为 SSE runtime invalidation + active-view 增量刷新；设置类数据不再周期轮询。
- READY DAG 独立节点并发执行，并受 Agent/Provider/DEVICE capacity 与依赖约束。
- 修复并发 Agent 同时触发大型 Observability Snapshot 的竞态；capture throttle 现在原子化。
- SQLite 本地权威库复用进程内连接，WAL 默认 synchronous=NORMAL，保留 FULL/EXTRA 可选模式。
- Validation Lab 新增 Scheduler、Memory 受控消融；低熵通信、Reference Split 重新实跑。
- 当前长程实测：1070 Event / 64 tasks / 67 messages / Evidence invalidation 1/1 recovery / 25.264 s；deterministic executor，Events != LLM Calls。
- 当前测试：99 PASS / 1 SKIP / 0 FAIL（100 collected）；UI 42/42 surfaces PASS，交互 0 failure。

## 1.7.1 — Final Acceptance Hardening — 2026-09-03

- 收紧 Judge 闭环语义：Reasoning Deliverable 不再等价于 Concrete Tool Execution。
- 收紧 Provider provenance：OpenAI-compatible 不能因复用 DeepSeek hostname 继承官方 DeepSeek 严格声明。
- Windows 启动器改用临时 loopback 空闲端口，继续禁止普通浏览器 fallback。
- Reference Split 从干净源码路径重新实跑并验证跨进程数值等价。
- 重新实跑 1000+ Event 单 Run Benchmark、ROS UI authoritative probe、Unit/Integration 测试。
- 当前发布证据统一升级为 v1.7.1，并重新生成 FINAL_ACCEPTANCE、Truth Gate、Package Audit 与 SHA256 Manifest。

## 1.7.0 — Truth-Hardened Product Release — 2026-09-03

- 修复通用任务通过回显 acceptance 自证成功的问题；自然语言条件改为独立 Provider 语义验证，缺少独立验证时 Fail-Closed。
- Provider Secret 按 Provider 隔离；Ollama 不继承云 Key；本地地址只在显式设置页可见。
- Task DAG 改为真实 edges；Assignment 拆分 recommended/actual tier；延迟未测量不再显示 0 ms；Memory token reduction 明确 ESTIMATED。
- 新增 Deliverable 预览/下载与语义标题、Agent Studio 持久化 CRUD、SQLite durable resume、安全副作用 crash-stop。
- 新增跨进程 Reference MLP Pipeline Split，强制声明 `REFERENCE_MLP_NOT_LLM_SPLIT`，不冒充真实 LLM 层切分。
- Docker 改为认证内网服务；Windows App Window 生命周期绑定；增加离线 wheelhouse 准备。
- 重做 1280/1366/1920 UI 响应式审计与 v1.7 Truth/Package/Test Matrix。

## 1.6.0 — Product-First + Verified Remote Endpoint — 2026-09-03

- 重构产品信息架构：工作台/任务/智能体/结果/设置优先，技术观测与 Judge View 降为高级层。
- 重写主 UI 视觉系统，减少 Dashboard 方框堆叠；协同图、Task flow、Memory、端边云改为过程型可视化。
- 运行动画改为 event-driven：新 SEND message、真实 Task 状态变化、ContextPack 增量才触发；不循环制造“系统正在运行”的假象。
- 新增 Provider Settings：DeepSeek / OpenAI-compatible / Ollama；API Key 不返回前端，Windows 使用 DPAPI；“测试连接”执行真实 completion。
- 新增 Execution Endpoint Registry：配置 DEVICE / EDGE / CLOUD MQTT Agent；真实 PLAN_REQUEST/PLAN_RESPONSE 连通验证。
- 新增 Remote Endpoint Acceptance：远程 MQTT Agent 真正进入 OR-Tools Assignment → ToolRuntime → Evidence / Verifier 主链，结果仅在实跑后可得 `REMOTE_RPC_SCHEDULER_VERIFIED`。
- Public DTO 去除 PID、command、project/workspace、绝对文件路径与 loopback 地址；endpoint 地址只存在显式 Settings API。
- Windows 启动优先 Edge/Chrome App Window，不再把 API Key 配置塞进启动脚本。
- Provider runtime generalized，但官方 DeepSeek strict gate 继续 Fail-Closed；localhost DeepSeek stub 仍分类为 `API_TEST_ENDPOINT_NOT_COMPETITION_STRICT`。
- 本次 unit：75 collected，74 PASS / 1 SKIP / 0 FAIL；关键 integration PASS。Chromium 本地页面被构建平台策略阻止，因此没有伪造新的 v1.6 截图审计。

## 1.5.0 — Competition Interactive — 2026-09-03

- 将 Console 从只读观测升级为比赛可操作系统：任务中心、跨领域场景实验室、故障注入、长程/拓扑实验、最终交付物。
- 新增 `START_MOSAIC.bat` + `scripts/start_mosaic.ps1`：Windows 双击后自动创建隔离 `.venv`、安装 `.[all]`、检查 OR-Tools、隐藏读取 DeepSeek Key、启动浏览器。
- 自定义真实任务强制 `GoalSpec=DeepSeek + Agent=DeepSeek + Scheduler=OR-Tools`，缺 Key、OR-Tools 或非官方 `api.deepseek.com` endpoint 均 Fail-Closed。
- 新增 ROS 软件自主修复与端/边/云金融研究两个跨领域可运行场景。
- 新增 Agent 下线、工具失败、需求变更、Evidence 失效四类后端故障入口；活动任务通过 LiveFaultMailbox 在下一 round 边界真实注入，空闲时运行独立可复现实验；故障/恢复写入 EventStore，不由 UI 伪造状态。
- 长程 Benchmark 升级为**单一权威 Run**：当前离线确定性实测 1,002 Event / 64 tasks（16×4 DAG），初始完成后注入 1 次 Evidence 失效并重执行 8 个受影响节点；executor 明确为 deterministic，token equivalent 明确为 estimate。
- 更新拓扑对照为同一捕获消息集 replay：当前 sparse 相对 full-mesh transmission/bytes 均降低 50%；Full Mesh/Star 明确不是独立物理网络执行。
- 新增控制面数据血缘、状态进入契约、按钮 endpoint 映射和真实 subprocess 生命周期审计。
- 测试矩阵更新为 78 collected：77 PASS / 1 SKIP / 0 FAIL；Console Chromium 70/70；ToDAG 27/27；Competition Control 22/22；Runtime truthfulness 15/15；长程 Benchmark 控制按钮 4/4。
- 删除当前申报材料中旧 2,260-event、94.44%、“真实 Token”式过度口径；历史文件仅保留为 legacy，不作为 v1.5 当前证明。
- 最终对外“当前 v1.5 公网 DeepSeek + OR-Tools 联合实跑通过”的门槛仍是 `CURRENT_STRICT_VERIFIED`。

## 1.4.0 — Final Truth Gate — 2026-09-03

- Removed MockAgent implementation from production `src/`; test fixture is isolated from production imports.
- Production Agent registration fails closed without an explicit execution adapter.
- DeepSeek strict-real verdict requires real network transport, official `api.deepseek.com`, committed assignment and complete `TOOL_EXECUTED` provenance.
- OR-Tools verdict requires `Assignment.solver_provenance` identifying `SimpleMinCostFlow` and `OPTIMAL`; policy labels alone are insufficient.
- Added machine-readable Console data lineage and status contracts.
- Missing runtime measurements render unavailable instead of fabricated defaults.
- Removed ToDAG manual completion button; completed results require explicit evidence.

## Earlier fusion history

See repository history/materials from v1.0-v1.3. Current competition claims must follow the current-release truth-gate documents and FINAL_ACCEPTANCE.json.
### Windows zero-install hotfix
- `START_MOSAIC.bat` 不再创建 `.venv`，也不再执行任何 `pip install`。
- 桌面核心直接从发布包 `src/` 加载；缺少 `jsonschema` 时使用内置 stdlib GoalSpec schema validator。
- DeepSeek/OpenAI-compatible API 无 OpenAI SDK 时使用内置 stdlib HTTP transport。
- 旧版 `start_mosaic.ps1` / 自动安装依赖流程已废弃，不再属于当前 Windows 启动路径。

