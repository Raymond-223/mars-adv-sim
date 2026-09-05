# MOSAIC-Ω V3

MOSAIC-Ω V3 是面向长程多智能体任务的可重放自治执行主链。当前仓库只保留一套任务状态、调度、工具执行、验收与恢复路径。

## 主链

```text
UserGoal
  ↓
goalspec/              目标与约束编译
  ↓
todag/                 TaskGraph / ToDAG
  ↓
execution_scheduler/   Capability + Scheduler + EventStore
  ↓
memory_recovery/ + message_topology/
  ↓
agent_runtime/         Agent 运行时公共契约
  ↓
execution_scheduler/   Agent Adapter + ToolRuntime
  ↓
verifier/              Evidence 验收
  ↓
recovery/              retry / replace / rollback / replan / safe-stop
  ↓
observability/         只读运行投影
  ↓
apps/console/          只读可视化
```

关键原则：

- `EventStore` 是任务运行状态唯一事实源。
- Agent 只产生计划/ToolCall，不直接宣布成功。
- 外部副作用统一经过 `ToolRuntime`。
- `Verifier` 通过后任务才能进入 `SUCCEEDED`。
- `RecoveryEngine` 只处理受影响子图。
- Console 只读，不形成第二控制平面。

## 目录

```text
src/mosaic_omega/
├── goalspec/              # GoalSpec 编译
├── todag/                 # TaskGraph / ToDAG
├── execution_scheduler/   # 调度、EventStore、ToolRuntime、执行服务
├── message_topology/      # 动态拓扑与低熵通信
├── memory_recovery/       # 长程记忆与 ContextPack
├── agent_runtime/         # Agent/端边云公共运行时模型
├── verifier/              # Evidence 与验收
├── recovery/              # 影响子图与局部恢复
├── observability/         # 日志、指标、Trace、Snapshot
├── integration/           # 主链装配与跨模块 Contract Adapter
├── console_api/           # Console 只读数据接口
├── schemas/               # 公共 Schema
└── storage/               # 文件对象存储

apps/console/               # Web Console
scenarios/ros_repair/       # ROS Repair 演示场景
tests/                      # 单元与集成测试
scripts/                    # 验收与演示脚本
deploy/                     # Docker Compose / MQTT
experiments/benchmarks/     # 保留的基准输入
```

## 安装

```bash
python -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -e ".[dev]"
```

生产依赖：

```bash
pip install -e ".[all]"
cp .env.example .env
```

## 验证

```bash
./scripts/verify_local.sh
```

单独运行：

```bash
python scripts/demo_main_chain.py
python scripts/demo_ros_repair.py
./scripts/demo_console.sh
```

Windows PowerShell 下的 DeepSeek 严格真实 API 路径：

```powershell
py -m pip install -e ".[dev,deepseek]"
$env:DEEPSEEK_API_KEY="在本机填入，不要提交到 Git"
py scripts/check_deepseek_api.py
py scripts/demo_main_chain.py "完成一个可验证长程任务" --goalspec-mode deepseek --agent-mode deepseek --output experiments/results/deepseek_mainchain.json
py scripts/verify_deepseek_run.py experiments/results/deepseek_mainchain.json
```

`--agent-mode deepseek` 不会静默回退到 MockAgent。API 错误会进入主链的错误与恢复记录，成功结果则在每个 `TOOL_EXECUTED` 事件中保存不含密钥的 `api_provenance`。

生产后端验收：

```bash
./scripts/verify_production.sh
```

详细边界见 `docs/architecture/MAIN_CHAIN.md` 和 `docs/architecture/ARCHITECTURE.md`。
