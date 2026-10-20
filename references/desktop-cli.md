# WorkBuddy 桌面控制 CLI

本 CLI 连接**已安装的 WorkBuddy 桌面应用**，通过桌面主窗口暴露的 `__wbInvoke("wb:conversations:<method>", context, ...args)` 调用其原生 conversations 服务。它不运行独立 `codebuddy` 命令，不使用 WorkBuddy 开放平台，也不读取或搬运登录令牌。

适配依据：国内版 WorkBuddy 5.8.3 的 `preload/index.js` 和 `main/log-acl-guard.js` 中的桌面桥接契约。当前 profile 为 5.8.x；其他大/小版本写操作拒绝执行，需重新核对接口。此接口属于桌面内部接口，客户端升级可能需要更新适配器。

## 环境与启动

需要 Node.js 22+；Windows/macOS 使用相同的两个脚本：

```text
scripts/workbuddy-desktop.mjs
scripts/desktop_cdp.mjs
scripts/interaction_policy.mjs
```

先正常退出空闲的 WorkBuddy，然后运行：

```text
node <skill>/scripts/workbuddy-desktop.mjs launch --port 9335
node <skill>/scripts/workbuddy-desktop.mjs doctor --port 9335
```

未找到安装路径时：

```text
# macOS 国内版
node <skill>/scripts/workbuddy-desktop.mjs launch --app "/Applications/WorkBuddy.app"

# Windows（替换成实际安装位置）
node <skill>/scripts/workbuddy-desktop.mjs launch --app "C:/Users/你的用户名/AppData/Local/Programs/WorkBuddy/WorkBuddy.exe"
```

launch 只为这次桌面进程设置 `WORKBUDDY_REMOTE_DEBUGGING_PORT`，使用 `127.0.0.1` 控制入口，不修改应用文件或永久环境变量。调试接口具有桌面控制能力，不应映射到局域网/公网。退出该客户端并从正常图标重新启动即可不再带此控制入口。

如果客户端已经运行但未开启控制端口，launch 不能给现有进程补上启动参数，会提示正常退出后重试；不会强制结束已有任务。客户端登录仍在原来的桌面界面处理；任务问答和工具审批按下述策略处理。同一配置目录、版本和平台的多个窗口共享会话服务，CLI 优先使用聚焦/可见窗口，再按稳定 ID 选择；doctor 输出窗口数量及选择依据。配置目录不同或身份不足时仍要求通过 `--target` 明确选择，不跨配置自动切换。

## 命令

正文放入 UTF-8 `task.txt`。工作目录必须存在。

```text
node <skill>/scripts/workbuddy-desktop.mjs sessions --limit 20

node <skill>/scripts/workbuddy-desktop.mjs submit --prompt-file task.txt --workspace "工作目录" --receipt-file receipt.json

node <skill>/scripts/workbuddy-desktop.mjs submit --prompt-file task.txt --workspace "工作目录" --receipt-file receipt.json --wait --timeout 300

node <skill>/scripts/workbuddy-desktop.mjs status --session 会话ID --request 请求ID
node <skill>/scripts/workbuddy-desktop.mjs wait --session 会话ID --request 请求ID --timeout 300
node <skill>/scripts/workbuddy-desktop.mjs cancel --session 会话ID
```

也可以从 stdin 传正文。参数通过 JSON 进入桌面调用，正文不拼接成 shell 或 JavaScript 代码。默认创建本地新会话，cwd 指向 workspace。没有显式指定 `--model`、`--mode` 时由桌面新会话配置决定；回执记录实际 model/mode/permission_mode。CLI 默认按当前用户授权选择明确推荐项，并逐次批准本任务的工具权限请求；不修改全局模型或永久权限规则，不提供任意 eval、删除会话或写数据库命令。

输出是 JSONL，方便被 Python/其他评测器消费：

```json
{"type":"submission.receipt","submitted":true,"session_id":"...","request_id":"...","selector":{"request_id":"..."},"transport":"workbuddy_desktop"}
{"type":"execution.status","status":"running","session_id":"..."}
{"type":"execution.completed","status":"completed","session_id":"..."}
```

回执必须在观察到桌面请求记录后才标 submitted=true；仅发出 RPC 不作为已接受的证据。相同 receipt-file 再次调用不重发；正文/目录/显式配置不同则拒绝复用。创建或发送超时保留原会话/请求 ID 及未知状态，使用 status 核查，不自动重试。wait 超时只停止观察，客户端任务继续；取消任务使用 cancel 并再次查询状态确认。

## 自动问答与权限

当前默认 `--questions recommended --permissions approve`：AskUser 选明确标记为推荐的项；标准工具权限选允许（优先本次允许）。缺少推荐项或需自由填写时保持待处理。需要人工处理时使用 `--questions manual` 或 `--permissions manual`。

自动动作输出 `interaction.resolved` JSONL，也保存到 `~/.workbuddy-desktop-cli/interactions/`。只处理命令中指定/本 CLI 创建的会话，不修改其他桌面任务。

## 与评测技能连接

任务配置使用：

```json
{
  "providers": {
    "workbuddy": {
      "driver": "workbuddy_desktop",
      "node": "node",
      "port": 9335,
      "timeout_seconds": 900
    }
  }
}
```

通过原有 `orchestrate.py prepare/launch/watch/serve/export` 流程运行。桌面 CLI 直接创建指定工作目录的原生会话，评测器继续从 `.workbuddy/projects` 或 `.workbuddy-ai/projects` 读 Token、工具调用及耗时。不会将 API 凭据作为此后端的前提，也不会自动退回开放平台或 CodeBuddy CLI。

Windows 使用 `node.exe`（或配置其绝对路径）和 `py -3`；路径带空格时加引号，不需要 Bash、PowerShell UI 自动化模块或浏览器驱动下载。Windows 的真实客户端联调状态应单独报告，不把 macOS/模拟接口测试当作 Windows 实测。

## 开发验证

```text
node --test <skill>/tests/desktop.test.mjs
python -m unittest discover -s <skill>/tests -v
```

源码只基于已检查的桌面契约发起调用；doctor 读取版本、配置目录和桥接能力，不输出 Cookies、会话种子、登录 token 或连接器密钥。请求状态不等同于业务验收结果，仍需模型评价和人工复核。
