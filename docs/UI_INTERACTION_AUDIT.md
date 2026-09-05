# Console 交互、状态、动画真实性审计

## 按钮 / 可点击元素

| 元素 | 实际动作 | 是否修改后端 | 验收方式 |
|---|---|---:|---|
| 侧边导航 10 个按钮 | 切换已经读取的 snapshot 投影视图 | 否 | 点击后对应 `view-*` 激活，标题同步变化 |
| `立即刷新` | `GET /api/snapshot[?run_id=...]`，并恢复自动轮询 | 否 | Network/API 返回后页面数据更新 |
| `暂停自动刷新` | 切换浏览器 `state.paused`，停止/恢复 1 秒轮询 | 否；也**不暂停 Agent** | 按钮文案在“暂停/恢复自动刷新”之间切换 |
| Run Selector | 用所选 `run_id` 请求对应 snapshot | 否 | `/api/snapshot?run_id=...` 与页面 Run ID一致 |
| TaskGraph 节点 | 展开该节点状态、状态事件、Assignment、Acceptance、Evidence count | 否 | 详情中的 event_id 可在 Events 页查到 |
| Topology 边 | 展开后端真实 edge object | 否 | 详情 JSON 与 `snapshot.topology.edges[]` 一致 |
| 消息过滤框 | 客户端过滤已经读取的消息 | 否 | 清空后恢复全部行 |
| 事件过滤框 / 类型选择 | 客户端过滤 EventStore projection | 否 | 清空后恢复全部行 |

Server 对 `POST/PUT/PATCH/DELETE` 固定返回 HTTP 405 `read_only_console`，因此前端不存在“看起来能控制后端但实际无效”的控制按钮。

## 动画与视觉变化

### `pulse` 等待动画

唯一 `@keyframes`：`apps/console/frontend/assets/style.css @keyframes pulse`。

它只在 `/api/snapshot` 尚不可取得时显示，语义是：**“浏览器正在等待/轮询 snapshot”**。它不代表 Agent 在推理、不代表 Task 正在 RUNNING、不代表网络正在通信。页面中已直接写明这一点。

### 其他 transition

导航 hover、Task 节点 hover 等 CSS `transition` 仅是交互反馈，不编码任何运行时状态，因此不作为“过程动画”解释。

### TaskGraph / Topology

没有循环播放的伪流程动画。TaskGraph 颜色直接由 task current state 决定；Topology 边宽只有在后端存在 `score` 时才按 score 映射。score 缺失时固定 1px，并不填 UI 默认分数；当前 TopologySnapshot 的 `edges` 本身就是 active edge 集合，因此 UI 不制造 standby/active 动画或虚线状态。

## 顶栏状态

- 不再使用 `LIVE` 表示一次 HTTP 请求成功。
- `SNAPSHOT FRESH/STALE` = `browser_now - snapshot.generated_at <= 5s` 与否。
- 这是**快照新鲜度**，不是 Agent liveness。
- Agent 的 ONLINE/OFFLINE 来自 CapabilityRegistry，并显示 `metadata.status_updated_at` 与 `metadata.status_source`。
