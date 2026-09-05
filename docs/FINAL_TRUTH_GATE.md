# v1.9.0 Final Truth Gate

## 1. 数字与数据

任何屏幕数字必须满足至少一种口径：

- `MEASURED`：由真实运行/API/系统计时取得。
- `ESTIMATED`：由明确估算器计算，UI 直接显示 ESTIMATED。
- `CONFIGURED`：配置参数，不得描述为实测。
- `REPLAY`：同一捕获数据的对照重放，不得描述为独立物理网络实验。
- `UNMEASURED`：没有测量就显示“未测量”，禁止默认填 0。

## 2. 状态

UI 状态必须来自后端 authoritative state/event：

- Task：READY / RUNNING / VERIFYING / SUCCEEDED / FAILED / PAUSED 等。
- Job：QUEUED / RUNNING / STOPPING / SUCCEEDED / FAILED / INTERRUPTED。
- Fault：PENDING / APPLIED / FAILED。

前端不得自行把按钮点击解释为成功。

## 3. 动画

### 协同消息粒子
仅在相邻 snapshot 之间出现新的真实 `SEND message_id` 时播放一次。历史 SEND 不循环播放。

### Task 节点短高亮
仅在相邻 authoritative snapshot 的 Task state 发生变化时播放。

### Memory 流动
仅在 `context_pack_count` 增加时播放。

### waiting pulse
只表示浏览器尚未取得 snapshot，不代表 Agent/网络/模型正在运行。

并支持 `prefers-reduced-motion: reduce`。

## 4. Task DAG

任何箭头必须直接对应 `task_graph.edges[]`。排序、布局、层次计算只能改变几何位置，不能制造新的依赖边。

## 5. 通用任务验收 / Verifier

- `task` executor 不得把 acceptance condition 拼接到结果。
- 确定性 DSL：`exit_code==0` / `contains:` / `file_exists:` / `file_contains:`。
- 其他自然语言验收必须由独立语义 verifier 判断；Provider 不可用时 Fail-Closed。
- Evidence 必须存在并通过 hash 完整性校验。

## 6. Provider Secret

- 每个 Provider 的持久 secret 独立存储。
- Ollama 不读取 DeepSeek/OpenAI Key。
- Secret 明文不返回前端、不写 localStorage。
- 设置页可以显示用户主动配置的本地 endpoint；非设置 DTO 必须脱敏本机路径与 loopback。

## 7. 端边云

必须分开：

- `recommended_tier`
- `actual_execution_tier`
- `placement_fallback`

界面分组与动画只使用实际层级。

## 8. 模型切分

Reference MLP 跨进程 Split 证据必须标记：

```text
REFERENCE_MLP_NOT_LLM_SPLIT
```

未执行真实 LLM layer split 时不得使用“已完成大模型切分推理”的表述。

## 9. 交付物

下载/预览只允许 approved artifact roots。浏览器拿到的是 `artifact_id`、逻辑名称和语义元数据，不得拿服务器绝对路径。

## 10. 发布证据

v1.9.0 发布只接受 v1.9.0 当前代码重新生成的：

- Unit / Integration Test Matrix
- UI layout / browser audit
- Final Truth Gate
- Release Package Audit
- SHA256 Manifest

旧版本验收 JSON/截图不得作为 v1.9.0 当前发布结论；根目录 `FINAL_ACCEPTANCE.json` 必须明确标记 v1.9.0。
