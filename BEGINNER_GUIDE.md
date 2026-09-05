# MOSAIC-Ω v1.9.0 保姆级使用指南

## 1. 启动

Windows **只需要双击项目根目录 `START_MOSAIC.bat`**。不再有第二个 BAT，也不需要先准备离线依赖或手工运行 PowerShell。

首次启动**不会创建 `.venv`，不会执行 pip，也不需要访问 PyPI**。MOSAIC 核心桌面运行时为离线零安装路径，直接加载本地源码。启动器优先选择 Python 3.11，并自动尝试 3.12 / 3.10 / 3.13。Edge / Chrome 可用时打开独立 App Window；如果浏览器路径识别失败，会自动使用系统默认浏览器。

如果启动失败，窗口会停住并给出错误码。先看 `.mosaic_logs/launcher.log`，后端异常再看 `.mosaic_logs/server-stderr.log`。OR-Tools 单独安装失败不会阻止 UI 打开，但需要严格 OR-Tools 调度的任务会保持禁用并明确提示。

## 2. 第一次必须先配置模型 API

打开左侧“设置”。

1. 选择 Provider：DeepSeek / OpenAI-compatible / Ollama。
2. 填写模型名称与服务地址。
3. 需要 Key 的 Provider 填写 API Key。
4. 先点“测试连接”。只有后端真的收到模型响应才会显示成功。
5. 点“保存设置”。

浏览器页面不会读取保存后的 Key 明文；以后只会看到“已配置”。

## 3. 运行一个真实复杂任务

回到“工作台”：

1. 在大输入框描述完整任务目标。
2. 点击“开始任务”。
3. 页面只显示用户需要理解的阶段：理解目标 → 规划任务 → 协同执行 → 验证结果 → 完成交付。
4. 如果需要看底层过程，点击“查看技术过程”。
5. 结果文件在“结果与文件”查看，可直接预览/下载；ROS/金融模板会把已验证成果复制到受控交付物目录，产品界面不展示电脑绝对目录。

## 4. 跑比赛跨领域模板

“工作台”右侧或“验证实验”提供：

- ROS 软件仓库自主修复
- 金融研究与风险分析

要争取“2 个及以上跨领域高完成度任务”的证据，需要两个场景都在你的比赛电脑上真实跑成功并保留结果/Evidence。

## 5. 配置真实远程端 / 边 / 云 Agent

如果你有另一台电脑、Jetson 或边缘节点：

1. 准备可访问的 MQTT Broker。
2. 在远程节点运行 `scripts/run_mqtt_agent.py --agent-id <你的AgentID> --host <Broker地址>`。
3. 主应用进入“设置 → 远程执行节点”。
4. 填 Endpoint ID、节点名称、DEVICE/EDGE/CLOUD、Agent ID、Broker、端口、Topic Prefix。
5. 点“保存节点”。
6. 点“连通验证”：系统会真实发送 MQTT PLAN_REQUEST；只有远程 Agent 返回 PLAN_RESPONSE 才会 VERIFIED。
7. 点“联合验收”：系统会启动权威 Run，由 OR-Tools 把任务分配给该远程 Agent，然后继续 ToolRuntime、Evidence、Verifier。
8. 最终结果只有出现 `REMOTE_RPC_SCHEDULER_VERIFIED` 才能证明这次远程 Agent 真正进入主链。

注意：这证明“真实远程 Agent 调度”，不自动证明“大模型层级 Split Inference”。

## 6. 故障恢复怎么验收

进入“验证实验”。可注入：

- Agent 下线
- 工具失败
- 需求变更
- Evidence 失效

有活动任务时，故障先写入 LiveFaultMailbox，再由 MainChain 在 round 边界消费；没有活动任务时才启动独立可复现实验。UI 不直接把节点改成失败/恢复。

## 7. 评委怎么查看

“评委模式”只做技术证明，不是产品首页。重点看：

- 当前 Run 是否形成闭环 Evidence
- ContextPack 是否有真实记录
- Topology / message policy 是否有真实运行记录
- DEVICE / EDGE / CLOUD 是否有真实 Assignment
- 是否发生真实 Recovery
- 两个跨领域场景是否都有 SUCCEEDED 记录
- Provider 支持与实际金标实跑是否分开

“数据血缘”可以逐项回答：每个数字来自哪里、怎么算；每个状态什么时候进入；每个动画由什么真实事件触发；每个按钮调用什么后端动作。

## 8. 不能混淆的几个概念

- `1,000+ Events` ≠ `1,000+ LLM Calls`
- estimated token ≠ provider measured token
- Provider 接口支持 ≠ 该 Provider 已实测通过
- `ExecutionTier` 决策 ≠ 物理三机实跑
- `ModelPartitionDescriptor` ≠ 已完成物理模型切分
- 远程 endpoint 已配置 ≠ 已验证
- MQTT `PLAN_RESPONSE` VERIFIED ≠ OR-Tools 主链联合验收通过
- 只有 `REMOTE_RPC_SCHEDULER_VERIFIED` 才证明该远程 Agent 真正进入这次权威任务链


## 9. Agent Studio 怎么用

打开“智能体”，可新增/修改/删除用于**下一次新 Run** 的 Agent 模板：角色、Skills、Tool Permissions、执行层级、模型覆盖、最大并发和启停状态。正在运行的 Run 不会被前端热改写。

## 10. 模型切分当前能证明什么

“验证实验”中的 Reference Split 会真的把一个 MLP 的前后两段放在两个 Python 进程运行，并记录阶段延迟、Activation Bytes 与数值等价性。证据固定标记 `REFERENCE_MLP_NOT_LLM_SPLIT`，不能表述成 DeepSeek/LLM 层切分已完成。
