# 同任务发布与追踪 v4

WorkBuddy 默认使用 [桌面控制 CLI](desktop-cli.md)（driver=workbuddy_desktop）。以下 Open API 内容保留为用户显式选择的可选后端，不是桌面 CLI 的安装或授权前提。

## 平台边界

| 平台 | 发布 | 实时追踪 | 结束依据 |
|---|---|---|---|
| Codex | 已登录的 Codex app-server（自动交互）或 exec（手动策略），独立 cwd | CLI 事件 + thread_id 定位本机 rollout，优先原生日志 | turn.completed / task_complete，失败/中断单独保留 |
| WorkBuddy 桌面 CLI（默认） | 连接原生桌面窗口，在指定 cwd 创建会话并发送任务 | 桌面会话/请求状态 + 同机 JSONL | 真实请求终态；按已授权策略自动选推荐项和批准工具权限 |
| WorkBuddy 本地助理 | 官方 Open API：先查在线再 POST 文本消息 | 用共同任务编号定位本机 JSONL，callId 配对；可读原生 SQLite 的已知状态 | 原生日志/数据库明确结束；只有回答时标 answer_observed，静默不等于结束 |
| Jahead | 默认 `jahead_desktop` 控制已登录的即先创造/开发/报告/搜索模块 | 会话状态、答案与工具事件快照自动接入 | 模型 Token/延迟留空，计费 Token 单列；本地附件未实现 |

官方资料：[Codex CLI](https://learn.chatgpt.com/docs/cli/reference)、[WorkBuddy 本地助理接口](https://open.workbuddy.cn/en/docs/openapi#local-assistant-api)。实现依据核对日期 2026-10-19。没有使用 WorkBuddy 云端 `/tasks` API。

## WorkBuddy 授权与接口

WorkBuddy 开放平台需要用户授权第三方应用取得 access_token；客户端登录态不是这个凭据。应用需具备以下 scopes：

- `user.localassistant.readable`
- `user.localassistant.invokable`

调用顺序：

```text
GET  https://www.workbuddy.cn/openapi/v2/localassistant
POST https://www.workbuddy.cn/openapi/v2/localassistant/message
Authorization: Bearer <环境变量中的 access_token>
Content-Type: application/json

{"content":"三个平台相同的任务文本","msg_type":"text"}
```

GET 响应 `data.online=true` 才 POST；POST 成功保存 `data.message_id`。这不是 session_id，不能按其名字直接找本机文件。API 不允许用 permission_response 替代用户的本地审批；该可选 API 后端不能通过此接口回答审批；自动交互使用默认桌面 CLI 后端。

凭据仅通过 `token_env` 指定的环境变量读取，默认 `WORKBUDDY_ACCESS_TOKEN`。不要放入 task.json、argv、报告或聊天。PowerShell 可在用户自己的终端设置当前进程环境变量：

```powershell
$env:WORKBUDDY_ACCESS_TOKEN = [System.Net.NetworkCredential]::new('', (Read-Host 'WorkBuddy Open API access_token' -AsSecureString)).Password
```

然后在同一终端运行 launch。macOS/Linux 可通过现有密钥管理器设置环境变量。过期或权限不足时由用户按官方 OAuth 流程更新；本技能不抓取私有 Cookie/应用认证文件，也不代替用户注册开放平台应用。启动 Codex/Jahead 子进程时剥离其他平台配置的 token_env。

本地助理必须在执行机器上在线；追踪器应运行在存有日志的同一机器，或配置明确可访问的日志目录。若 API 实际连接了另一台 PC，当前机器无法读取它的本地日志，状态保持已提交/待绑定，不假装任务没运行。

该接口只有 `content`、`msg_type`，没有 cwd、模型、附件参数。包含 input_files 的实验在此 driver 下拒绝自动提交，防止假装文件已送达。需要文件任务时可先在所有目标平台提供同一可访问数据源并在统一 prompt 中说明，或使用经过验证、能绑定 cwd 的 command 桥接器。普通文本任务可直接使用本地助理 API。

## 任务定义与目录

使用 assets/task.example.json。关键字段：

- `prompt`：用户明确授权的新任务，不是从日志中擅自抽出的指令。
- `criteria`：统一的完成效果验收标准。
- `providers`：codex/workbuddy/jahead，可仅选部分平台；同一实验每平台一次独立执行，重复实验用新目录。
- `input_files`：显式文件数组 `[{"source":"./source.csv","name":"source.csv"}]`，source 相对于 task.json。prepare 冻结输入并复制到每个平台 workspace/input，记录 SHA256。不能用越界路径覆盖其他文件。
- `context_id`、`alignment_confirmed`：输入/权限/任务范围的可比性声明，默认不自动确认。
- `log_roots`：可选 `{"codex":["~/.codex/sessions"],"workbuddy":["~/.workbuddy-ai/projects","~/.workbuddy/projects"]}`。相对目录在 prepare 时以 task.json 目录解析。

```text
experiment-001/
  experiment.json       # 冻结任务与适配器配置，不含密钥
  prompt.txt            # 三平台完全一致，含同一个追踪编号
  input/                # 输入快照
  live-status.json       # 最近一次观察结果
  runs/<platform>/
    prompt.txt
    workspace/          # Codex/command 独立任务目录
    state.json          # 发布与执行状态
    dispatch.lock       # 持久防重发标记
    receipt.json        # 提交回执（可选）
    events.jsonl        # CLI/桥接器事件（可选）
    snapshot.jsonl      # 已完整写入行的只读来源快照
    normalized.json    # 解析缓存
    submission.zip     # 人工渠道任务包
```

所有 prompt 都带相同 `[评测任务编号: arc-...]`，其余任务正文不因平台而改变。原始业务 prompt、发布文本哈希、输入哈希分别保留。带相同编号不证明工具权限/数据源完全相同，仍需核对可比性。

## 命令

Windows 把 `python` 换为 `py -3`；macOS/Linux 可用 `python3`。`<skill>` 指本技能目录。路径包含空格时加引号。

```text
python <skill>/scripts/orchestrate.py prepare --task task.json --out experiment-001
python <skill>/scripts/orchestrate.py launch --experiment experiment-001
python <skill>/scripts/orchestrate.py launch --experiment experiment-001 --platform codex --platform workbuddy
python <skill>/scripts/orchestrate.py status --experiment experiment-001
python <skill>/scripts/orchestrate.py watch --experiment experiment-001 --seconds 300 --interval 2
python <skill>/scripts/orchestrate.py serve --experiment experiment-001 --port 18744
```

launch 后后台工作进程继续运行，status/watch/serve 可稍后重新打开，不需要重发任务。状态文件是恢复依据；跨进程 O_EXCL 锁防止重复提交。后台退出但回执不明时是 dispatch_unknown/exited_unconfirmed，不能据此自动重试。需要重跑时确认原任务状态后新建实验；不要删除锁文件当作重试机制。

serve 只监听 `127.0.0.1`，只提供只读实时视图，没有发布或审批 HTTP 接口。浏览器每 2 秒刷新；日志增长才重新解析。客户端 elapsed 是发布器墙钟时间，native duration 是任务轮次时间，口径不同；界面优先显示日志内原生指标。

`cancel --experiment ... --platform codex` 写取消请求，由拥有子进程的发布器终止该子进程；不按可能复用的 PID 杀其他进程，也不保证远端独立任务已取消。对已提交的 WorkBuddy API 任务，在 WorkBuddy 客户端停止；这里没有猜测或调用未公开取消接口。

## 人工提交与绑定

即先 CLI 使用见 [jahead-cli.md](jahead-cli.md)。显式选择 `manual` 时，Jahead 的 submission.zip 包含 prompt.txt 及输入。人工提交后记录真实回执：

```text
python <skill>/scripts/orchestrate.py submitted --experiment experiment-001 --platform jahead --external-id 实际任务ID
python <skill>/scripts/orchestrate.py bind --experiment experiment-001 --platform jahead --path uploaded.jsonl --selector '{"record_id":"实际记录ID"}'
```

上述 selector 引号适用于 POSIX/现代 PowerShell；Windows PowerShell 5 原生参数转发可能改写引号，可以从 Python 调用 `bind`，或使用下面的免命令行 JSON 转义方式：

```powershell
# 先将 selector 保存成 selector.json，再使用 --selector-file（见脚本帮助）
py -3 <skill>/scripts/orchestrate.py bind --experiment experiment-001 --platform jahead --path uploaded.jsonl --selector-file selector.json
```

WorkBuddy/Codex 自动匹配存在歧义时也可 bind，选择器分别为 request_id/turn_id，来源用 discover 列出。显式 bind 是人工对应关系，不因为正文不同就虚构自动匹配。

## 完成后的评测

```text
python <skill>/scripts/orchestrate.py export --experiment experiment-001 --out frozen-001
python <skill>/scripts/compare.py judge-packet --report frozen-001/report/results.json --out judge-packet.json
```

export 将当前完整日志另存为不可覆盖的新快照，生成 manifest 与报告。未导入平台明确标缺失。然后按 SKILL.md 使用宿主模型主评、人工复核；在任务尚未结束时只展示临时结果，不因文件停止增长就产生成功率。不能把发布器验证小任务当作平台能力评测。

完成模型评价或人工复核并重新构建报告后，调用 `link-evaluation --experiment experiment-001 --report <评测后 results.json>` 将结论同步回实时看板。它重新验证评分引用、当前日志指纹及 rubric_hash；只关联同一个实验的同一个平台执行，不修改原始日志。

## 可选 command 桥接器

仅当用户提供并确认可用的接口/命令时使用，不能猜端口或对应用数据库写入任务。

```json
{"driver":"command","argv":["python","/absolute/path/verified_bridge.py","--prompt-file","{prompt_file}","--workspace","{workspace}","--receipt-file","{receipt_file}"],"timeout_seconds":900}
```

argv 用 subprocess shell=False；任务正文始终通过 stdin/prompt_file，不拼进 shell。桥接器需要记录明确回执文件或 stdout JSON 行：

```json
{"type":"submission.receipt","submitted":true,"session_id":"真实ID","log_path":"已验证的日志路径","selector":{"request_id":"真实请求ID"}}
```

发布器进程退出码 0 只说明调用返回，不能证明 Agent 执行完成。真正的完结事件可显式输出 `execution.completed`/`execution.failed`；等待审批输出 `execution.needs_input`。不允许在回执里保存 access_token。日志路径超出已配置目录时要求显式 bind。

Windows `.cmd/.bat` 不作为 shell 调用。Codex npm `.cmd` 自动解析到同安装目录的 Node JS 入口；其他桥接器用原生 exe 或 `[python.exe, bridge.py]`、`[node.exe, bridge.js]`。Windows 原生运行仍需在实际机器联调，不把 macOS 的兼容测试说成 Windows 实测。
