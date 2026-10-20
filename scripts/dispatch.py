"""Explicit publishing adapters. No shell interpolation or auth scraping."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.error
import urllib.request


class DispatchUncertain(RuntimeError):
    """The server may have accepted a write; never resend automatically."""


class ConfigurationError(RuntimeError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "API redirect rejected", headers, fp)


def workbuddy_request(method, suffix, token, data=None):
    url = "https://www.workbuddy.cn/openapi/v2/localassistant" + suffix
    req = urllib.request.Request(url, data=json.dumps(data, ensure_ascii=False).encode("utf-8") if data else None,
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json", "Accept": "application/json"}, method=method)
    try:
        with urllib.request.build_opener(NoRedirect).open(req, timeout=30) as response:
            obj = json.load(response)
    except (OSError, ValueError) as exc:
        if method == "POST":
            raise DispatchUncertain("WorkBuddy 提交未得到可确认回执；可能已接受，禁止自动重发") from exc
        raise ConfigurationError("无法确认 WorkBuddy 本地助理在线状态或授权；未发送任务") from exc
    if obj.get("code") != 0:
        raise ConfigurationError(f"WorkBuddy API 返回 code={obj.get('code')}；需要检查授权/客户端")
    return obj.get("data") or {}


def publish_workbuddy(prompt, config, request=workbuddy_request):
    env = config.get("token_env", "WORKBUDDY_ACCESS_TOKEN")
    token = os.environ.get(env)
    if not token:
        raise ConfigurationError(f"未设置 {env}；需要已授权的 WorkBuddy Open API access_token")
    online = request("GET", "", token)
    if online.get("online") is not True:
        raise ConfigurationError("WorkBuddy 本地助理不在线；未发送任务")
    result = request("POST", "/message", token, {"content": prompt, "msg_type": "text"})
    mid = result.get("message_id")
    if not isinstance(mid, str) or not mid:
        raise DispatchUncertain("WorkBuddy 接口未返回 message_id；不得自动重复提交")
    return {"message_id": mid, "submitted": True,
            "workspace_binding": "Open API 不提供 cwd/session 参数，需通过日志匹配并核验实际工作目录"}


def resolve_program(executable):
    """Avoid Windows cmd/bat shell interpretation; support the npm Codex shim."""
    path = shutil.which(executable) or (str(Path(executable).expanduser().resolve()) if Path(executable).expanduser().is_file() else None)
    if not path:
        raise ConfigurationError(f"找不到可执行程序：{executable}")
    p = Path(path)
    if p.suffix.lower() in (".cmd", ".bat"):
        # Official npm installs have this layout; do not execute arbitrary cmd shims.
        js = p.parent / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
        node = shutil.which("node")
        if p.stem.lower() == "codex" and js.is_file() and node:
            return [node, str(js)]
        raise ConfigurationError("Windows .cmd/.bat 不直接执行；请配置原生 .exe 或 [python.exe, script.py] / [node.exe, script.js]")
    return [path]


def codex_command(config, workspace, final_path):
    sandbox = config.get("sandbox", "workspace-write")
    if sandbox not in ("read-only", "workspace-write"):
        raise ConfigurationError("发布器仅接受 read-only/workspace-write；不自动关闭沙箱")
    argv = resolve_program(config.get("executable", "codex"))
    policy = config.get("interaction_policy", {})
    if policy.get("permissions") == "approve" or policy.get("questions") == "recommended":
        node = resolve_program(config.get("node", "node"))
        command = node + [str(Path(__file__).with_name("codex-interactive.mjs")), "--server-argv", json.dumps(argv+["app-server", "--stdio"]),
                          "--workspace", str(workspace), "--output", str(final_path), "--sandbox", sandbox,
                          "--questions", policy.get("questions", "manual"), "--permissions", policy.get("permissions", "manual"),
                          "--timeout", str(config.get("timeout_seconds", 900))]
        if config.get("model"):
            command += ["--model", str(config["model"])]
        return command
    argv += ["-a", "never", "exec", "--json", "--color", "never", "--skip-git-repo-check", "--sandbox", sandbox,
             "-C", str(workspace), "--output-last-message", str(final_path)]
    if config.get("model"):
        argv += ["--model", str(config["model"])]
    argv += ["-"]
    return argv


def adapter_command(config, values):
    argv = config.get("argv")
    if not isinstance(argv, list) or not argv or any(not isinstance(x, str) for x in argv):
        raise ConfigurationError("command 发布器必须配置 argv 字符串数组")
    try:
        expanded = [item.format_map(values) for item in argv]
    except (ValueError, KeyError) as exc:
        raise ConfigurationError("argv 仅支持 experiment_id/run_id/prompt_file/workspace/receipt_file 占位符") from exc
    return resolve_program(expanded[0]) + expanded[1:]


def desktop_command(config, workspace, prompt_file, receipt_file):
    """Use the WorkBuddy Desktop bridge, never the standalone codebuddy executable."""
    node = resolve_program(config.get("node", "node"))
    is_jahead = config.get("driver") == "jahead_desktop"
    script = Path(__file__).with_name("jahead-desktop.mjs" if is_jahead else "workbuddy-desktop.mjs")
    port = str(config.get("port", 9336 if is_jahead else 9335))
    probe = node+[str(script), "doctor", "--port", port]
    if config.get("target"):
        probe += ["--target", str(config["target"])]
    if is_jahead and config.get("module"):
        probe += ["--module", str(config["module"])]
    try:
        check = subprocess.run(probe, capture_output=True, text=True,
                               encoding="utf-8", timeout=45, shell=False)
    except subprocess.TimeoutExpired as exc:
        raise ConfigurationError(f"{script.name} 连接检查超时；未发送任务，请检查桌面模块加载状态") from exc
    try:
        info = json.loads(check.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise ConfigurationError("桌面 CLI 检查失败；确认 Node.js 22+ 与本地控制端口") from None
    if check.returncode != 0 or not info.get("ok") or info.get("profile") == "unsupported":
        detail = info.get("error") or ("桌面版本尚未适配" if info.get("profile") == "unsupported" else "未返回可用连接")
        raise ConfigurationError(f"{script.name} 连接检查失败：{detail}；未发送任务")
    argv = node+[str(script), "submit", "--port", port, "--workspace", str(workspace), "--prompt-file", str(prompt_file),
                 "--receipt-file", str(receipt_file), "--wait", "--timeout", str(config.get("timeout_seconds", 900))]
    for key in (("mode", "title", "target", "web-search", "module", "stage", "project", "assistant", "search-type") if is_jahead else ("model", "mode", "title", "target")):
        if config.get(key):
            argv += ["--"+key, str(config[key])]
    for key, value in ({} if is_jahead else config.get("interaction_policy", {})).items():
        if key in ("questions", "permissions"):
            argv += ["--"+key, value]
    return argv


def mesh_command(config, prompt_file, receipt_file):
    from mesh_api import MeshClient
    try:
        MeshClient.from_config(config).doctor(config.get("model"))
    except (ValueError, OSError) as exc:
        raise ConfigurationError(f"Mesh API 连接检查失败：{exc}；未发送任务") from exc
    argv = [sys.executable, str(Path(__file__).with_name("mesh_api.py")), "submit",
            "--prompt-file", str(prompt_file), "--receipt-file", str(receipt_file), "--wait",
            "--timeout", str(config.get("timeout_seconds", 1800))]
    for key in ("base_url", "company_id", "project_id", "agent_id", "token_env", "model"):
        if config.get(key):
            argv += ["--" + key.replace("_", "-"), str(config[key])]
    return argv


def epsilon_command(config, prompt_file, receipt_file, workspace):
    from epsilon_api import EpsilonClient
    try:
        EpsilonClient(config).doctor(config.get("model"))
    except (ValueError, OSError) as exc:
        raise ConfigurationError(f"Epsilon API 连接检查失败：{exc}；未发送任务") from exc
    argv = [sys.executable, str(Path(__file__).with_name("epsilon_api.py")), "submit",
            "--prompt-file", str(prompt_file), "--receipt-file", str(receipt_file),
            "--workspace", str(workspace)]
    for key in ("base_url", "user_id", "company_id", "project_id", "agent_id", "token_env", "model", "mode"):
        if config.get(key):
            argv += ["--"+key.replace("_", "-"), str(config[key])]
    return argv


def cancel_epsilon_task(receipt_file):
    from epsilon_api import EpsilonClient, cancel
    path = Path(receipt_file)
    if not path.is_file():
        return False
    receipt = json.loads(path.read_text())
    if not receipt.get("run_id") or not receipt.get("submitted"):
        return False
    result = cancel(EpsilonClient(receipt["connection"]), receipt)
    return result.get("cancelled") is True


def cancel_mesh_task(receipt_file):
    from mesh_api import MeshClient, cancel
    path = Path(receipt_file)
    if not path.is_file():
        return False
    receipt = json.loads(path.read_text())
    if not receipt.get("run_id") or not receipt.get("submitted"):
        return False
    cancel(MeshClient.from_config(receipt["connection"]), receipt, path)
    return True


def cancel_desktop_task(config, session_id, receipt_file=None):
    node = resolve_program(config.get("node", "node"))
    is_jahead = config.get("driver") == "jahead_desktop"
    argv = node+[str(Path(__file__).with_name("jahead-desktop.mjs" if is_jahead else "workbuddy-desktop.mjs")), "cancel", "--port", str(config.get("port", 9336 if is_jahead else 9335)),
                 "--session", str(session_id)]
    if config.get("target"):
        argv += ["--target", str(config["target"])]
    if is_jahead and receipt_file:
        argv += ["--receipt-file", str(receipt_file)]
        if config.get("module"):
            argv += ["--module", str(config["module"])]
    result = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", timeout=20, shell=False)
    try:
        return result.returncode == 0 and json.loads(result.stdout.strip().splitlines()[-1]).get("type") == "cancel.requested"
    except (ValueError, IndexError):
        return False


def spawn(argv, workspace, env=None):
    kwargs = {"cwd": str(workspace), "stdin": subprocess.PIPE, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE,
              "text": True, "encoding": "utf-8", "errors": "replace", "bufsize": 1, "shell": False, "env": env}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(argv, **kwargs)
