# 实验结果口径说明（v1.9.0）

当前可用于 v1.9.0 申报/答辩的本地量化结果：

- `benchmark_1000_events_v3_monolithic.json`：单一 authoritative Run；1153 measured EventStore events，64/64 tasks，Evidence invalidation 1/1 recovery。离线确定性 executor；`Events != LLM Calls`。
- `topology_ablation_v1.9.0.json`：同一捕获 message set 的 Full Mesh / Static Star fan-out replay；MOSAIC sparse 相对 Full Mesh transmissions 与 bytes 均减少 50.0%。对照是 replay，不是三套物理网络独立实跑。
- `scheduler_ablation_v1.9.0.json`：正式 Scheduler + CostModel 的受控 assignment benchmark；当前环境 OR-Tools 缺失时明确 `available=false`，不把 Greedy/Round-Robin 冒充 OR-Tools。
- `memory_ablation_v1.9.0.json`：正式 ContextBuilder；Full History 7808 EST. tokens → ContextPack 721 EST. tokens，关键事实召回保持 100%，估算缩减 90.77%；无 Provider API 调用，因此真实 API token 为 null。
- `split_inference_reference_v1.9.0.json`：跨两个 Python 进程的 Reference MLP Pipeline Split；`max_abs_error=0`，claim boundary=`REFERENCE_MLP_NOT_LLM_SPLIT`。

## 比赛电脑必须重新验证的外部事实

1. 当前 Provider 的公网真实 API；
2. strict OR-Tools（若现场环境安装成功）；
3. 真实物理 DEVICE / EDGE / CLOUD MQTT 节点；
4. 若主张真正 LLM 层切分，必须接入可拆分模型并保存实际 split provenance。

最终发布口径以根目录 `FINAL_ACCEPTANCE.json`、`evidence/test_matrix_v1.9.0.json` 与 `docs/FINAL_TRUTH_GATE.md` 为准。
