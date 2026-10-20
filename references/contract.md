# 数据与比较契约 v1

## 比较单位

task 是共同的业务目标与验收标准；run 是某平台的一次执行。task 的 `context_id` 标识相同输入快照、可访问数据和权限。人工在 `alignment_notes` 说明不同措辞、工具差异、人工干预及任务范围后设置 `alignment_confirmed`。

默认 Codex 按 `task_started.turn_id` 分割，WorkBuddy 按用户 `startsNewUserRequest` / `conversationRequestId` 分割，即先按 JSONL 一行的 `id` 作为 session。选择器来自 discover，不能猜。

Codex/WorkBuddy 可用 `selector:{"session":true}` 合并文件所有轮次。若一个文件含不同业务任务应逐轮选取，不用整段会话。整段会话合并时耗时是各轮次耗时之和，不含轮间用户等待；并发可能重叠。即先一个 session 内有多次用户消息时保留人工干预计数。子代理/分叉、不同机器和遗漏日志必须明确披露，不能宣称覆盖全部成本。

## Token

Codex：优先 `type=token_usage_record.payload.usage`，按 response_id 去重。`turn_token_usage` 和 `thread_token_usage` 是累计值，不叠加。旧版回退 `event_msg/token_count.info.total_token_usage` 的相邻差分；首次累计值与 last 一致时才把首条 last 纳入，避免继承历史成本。累计下降标记异常，不猜基线。独立 usage 存在时完全忽略同一轮 token_count。

WorkBuddy：`providerData.rawUsage` 优先，缺失才用 `providerData.usage`，保留合法零值；按 messageId/id 去重。input/output/total、cached、cache write、reasoning 的来源在 adapters.py 中明确映射。不是用 tokenizer 对文本重新计数。

即先当前 reporting_session_trajectory 导出没有 usage。`stats.assistant_turns` 不是请求 Token 或真实 LLM 请求次数。

输出 Token 已含推理子项，输入已含缓存子项；总 Token 按记录的 total 或 input+output，不额外累加缓存/推理。字段部分缺失显示 partial_sum 与已知/观察记录数，不冒充完整统计。零个用量记录显示 missing。

## 时间

| 指标 | 定义 / 数据源 |
|---|---|
| 任务耗时 | Codex 优先 task_complete.duration_ms；其次起止事件；WorkBuddy 用户事件到最后业务事件的观测跨度（估算）；即先缺失 |
| LLM 响应耗时 | 原三平台无可靠请求计时时留空，可补充 telemetry；Mesh 使用 session_stats 的步骤计时并明确标注，逐步骤样本可计算 P95 |
| 首 Token 耗时 | Codex task_complete.time_to_first_token_ms 为轮次指标；Mesh 为运行开始到首个步骤首次流 Token；每次 LLM 的平均首 Token 耗时另列 |
| 消息输出跨度 | Codex AgentMessage 的 started_at_ms/completed_at_ms，只是输出片段，不替代完整请求延迟 |
| 工具响应耗时 | 同一 call_id/callId 的调用日志时间到结果日志时间；包含调度、封装、传输，不宣称纯服务耗时 |
| P50/P95 | 对有计时的请求线性插值分位数；展示样本数与覆盖，少量样本不推断总体性能 |

不同 method 的时间不做直接优劣比较。并行工具时间不能与 LLM 时间相加当任务耗时；相邻事件间隔也不是模型推理时间。时间戳统一为 epoch ms，含 Z/时区 ISO 可解析，无时区字符串留空避免错配。

## 工具计数

按结构化发起调用的唯一 ID 统计，不把输出、重发记录、事件 UI 镜像再计一次。实际重试有不同 ID 时算新调用。Codex `exec`/其他组合调用内部动作不混入顶层次数，因此次数反映日志/API 抽象层，不是等价业务工作量；必须同时看工具名与结果。不能从工具名少或次数少直接认定更高效。

结果覆盖率 = 已配对结果 / 记录调用。错误只依据结构化状态/is_error，不按文本含 error 关键词猜。日志中断、结果缺失和已恢复工具错误不是业务完成判定。

## 补充计时（可选）

从实际采集器取得 request 开始/结束或显式 duration 后，在 run 中设置 `telemetry:"./timing.json"`。不能为填满图表手写猜测数据。格式：

```json
{
  "schema_version": 1,
  "source_sha256": "inventory 中的日志 SHA256",
  "selector": {"record_id": "即先记录 ID"},
  "recorder": "采集器名称/版本",
  "events": [
    {"id": "request-001", "kind": "llm_request", "start_ms": 1700000000000, "end_ms": 1700000002500},
    {"id": "tool-span-001", "kind": "tool", "call_id": "日志中的工具调用 ID", "duration_ms": 123}
  ]
}
```

telemetry 必须绑定来源哈希和 selector；重复 ID、负时长、与日志无对应的工具 ID 或不一致起止时间被拒绝。补充工具时间与原事件跨度不混算，报告说明补充来源。该机制不能恢复从未采集的历史数据。

## 产物、来源与跨平台

run.artifacts 为 `[{"path":"./result.md","label":"最终报告"}]`。只记录用户明确选择的文件；SHA256 固定证据快照。小于等于 200KB 的 UTF-8 txt/md/json/csv/tsv 可读取文本，其他文件只提供存在、大小、哈希与链接。需视觉验收的报告/图表要另行检查，不能凭文件存在判通过。

manifest 中相对路径以 manifest 所在目录为基准。JSON 的 Windows 绝对路径使用 `C:/Users/name/...` 或双反斜杠 `C:\\Users\\name\\...`；推荐可移植的 `~/.codex/...` 和 `./uploads/...`。PowerShell 示例：

```powershell
py -3 "$env:USERPROFILE/.codex/skills/agent-run-compare/scripts/compare.py" discover --jahead "C:/评测/uploads/reporting_session_trajectory.jsonl" --out "C:/评测/inventory.json"
py -3 "$env:USERPROFILE/.codex/skills/agent-run-compare/scripts/compare.py" build --manifest "C:/评测/manifest.json" --out "C:/评测/report"
```

跨机复制日志后路径可能不同；评分绑定源字节、选择器及产物，来源文件增加新行会使旧评价失效。对活跃会话先导出只读快照；不是删除/修改原日志。schema 不认识或 JSONL 未写完时显式报错；discover 记录每个文件错误继续，build 则拒绝生成错误比较。

## Mesh API

Mesh 主会话按原生 run_id 统计；输入 Token 包含缓存读取和写入，原始 inputTokens 为未缓存输入。读取 assistant/message 的 usage，以 generation/seq 去重，工具按 callId 配对。会话结束以匹配 run_id 的 mesh/run-* 终态事件为准。session-log 分页与具体来源边界见 [mesh-api.md](mesh-api.md)。

缓存率 = 缓存读取 Token / 包含缓存读写的输入 Token × 100%。输入记录须完整且分母大于零；缓存字段部分缺失时只展示下界。工具错误按明确标记的独立调用 ID 统计，同 ID 恢复成功不抹掉先前错误；即先缺少 ID 时明确按结果事件计数。原始计费 Token 保留为审计数据，不再展示为横向指标。

Epsilon 通过 HTTP/SSE 记录执行，指标口径和完成边界见 [epsilon-api.md](epsilon-api.md)。

Epsilon 工具额外 Token（AgentRuntime）单列统计 `tool_exec_completed.usage`，不合并入主模型 Token、输入输出与缓存率。按 SSE 事件身份去重，details 分项不重复累加，历史未采集用量保持缺失。部分工具属于运行时固定记账，并非供应商实测。
