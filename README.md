# agent-lab

一个本地运行的 Agent 任务发布与横向评测工具。保留 Excel 导入、任务勾选、批量发布和批次进度，在同一张表中对比 **Codex、WorkBuddy、即先 AI、Mesh API、Epsilon API**。

当前版本：**4.5.1**。仓库包含工具源码、模板、测试与 `agent-run-compare` 技能定义，不包含真实任务记录或服务端凭据。

## 功能

- **任务发布**：导入 XLSX、映射列、搜索与勾选任务，也可手动添加。
- **执行追踪**：独立任务目录和会话，记录回执、模型、执行状态、公开回答与日志。
- **并发队列**：新批次的 Mesh、Epsilon 各最多并发 **10** 条；其他三个平台各串行执行。并发上限跨批次共享，已有批次沿用原配置。
- **五平台同表**：切换批次和任务查看指标；没有执行记录的平台保留空列。
- **指标**：模型 Token、缓存率、任务耗时、工具调用与错误数、响应均值/P95、首 Token 耗时，以及 Epsilon 工具额外 Token。
- **可靠性**：重复发布请求不重发；未知提交状态需核查；重启后未发送队列暂停；刷新保留页面、批次与任务选择。

## 快速开始

需要 Python **3.10+**。Python 服务使用标准库，无需安装第三方运行依赖。桌面平台适配器还需要 Node.js **22+** 及对应已登录的客户端。

```bash
git clone https://github.com/narutoZed/agent-lab.git
cd agent-lab
python3 -m venv .venv
.venv/bin/python scripts/task_console.py --root ./runs --port 18749
```

Windows PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe scripts/task_console.py --root .\runs --port 18749
```

打开 [本地发布台](http://127.0.0.1:18749/)：

1. 下载页面上的 Excel 模板，或使用 [任务模板](assets/task-list-template.xlsx)。
2. 导入任务，确认列映射并勾选需要执行的行。
3. 选择平台、模型和连接参数。
4. 核对后发布，在“批次进度”查看状态。
5. 批次结束后进入“指标对比”，查看同一任务的五平台结果。

导入和勾选不会调用模型，最终发布才会真实执行任务。控制台仅监听本机，请通过 HTTP 地址使用，不要直接打开 `assets/console.html`。

## 平台接入

| 平台 | 接入方式 | 配置说明 |
| --- | --- | --- |
| Codex | 已登录的 CLI / app-server | [任务编排](references/orchestration.md) |
| WorkBuddy | 桌面控制 CLI | [桌面接入说明](references/desktop-cli.md) |
| 即先 AI | 桌面控制 CLI，支持多个模块 | [即先接入说明](references/jahead-cli.md) |
| Mesh | HTTP 提交与分页 session-log | [Mesh API](references/mesh-api.md) |
| Epsilon | HTTP/SSE 提交与执行追踪 | [Epsilon API](references/epsilon-api.md) |

Mesh 默认示例地址为 `http://127.0.0.1:18004`；Epsilon 为 `http://127.0.0.1:30100`。在发布台的连接设置中填写自己的服务地址。Epsilon 支持明确的内网 IP，远程请求使用服务端工作目录，不传入本机路径。模型列表从对应服务读取。

桌面适配器使用客户端内部接口，客户端升级可能需要更新适配。Windows 路径已兼容，尚未完成 Windows 真机端到端验证。

## 指标口径

- 缺失用量和时间显示 `—`，不按字数估算 Token。
- 缓存率为 **缓存读取 Token ÷ 含缓存读写的输入 Token**。缓存记录不全时显示下界，分母缺失或为零时留空。
- 工具错误按明确错误标记统计。没有调用 ID 的来源按错误结果事件计数，并注明口径。
- Epsilon 的主模型用量来自 `turn_completed.usage`；工具额外用量来自 `tool_exec_completed.usage`，单列展示，不重复累加 `details`。旧日志未采集工具用量时保持缺失。
- 不同平台的计时来源可能不同，报告会标明来源；没有可靠 LLM 请求边界时不推算耗时。
- 执行结束与业务验收分开。模型评价、人工复核和证据记录见 [评测方法](references/evaluation.md)。

完整定义见 [指标与证据契约](references/contract.md)。

## 命令行

查看已有日志：

```bash
python scripts/compare.py discover --only codex --out inventory.json
python scripts/compare.py discover --mesh mesh-session.jsonl --only mesh --out inventory.json
python scripts/compare.py discover --epsilon epsilon-session.jsonl --only epsilon --out inventory.json
```

发布任务前，复制 [任务配置示例](assets/task.example.json)，填写任务和验收项，并删除不使用的平台：

```bash
python scripts/orchestrate.py prepare --task task.json --out experiment-001
python scripts/orchestrate.py launch --experiment experiment-001
python scripts/orchestrate.py watch --experiment experiment-001 --seconds 300
python scripts/orchestrate.py export --experiment experiment-001 --out comparison-001
```

`prepare` 只准备文件，`launch` 才调用目标平台。网页批次并发由发布台管理。详细用法见 [网页发布台](references/web-console.md) 和 [任务编排](references/orchestration.md)。

## 验证

```bash
python -m unittest discover -s tests -v
node --test tests/*.test.mjs
node --check assets/console.js
```

自动化测试使用本地模拟执行器和模拟 HTTP 客户端，不发送真实模型任务。

## 目录

```text
scripts/       发布、调度、平台适配与指标解析
assets/        发布台、报告页面、任务模板
tests/         Python / Node.js 测试
references/    接入、使用与指标说明
agents/        技能界面配置
SKILL.md       agent-run-compare 技能入口
```

运行数据保存在 `--root` 指定目录。`.gitignore` 已排除常见日志、虚拟环境、回执、报告目录和凭据文件；不要把真实任务数据提交到仓库。
