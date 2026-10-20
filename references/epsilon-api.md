# Epsilon API 接入

第五个平台通过 HTTP/SSE 接入。默认示例地址为 `http://127.0.0.1:30100`，可在发布台的连接设置或 CLI `--base-url` 中指定自己的本机或内网服务。使用已有服务，不修改或重启服务，不读取凭据配置。

## 接口与状态

- `GET /health` 检查服务；`GET /models` 动态读取模型 profile。
- `POST /agent/response` 为每次评测创建随机独立 session，持续消费 SSE。`run_started` 返回原生 run_id。
- `POST /agent/interrupt` 携带本次完整身份范围、session_id 和原生 run_id，精确中断本次运行。
- 当前服务没有公开的独立运行状态、历史事件或 metrics 查询路由；本地 SSE 日志和回执持续保存，可离线解析及导出。

`turn_completed` 是一次 assistant 轮次，不代表整个 Agent 已结束。只有收到 `[DONE]`、末轮公开回答且无待返回工具、error 或暂停事件，才记为 completed。`agent_suspended` 为等待处理，阻塞本平台后续队列；不自动恢复或放行权限。断流和未知提交状态不自动重发，因为新 `/agent/response` 可能替换同 session 的原运行。

请求使用配置中的 user/company/project/agent，默认 `local-user` / `local-company` / `local-project`，Agent 留空使用服务默认。用户可在网页连接设置中修改；这些是本机测试范围，不代表任何外部账户身份认证。接受本机及明确的 RFC1918/ULA 内网 IP，不接受任意域名、公网或链路本地地址，无重定向。模型必须来自接口列表，不能用 Mesh 的模型列表代替。

本机服务通过 `options.cwd` 使用本次独立工作目录。远程服务不接收本机路径，使用服务端配置的工作目录；会话 ID 每次独立生成，本机文件不上传。不设置全局或 `full_auto` 权限。当前只支持纯文本任务，输入附件在发布前拦截。

## 取值

- 每个 `turn_completed.payload.usage` 累加一次。`input_tokens` 为未缓存输入，统一输入为 input + cache_read + cache_write，总 Token 再加 output；不重复累加 `details`。实际模型取 usage 的 last_model / details 模型名。
- 工具按 `tool_use_id` 去重及配对，`is_error=true` 记录一次错误调用；同 ID 后续恢复成功也保留已发生的错误。
- 任务和工具耗时为本机单调时钟接收 SSE 的时间跨度，含传输和调度开销。首 Token 字段明确标为“请求至首个公开文本片段”，不是隐藏思考首 Token。
- 当前 SSE 没有完整 LLM 请求开始时间，不从相邻事件或任务减工具耗时估算 LLM 延迟。
- 缓存率统一按“缓存读取 / 含缓存读写的输入 × 100%”；分母缺失或为零时留空；分母完整但缓存记录不全时显示下界。
- 不保存隐藏思考、完整消息块、工具输入、请求体或凭据。工具输出及最终公开回答保留为核验证据。

## CLI

```text
python <skill>/scripts/epsilon_api.py doctor
python <skill>/scripts/epsilon_api.py submit --prompt-file task.txt --receipt-file epsilon-receipt.json --workspace workspace --model deepseek-v4-flash
python <skill>/scripts/epsilon_api.py status --receipt-file epsilon-receipt.json
python <skill>/scripts/epsilon_api.py export --receipt-file epsilon-receipt.json --output session.jsonl
python <skill>/scripts/epsilon_api.py cancel --receipt-file epsilon-receipt.json
python <skill>/scripts/compare.py discover --epsilon session.jsonl --only epsilon --out inventory.json
```

统一 providers 配置：`{"epsilon":{"driver":"epsilon_api","base_url":"http://127.0.0.1:30100","model":"deepseek-v4-flash","timeout_seconds":1800}}`。网页新批次的 Epsilon 每平台跨批次最多并发 10 条任务，每条独立 session，其他平台可并行；旧批次保留原串行配置。接入功能本身不会自动补跑历史业务任务。

## 工具额外 Token（AgentRuntime）

从 `tool_exec_completed.payload.usage` 单独统计工具内部用量；输入与缓存语义和主模型一致，合计 input + output + cache_read + cache_write。`details` 仅为模型分项，不与平铺总量再次累加。工具模型名单独保留，不替换主模型名称。

按本次 SSE 事件序号去重；同一工具 ID 的不同执行阶段若分别报告用量，分别计入。工具调用次数仍按 ID 去重。某些工具（如配置的搜索脚本）由运行时固定记账，该字段表示接口报告用量，不冒充供应商实测消耗。

新日志的 `epsilon.session.capture_capabilities.tool_usage=true` 表示保留了工具用量字段。已开启采集但没有任何 usage 报告时显示“0 / 接口未报告额外用量”；已报告但字段不完整时标为缺失或部分记录。老日志没有采集标记且没有工具 usage 时显示“未采集”，不能当作零。已有主模型总/输入/输出/缓存 Token 与缓存率口径不变，工具额外 Token 不自动加进主模型总量。
