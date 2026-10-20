# Mesh API 接入

Mesh 是第四个平台，与 Codex、WorkBuddy、即先共用任务清单、队列和横向指标报告。通过 HTTP 调用现有服务，不启动或重启 PM2 服务，不读取 Mesh 的私有压缩日志。

默认 Mesh 地址为 `http://127.0.0.1:18004`。在发布台的连接设置或 CLI `--base-url` 中指定已有服务，脚本不依赖服务源码目录。

## 网页

在发布台勾选 **Mesh API**，点击“读取模型”从 `/models` 加载模型列表。留空使用服务默认模型，指定时以 API 返回的模型 ID 为准；实际执行模型由 session-log 中的模型事件确认。

连接设置可填写本机 API 地址、公司、项目和 Agent ID。当前免登录服务使用 `local-company` / `local-project`，Agent 留空使用服务默认命名空间。用户 ID 由服务端决定，不伪造、不在请求体传入。网页不接受密码、Token 或任意可执行程序。

支持单任务和 XLSX 勾选后的批量发布；新批次的 Mesh 每平台跨批次最多并发 10 条任务，各任务使用独立 session；已有旧批次保留串行配置。已有历史批次不会自动补跑 Mesh；新批次选择四个平台即可形成四列对比。

Mesh 在服务端分配独立会话工作区，本地评测目录不会自动挂载到 Docker。任务列表里的正文会发送；`input_files` 附件当前会被发布前校验拦截。新会话使用 `workspace-write` 权限，按原生 revision 读后更新，只影响本次新会话。AskUser、额外权限请求会显示“等待处理”，在原 Mesh 客户端处理；不会擅自扩大全局权限。

## 接口与任务身份

| 用途 | 接口 |
|---|---|
| 就绪检查 | `GET /ready` |
| 模型列表 | `GET /models` |
| 身份范围检查 | `GET /sessions` |
| 新会话文件权限 | `GET/PUT /sessions/{session_id}/permissions` |
| 提交任务 | `POST /agent/runs` |
| 拉取 session-log | `GET /sessions/{session_id}/events` |
| 会话统计核对 | `GET /sessions/{session_id}/token-metrics` |
| 取消已确认运行 | `POST /agent/runs/{run_id}/cancel` |

每次创建新的随机 `session_id` 和稳定 `request_id`，保存回执后再发送。HTTP 202 只表示接收成功；以对应 run_id 的 `mesh/run-completed` / `mesh/run-failed` / `mesh/run-cancelled` 判断结束。出现文本、`turn/end` 或暂时没有事件都不算整次运行结束。

事件接口携带公司/项目/可选 Agent、`generation`、`after` 和 `limit`，按 `next_after` 拉到空页；短页不提前结束。游标倒退、重复序号、代次或身份冲突会显式报错。导出文件中同一代次/seq 重放只计一次，冲突内容不静默合并。

## 命令

Python 3.10+ 标准库即可，无额外依赖。此版本仅允许 localhost/127.0.0.1/::1 地址，不接受 URL 凭据或跨地址重定向。若本机服务启用 Bearer，可在命令行配置 `--token-env MESH_API_TOKEN`，凭据值不落盘；不要将 Token 放进任务 JSON。

```text
python <skill>/scripts/mesh_api.py doctor --base-url http://127.0.0.1:18004
python <skill>/scripts/mesh_api.py submit --prompt-file task.txt --receipt-file mesh-receipt.json --model qwen3.8-flash --wait
python <skill>/scripts/mesh_api.py status --receipt-file mesh-receipt.json
python <skill>/scripts/mesh_api.py wait --receipt-file mesh-receipt.json --timeout 300
python <skill>/scripts/mesh_api.py export --receipt-file mesh-receipt.json --output session-log.jsonl
python <skill>/scripts/mesh_api.py cancel --receipt-file mesh-receipt.json
```

重复使用同一回执不重发。提交结果不确定时，`status` 从原会话日志恢复唯一 run_id，不重新 POST 业务任务。多个运行导致归属不明确时停止并要求核查。单独 `wait` 超时停止观察；统一发布器的任务预算到期时会对该回执的准确 run_id 请求取消，随后仍需确认原生终态。

统一任务配置：

```json
{
  "providers": {
    "mesh": {
      "driver": "mesh_api",
      "base_url": "http://127.0.0.1:18004",
      "company_id": "local-company",
      "project_id": "local-project",
      "model": "qwen3.8-flash",
      "timeout_seconds": 1800
    }
  }
}
```

## 指标口径

- Token 取主会话 `assistant/message.data.usage`，不把流式片段或会话汇总再次累加。每条事件有缺失字段时保留缺失/部分汇总，不用字数推算。
- Mesh `inputTokens` 是未缓存输入。为对齐对比表的“输入 Token”，优先用已报告 `totalTokens − outputTokens`；或在未缓存、缓存读取、缓存写入都明确时相加。缓存读取仍作为输入子集单列；reasoning 不重复加到输出或总量。
- `context_pressure`、`context_breakdown` 的字符密度估算不作为 Token 消耗量。`token-metrics` 只保留用量及语义信息用于核对，不覆盖逐事件汇总。
- 工具调用按 callId 去重，与 `tool/result.message.source.callId` 配对；时间为调用和结果的 `time_ms` 差，均值/P95 使用原比较器算法。
- 任务耗时为匹配 run_id 的原生运行起止事件跨度。`GET /sessions/{session_id}/token-metrics` 的 `session_stats` 提供 `llm_ms`、`steps`、`ttft_ms`、`ttft_steps`。LLM 累计耗时取 `llm_ms`，均值取 `llm_ms / steps`；LLM 首 Token 均值取 `ttft_ms / ttft_steps`，分母为零时留空。
- 上述 API 是整个会话的累计值。只有单一 run、无继承 seed、代次与身份匹配、日志水位和步骤数核对通过时才用于该运行。其他情况仅展示可配对的逐步骤计时，不把整个会话总量套给某一轮。
- Mesh LLM 计时沿用服务实现：匹配同一 turn/step 的 `step/start → assistant/message`，可能包含重试等待；不冒充单次 HTTP 请求耗时。P95 来自逐步骤样本的线性插值，只有样本数及耗时之和与 API 的 `steps` / `llm_ms` 一致才标为完整；仅有聚合值时 P95 留空。
- “任务首 Token 耗时”是 `mesh/run-started` 到首个 LLM 步骤首次流 Token 的时间差，与所有 LLM 步骤的平均 TTFT 分开显示。流记录仅提取时间戳，包含文本、思考或工具调用首次产出，不保存思考文本、流片段或参数。第一步计时缺失时，不用后续步骤替代。
- 当前只统计主会话；子代理、没有公开用量的失败请求不会自动合并。只保存公开消息、用量和工具元数据，不保存隐藏思考、系统指令、原始请求体或凭据。

导出的 `mesh.session` 身份头和 `mesh.event` JSONL 可直接交给 `compare.py discover --mesh session-log.jsonl --only mesh`，再按 `run_id` 选择。网页生成报告沿用相同解析器。

旧任务可使用 `export` 重新读取同一回执的 session-log 和 metrics，导出到新文件，再用 `orchestrate.py bind --platform mesh --path <新文件> --selector '{"run_id":"原生 run_id"}'` 绑定。该流程只读 Mesh API，不重发任务；原日志及旧报告保留。
