# 即先桌面 CLI：四个任务入口与资源管理

需要 Node.js 22+。支持 macOS / Windows 路径与无 shell 启动；目前真机验证为 macOS、即先 1.0.17。所有操作在已登录的即先桌面内执行，复用各模块自己的会话。认证值只用于原模块请求，不导出到命令行、日志或安装包。

报告入口已核验 2026-10-20 的 `index-8hWVE1So.js`，同时保留原 `index-vlalPs-Y.js` 适配；根据实际加载的已核验入口选择接口模块。新入口的认证导出、会话创建/查询、生成流和取消协议已核对，并验证真实报告提交回执。未知版本仍拒绝执行，不按任意脚本名自动放行。

## 启动与选择模块

首次正常退出空闲即先后执行 `node scripts/jahead-desktop.mjs launch`。CLI 自动打开需要的模块，不再要求手动停留在“报告”。本地端口默认为 127.0.0.1:9336；客户端更新或自行重启后需要再次通过 launch 启动。

```text
node scripts/jahead-desktop.mjs modules
node scripts/jahead-desktop.mjs doctor --module create
node scripts/jahead-desktop.mjs doctor --module developer
node scripts/jahead-desktop.mjs doctor --module report
node scripts/jahead-desktop.mjs doctor --module search
```

`--module` 支持 `create/创作/创造`、`developer/开发`、`report/报告`、`search/搜索`。默认 report。资源模块为 experts、skills、knowledge、hardware。

macOS 优先 ~/Applications/即先 AI.app，再查 /Applications/即先 AI.app。Windows 可明确指定实际安装位置：

```powershell
node scripts/jahead-desktop.mjs launch --app "C:\Users\用户名\AppData\Local\Programs\即先 AI\即先 AI.exe"
```

CLI 不强杀现有客户端。前端版本不匹配会停止；应更新适配器，不能跳过构建检查。

macOS 通过 LaunchServices（`open -a … --args …`）启动签名应用，并保留本机调试参数，避免直接运行包内可执行文件时继承终端/调用程序的权限归属。若安装目录不支持更新，可使用用户目录 `~/Applications/即先 AI.app`；应保留旧版备份并在没有进行中任务时切换，启动后核验应用自身的更新权限状态。

## 四个任务入口

```text
node scripts/jahead-desktop.mjs submit --module create --prompt-file task.txt --receipt-file create-run.json --wait
node scripts/jahead-desktop.mjs submit --module developer --stage requirement --prompt-file task.txt --title "测试系统" --receipt-file dev-run.json --wait
node scripts/jahead-desktop.mjs submit --module report --prompt-file task.txt --receipt-file report-run.json --mode expert --web-search true --wait
node scripts/jahead-desktop.mjs submit --module search --prompt-file task.txt --receipt-file search-run.json --search-type full_web_search --wait
```

- **创造**：使用已登录 Master（阿吉）的真实 IM 发送入口；复用空白会话或新建会话。发送前检查原生当前会话与草稿，不覆盖已有草稿；原生 trace_done 用于确认完成，不把 idle 当成完成。现有普通助理支持查询/管理，当前自动任务提交入口是 Master。
- **开发**：创建项目/需求会话，建立原生 SSE 通道后提交，保存 project_id、conversation_id、record_id。`--stage requirement` 用于需求；`--stage coding --project ID` 先走原生“开始开发”流程，再发送编码指令；`console` 适用于已有项目的控制台会话。编码前必须完成真实需求生成，后端拒绝时会返回明确错误，不绕过流程。
- **报告**：复用报告创建和流式生成接口，专家/快速模式可选，默认专家且联网开启。
- **搜索**：解析需求、获得原生 task_id 并订阅任务流。意图解析已创建任务时不重复创建。支持 full_web_search、single_source_direct、multi_source_direct；当前 CLI 新建 single_task，不擅自创建循环或持续任务。

在开发需求已经正式生成后启动编码：

```text
node scripts/jahead-desktop.mjs submit --module developer --project 实际project_id --stage coding --prompt-file coding.txt --receipt-file coding-run.json --wait
```

若当前需求仍只是一段聊天概述，应先用需求阶段让 Agent 保存正式需求文档、完成原生需求流程，再开始编码。CLI 不把阶段切换成功当作代码产物验收通过。

## 查询、停止、导出

非报告任务建议使用回执，自动带回所需模块与身份字段：

```text
node scripts/jahead-desktop.mjs status --receipt-file create-run.json
node scripts/jahead-desktop.mjs wait --receipt-file dev-run.json --timeout 300
node scripts/jahead-desktop.mjs cancel --receipt-file search-run.json
node scripts/jahead-desktop.mjs export --receipt-file dev-run.json --output dev-result.jsonl
```

报告也可直接指定 `--module report --session rs_实际ID`。`export` 导出规范化的任务状态、公开回答和事件摘要为 JSONL；不是附件 ZIP 下载命令。原生结果附件是否存在需要另行核验。

每个新任务使用独立回执文件；相同回执不会再次发送。写请求连接中断保存 `dispatch_unknown`，应先查询原任务。不根据文件最新时间猜测会话。`wait` 超时只停止观察，`cancel` 才显式停止任务。遇到 AskUser、工作流卡片或接管请求返回 needs_input，由用户在桌面处理；不填写未知答案。

## 资源管理

统一使用 `manage --module ... --action ...`。写入数据来自明确的 `--data-file request.json`；`--dry-run` 只显示将调用的受限操作，不发请求。不提供任意 URL、任意 eval、强制卸载或绕过资源权限的命令。

| 模块 | 操作 | 说明 |
|---|---|---|
| create | list/get/create/update/delete | 管理用户助理实例；写入字段由原生助理接口校验 |
| experts | list/get/create-assistant | 官方专家目录只读；create-assistant 启动专家创建助理工作流，可能需要后续表单 |
| skills | list/get/markdown/files/create/import/install/update/delete | create/import 通过 ZIP 导入创建技能；update 是已安装技能的目标版本更新，不能编辑平台官方技能源代码 |
| knowledge | list/get/create/update/delete/documents | 知识库及文档列表；创建必须明确 title 与 scope_user_ids |
| hardware | list/get/create/update/delete/logs | 原生设备管理，创建需要真实 imei；不包含购买、付款或伪造硬件 |

```text
node scripts/jahead-desktop.mjs manage --module experts --action list
node scripts/jahead-desktop.mjs manage --module experts --action get --id 专家ID
node scripts/jahead-desktop.mjs manage --module create --action update --id 助理ID --data-file assistant-update.json --dry-run
node scripts/jahead-desktop.mjs manage --module skills --action create --file skill.zip
node scripts/jahead-desktop.mjs manage --module skills --action install --data-file install.json
node scripts/jahead-desktop.mjs manage --module skills --action update --id 已安装技能ID --data-file version.json
node scripts/jahead-desktop.mjs manage --module knowledge --action create --data-file library.json
node scripts/jahead-desktop.mjs manage --module hardware --action get --id 设备ID
```

示例请求：知识库 `{"title":"测试资料","scope_user_ids":[]}`（权限含义以原生服务为准）；安装技能 `{"skill_id":"技能ID","assistant_ids":["助理ID"]}`；更新版本 `{"target_version":"实际版本号"}`。删除必须提供准确 ID，可能无法恢复；本次实测没有删除用户现有数据。设备与权限配置按原生服务校验，不自动覆盖确认冲突。

技能管理复用“创造”内置的同版技能服务，避免独立技能页的旧版共享依赖在初始化前被调用。

## 统一编排

```json
{"providers":{"jahead":{"driver":"jahead_desktop","module":"search","node":"node","port":9336,"timeout_seconds":900}}}
```

`module` 可选四个入口；开发还可配置 stage/project，报告可配置 mode/web-search，搜索可配置 search-type。prepare/launch/status/watch/export 自动保存回执、事件、最终文本，cancel 从回执取完整身份。一个实验中一个 Jahead provider；需要测试多个模块时，分别创建实验并明确任务范围。

本地 workspace 不自动映射到即先云端。统一编排含 input_files 时会在发布前拦截，避免声称即先读到了本地文件。资源管理的技能 ZIP 导入是独立、明确的上传操作。

## 指标与验证边界

模型 Token/完整请求延迟没有原始证据则留空。报告的 task_usage.token_count 是计费口径，单列；工具计数仅为已观察事件，不猜没有 ID 的配对延迟。总时长为客户端提交到终态观测跨度。

macOS 已实测报告、创造 IM、开发需求→编码、搜索的真实提交/状态/等待/结果导出；也实测四类资源列表查询。管理写入有协议测试，未对用户现有专家、技能、知识或硬件执行破坏性验证。Windows 尚未真机验证。各次实测与限制记录在交付目录 validation.json 中。

本次编码测试原生终态为 completed，公开回答包含 ARC_CODING_OK；原生 Agent 报告了代码文件与其测试结果。CLI 已验证回执、状态、回答和导出，但没有独立下载源代码复跑其项目测试。
