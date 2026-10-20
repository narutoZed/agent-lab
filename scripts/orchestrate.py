#!/usr/bin/env python3
"""Prepare one task, publish explicitly, monitor local evidence, export for evaluation."""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path, PurePosixPath
import queue
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
import zipfile

from adapters import parse, workbuddy_user_text
from common import digest, redact, text_content, timestamp
from compare import VERSION, resolve, save
from dispatch import (ConfigurationError, DispatchUncertain, adapter_command,
                      codex_command, desktop_command, cancel_desktop_task, publish_workbuddy, spawn,
                      mesh_command, cancel_mesh_task, epsilon_command, cancel_epsilon_task)

ROOT = Path(__file__).resolve().parents[1]
PLATFORMS = ("codex", "workbuddy", "jahead", "mesh", "epsilon")
TERMINAL = {"completed", "failed", "cancelled", "timed_out", "imported"}


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def read(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        if default is not None:
            return copy.deepcopy(default)
        raise


def exp_path(value):
    path = resolve(value)
    return path.parent if path.name == "experiment.json" else path


def load_exp(path):
    root = exp_path(path)
    exp = read(root / "experiment.json")
    if exp.get("schema_version") != 2:
        raise ValueError("需要 schema_version=2 的 experiment.json")
    return root, exp


def run_dir(root, platform):
    if platform not in PLATFORMS:
        raise ValueError("未知平台")
    return root / "runs" / platform


def hash_file(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def prepare(task_path, output):
    task_path = resolve(task_path)
    task = read(task_path)
    prompt = task.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 1_000_000:
        raise ValueError("prompt 必须为 1–1000000 字符的任务正文")
    if not task.get("criteria") or not any(c.get("required", True) for c in task["criteria"]):
        raise ValueError("先定义至少一个必需验收项 criteria")
    seen_criteria = set()
    for criterion in task["criteria"]:
        cid = criterion.get("id")
        if not cid or cid in seen_criteria or not criterion.get("description") or not isinstance(criterion.get("required", True), bool):
            raise ValueError("criteria 需要唯一 id、description 和布尔 required")
        if criterion.get("verification", "answer") not in ("answer", "artifact", "process"):
            raise ValueError("criteria.verification 无效")
        seen_criteria.add(cid)
    providers = task.get("providers", {"codex": {"driver": "codex_cli"}, "workbuddy": {"driver": "workbuddy_desktop"}, "jahead": {"driver": "jahead_desktop"}})
    if not providers or set(providers) - set(PLATFORMS):
        raise ValueError("providers 仅支持 codex/workbuddy/jahead/mesh/epsilon")
    policy = task.get("interaction_policy", {"questions": "recommended", "permissions": "approve"})
    if policy.get("questions") not in ("recommended", "manual") or policy.get("permissions") not in ("approve", "manual"):
        raise ValueError("interaction_policy 需要 questions=recommended/manual，permissions=approve/manual")
    for platform, config in providers.items():
        if config.get("driver") in ("codex_cli", "workbuddy_desktop"):
            config.setdefault("interaction_policy", copy.deepcopy(policy))
        if any(k in config for k in ("token", "access_token", "api_key", "secret", "authorization")):
            raise ValueError("凭据不能写入任务文件；使用 token_env 引用环境变量")
        driver = config.get("driver")
        if driver not in ("codex_cli", "workbuddy_desktop", "jahead_desktop", "mesh_api", "epsilon_api", "workbuddy_openapi", "command", "manual"):
            raise ValueError(f"{platform}: 未知 driver")
        if driver == "codex_cli" and platform != "codex" or driver in ("workbuddy_openapi", "workbuddy_desktop") and platform != "workbuddy" or driver == "jahead_desktop" and platform != "jahead" or driver == "mesh_api" and platform != "mesh" or driver == "epsilon_api" and platform != "epsilon":
            raise ValueError("发布 driver 与平台不匹配")
        if config.get("timeout_seconds", 900) <= 0:
            raise ValueError("timeout_seconds 必须为正数")
    root = resolve(output)
    if root.exists():
        raise ValueError("实验目录已存在；新实验必须使用新目录，不覆盖历史或重发旧任务")
    sources = []
    names = set()
    for spec in task.get("input_files", []):
        source = resolve(spec["source"], task_path.parent)
        name = str(spec.get("name", source.name)).replace("\\", "/")
        rel = PurePosixPath(name)
        if not name or name == "." or not source.is_file() or rel.is_absolute() or ".." in rel.parts or ":" in name or name.casefold() in names:
            raise ValueError("input_files 需要存在的文件和不越界的唯一相对文件名")
        names.add(name.casefold())
        sources.append((source, rel))
    root.mkdir(parents=True)
    experiment_id = "arc-" + uuid.uuid4().hex[:16]
    published_prompt = f"[评测任务编号: {experiment_id}]\n\n{prompt.strip()}"
    (root / "prompt.txt").write_text(published_prompt, encoding="utf-8")
    inputs = []
    for source, rel in sources:
        dest = root / "input" / Path(*rel.parts)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, dest)
        inputs.append({"name": str(rel), "sha256": hash_file(dest), "bytes": dest.stat().st_size})
    exp = {"schema_version": 2, "id": experiment_id, "created_at": now(), "title": task.get("title", prompt[:60]),
           "prompt": prompt, "published_prompt": published_prompt, "prompt_sha256": hashlib.sha256(published_prompt.encode()).hexdigest(),
           "context_id": task.get("context_id", "待核验输入环境"), "alignment_confirmed": task.get("alignment_confirmed", False),
           "criteria": task["criteria"], "input_files": inputs, "providers": providers,
           "interaction_policy": policy,
           "log_roots": {p: [str(resolve(v, task_path.parent)) for v in values] for p, values in task.get("log_roots", {}).items()},
           "note": "同文任务编号用于定位日志；执行结束与业务验收结果分开。"}
    for platform, config in providers.items():
        dest = run_dir(root, platform)
        workspace = dest / "workspace"
        workspace.mkdir(parents=True)
        if inputs:
            shutil.copytree(root / "input", workspace / "input")
        (dest / "prompt.txt").write_text(published_prompt, encoding="utf-8")
        save(dest / "state.json", {"platform": platform, "driver": config["driver"], "status": "prepared", "updated_at": now(),
                                   "run_id": f"{experiment_id}:{platform}", "prompt_sha256": exp["prompt_sha256"]})
        if config["driver"] == "manual":
            with zipfile.ZipFile(dest / "submission.zip", "w", zipfile.ZIP_DEFLATED) as z:
                z.write(dest / "prompt.txt", "prompt.txt")
                for item in inputs:
                    z.write(root / "input" / item["name"], "input/" + item["name"])
    save(root / "experiment.json", exp)
    return root, exp


def preflight(root, exp, platform):
    config = exp["providers"][platform]
    dest = run_dir(root, platform)
    actual = hashlib.sha256((dest / "prompt.txt").read_bytes()).hexdigest()
    if actual != exp["prompt_sha256"]:
        raise ConfigurationError("发布前任务正文被修改；请创建新实验，不发送不同任务")
    for item in exp["input_files"]:
        p = dest / "workspace" / "input" / item["name"]
        if not p.is_file() or hash_file(p) != item["sha256"]:
            raise ConfigurationError("发布前输入快照被修改；请创建新实验")
    driver = config["driver"]
    if driver == "codex_cli":
        return codex_command(config, dest / "workspace", dest / "final.txt")
    if driver == "epsilon_api":
        if exp["input_files"]:
            raise ConfigurationError("Epsilon 当前仅支持纯文本评测任务；未发送附件")
        return epsilon_command(config, dest / "prompt.txt", dest / "epsilon-receipt.json", dest / "workspace")
    if driver == "mesh_api":
        if exp["input_files"]:
            raise ConfigurationError("Mesh 使用服务端会话工作区；当前发布器未上传本地附件，未发送任务")
        return mesh_command(config, dest / "prompt.txt", dest / "mesh-receipt.json")
    if driver in ("workbuddy_desktop", "jahead_desktop"):
        if driver == "jahead_desktop" and exp["input_files"]:
            raise ConfigurationError("即先报告为云端运行；当前 CLI 未实现文件上传，不能假装已读取本地输入。请先使用纯文本任务或人工提交附件")
        return desktop_command(config, dest / "workspace", dest / "prompt.txt", dest / "desktop-receipt.json")
    if driver == "command":
        return adapter_command(config, {"experiment_id": exp["id"], "run_id": f"{exp['id']}:{platform}",
                                       "prompt_file": str(dest / "prompt.txt"), "workspace": str(dest / "workspace"),
                                       "receipt_file": str(dest / "receipt.json")})
    if driver == "workbuddy_openapi":
        if not os.environ.get(config.get("token_env", "WORKBUDDY_ACCESS_TOKEN")):
            raise ConfigurationError("WorkBuddy Open API 凭据环境变量未配置；未发布")
        if exp["input_files"]:
            raise ConfigurationError("WorkBuddy 本地助理 API 不支持指定 cwd/附件；含文件任务请使用能绑定工作目录的 command 发布器")
    return None


def launch(root, exp, platform, background=True):
    dest = run_dir(root, platform)
    state = read(dest / "state.json")
    if (dest / "dispatch.lock").exists() or state["status"] != "prepared":
        return {"platform": platform, "status": state["status"], "message": "已有发布记录；未重复提交"}
    argv = preflight(root, exp, platform)
    # Exclusive marker persists even after a crash or timeout: never blindly replay a write.
    try:
        fd = os.open(dest / "dispatch.lock", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return {"platform": platform, "status": "dispatch_locked", "message": "另一个发布器已取得任务；未重复提交"}
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(now())
    driver = exp["providers"][platform]["driver"]
    state.update(status="awaiting_manual_submission" if driver == "manual" else "dispatching", updated_at=now(), submitted_at=None)
    save(dest / "state.json", state)
    if driver == "manual":
        return {"platform": platform, "status": state["status"], "prompt_file": str(dest / "prompt.txt"), "task_package": str(dest / "submission.zip")}
    if not background:
        worker(root, platform)
        return read(dest / "state.json")
    command = [sys.executable, str(Path(__file__).resolve()), "_worker", "--experiment", str(root), "--platform", platform]
    kwargs = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "cwd": str(root),
              "env": provider_environment(exp, platform)}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen(command, **kwargs)
    except OSError:
        state.update(status="launch_failed", updated_at=now(), error="工作进程未启动；本次发布已锁定，请检查后创建新尝试")
        save(dest / "state.json", state)
        raise
    return {"platform": platform, "status": "dispatching"}


def provider_environment(exp, platform):
    env = os.environ.copy()
    for name, config in exp["providers"].items():
        if name != platform:
            token_env = config.get("token_env", "WORKBUDDY_ACCESS_TOKEN" if name == "workbuddy" else None)
            if token_env:
                env.pop(token_env, None)
    if exp["providers"][platform].get("driver") == "workbuddy_desktop":
        env.pop("WORKBUDDY_ACCESS_TOKEN", None)
    return env


def update_state(dest, state, **fields):
    state.update(fields, updated_at=now())
    save(dest / "state.json", state)


def worker(root, platform):
    root, exp = load_exp(root)
    dest, config = run_dir(root, platform), exp["providers"][platform]
    state = read(dest / "state.json")
    # A second worker invocation must not start a second target process.
    try:
        fd = os.open(dest / "worker.lock", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
    except FileExistsError:
        return
    update_state(dest, state, heartbeat_at=now())
    proc = None
    try:
        argv = preflight(root, exp, platform)
        prompt = (dest / "prompt.txt").read_text(encoding="utf-8")
        if config["driver"] == "workbuddy_openapi":
            receipt = publish_workbuddy(prompt, config)
            save(dest / "receipt.json", receipt)
            update_state(dest, state, status="submitted", submitted_at=now(), receipt=receipt)
            return
        start = time.monotonic()
        update_state(dest, state, status="running", submitted_at=now(), heartbeat_at=now())
        proc = spawn(argv, dest / "workspace", provider_environment(exp, platform))
        update_state(dest, state, child_pid=proc.pid)
        events = queue.Queue()
        def pump(stream, source):
            try:
                for line in stream:
                    events.put((source, line))
            finally:
                stream.close()
                events.put((source, None))
        for name, stream in (("stdout", proc.stdout), ("stderr", proc.stderr)):
            threading.Thread(target=pump, args=(stream, name), daemon=True).start()
        proc.stdin.write(prompt)
        proc.stdin.close()
        closed, complete, failed, cancelled, uncertain, last_save = set(), False, False, False, False, 0.0
        with (dest / "events.jsonl").open("w", encoding="utf-8") as out, (dest / "stderr.log").open("w", encoding="utf-8") as err:
            out.write(json.dumps({"type": "agent_run_compare.prompt", "prompt": prompt, "timestamp": state["submitted_at"]}, ensure_ascii=False) + "\n")
            out.flush()
            while len(closed) < 2 or proc.poll() is None:
                elapsed = time.monotonic() - start
                if (dest / "cancel.request").exists() or elapsed > config.get("timeout_seconds", 900):
                    status = "cancelled" if (dest / "cancel.request").exists() else "timed_out"
                    stop_sent = False
                    if config["driver"] in ("workbuddy_desktop", "jahead_desktop"):
                        receipt = read(dest / "desktop-receipt.json", {})
                        if receipt.get("session_id"):
                            try:
                                stop_sent = cancel_desktop_task(config, receipt["session_id"], dest / "desktop-receipt.json")
                            except (OSError, ValueError, subprocess.TimeoutExpired, ConfigurationError):
                                pass
                    elif config["driver"] == "epsilon_api":
                        try:
                            stop_sent = cancel_epsilon_task(dest / "epsilon-receipt.json")
                        except (OSError, ValueError):
                            pass
                    elif config["driver"] == "mesh_api":
                        try:
                            stop_sent = cancel_mesh_task(dest / "mesh-receipt.json")
                        except (OSError, ValueError):
                            pass
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                    update_state(dest, state, status=status, finished_at=now(), client_elapsed_ms=elapsed*1000,
                                 desktop_stop_requested=stop_sent,
                                 note="已发送桌面任务停止请求并停止桥接子进程；可在桌面确认终态" if stop_sent else "已停止本发布器持有的子进程；不保证独立远端任务已撤销")
                    return
                try:
                    source, line = events.get(timeout=.2)
                except queue.Empty:
                    source, line = None, ""
                if source and line is None:
                    closed.add(source)
                elif source == "stderr":
                    err.write(redact(line)); err.flush()
                elif source == "stdout":
                    try:
                        event = json.loads(line)
                    except ValueError:
                        err.write("[non-JSON stdout] " + redact(line)); err.flush()
                        continue
                    if not isinstance(event, dict):
                        continue
                    # Do not export hidden reasoning payloads into orchestration evidence.
                    if isinstance(event.get("item"), dict) and event["item"].get("type") in ("reasoning", "Reasoning"):
                        continue
                    event["observed_at"] = now()
                    out.write(json.dumps(redact(event), ensure_ascii=False) + "\n"); out.flush()
                    typ = event.get("type")
                    if typ == "thread.started":
                        state["session_id"] = event.get("thread_id")
                    if typ in ("turn.completed", "execution.completed"):
                        complete = True
                    if typ in ("turn.failed", "execution.failed"):
                        failed = True
                        if isinstance(event.get("error"), str):
                            state["error"] = event["error"]
                    if typ == "execution.cancelled":
                        cancelled = True
                    if typ == "submission.receipt":
                        # Only explicitly documented bridge fields; no credentials persisted.
                        receipt = {k: event[k] for k in ("session_id", "log_path", "selector", "external_id", "submitted", "workspace_binding") if k in event}
                        save(dest / "receipt.json", receipt)
                        state["receipt"] = receipt
                        if receipt.get("session_id"):
                            state["session_id"] = receipt["session_id"]
                    if typ == "cli.error" and config["driver"] in ("workbuddy_desktop", "jahead_desktop", "mesh_api", "epsilon_api") and isinstance(event.get("receipt"), dict):
                        uncertain = True
                        receipt = {k: event["receipt"][k] for k in ("session_id", "request_id", "selector", "submitted", "phase") if k in event["receipt"]}
                        save(dest / "receipt.json", receipt)
                        state["receipt"] = receipt
                        state["session_id"] = receipt.get("session_id")
                        state["error"] = event.get("error", "桌面提交结果待确认")
                    if typ == "execution.needs_input":
                        state["status"] = "needs_input"
                    elif state["status"] == "needs_input" and typ == "execution.running":
                        state["status"] = "running"
                    state["last_event"] = typ
                if time.monotonic() - last_save > 1:
                    update_state(dest, state, heartbeat_at=now(), client_elapsed_ms=elapsed*1000)
                    last_save = time.monotonic()
        code = proc.wait()
        if (dest / "receipt.json").exists():
            receipt = read(dest / "receipt.json")
            state["receipt"] = {k: receipt[k] for k in ("session_id", "log_path", "selector", "external_id", "submitted", "workspace_binding") if k in receipt}
            if receipt.get("session_id"):
                state["session_id"] = receipt["session_id"]
        observation_only_timeout = config["driver"] in ("workbuddy_desktop", "jahead_desktop", "mesh_api", "epsilon_api") and state.get("last_event") == "observation.timeout"
        status = "needs_input" if config["driver"] == "epsilon_api" and state["status"] == "needs_input" and code == 0 else "dispatch_unknown" if uncertain else "cancelled" if cancelled else "failed" if failed or (code != 0 and not observation_only_timeout) else "completed" if complete else "submitted" if state.get("receipt", {}).get("submitted") else "exited_unconfirmed"
        update_state(dest, state, status=status, exit_code=code, finished_at=now() if status in TERMINAL else None,
                     heartbeat_at=now(), client_elapsed_ms=(time.monotonic()-start)*1000)
    except DispatchUncertain as exc:
        update_state(dest, state, status="dispatch_unknown", error=str(exc))
    except (ConfigurationError, OSError, ValueError, BrokenPipeError) as exc:
        update_state(dest, state, status="dispatch_unknown" if proc else "dispatch_failed", error=redact(str(exc)))
    finally:
        if proc and proc.poll() is None:
            proc.terminate()


def roots_for(exp, platform):
    config = exp.get("log_roots", {}).get(platform)
    if config:
        return [resolve(p) for p in config]
    if platform == "codex":
        return [resolve(os.environ.get("CODEX_HOME", str(Path.home()/".codex"))) / "sessions"]
    if platform in ("mesh", "jahead", "epsilon"):
        return []
    return [Path.home()/".workbuddy-ai/projects", Path.home()/".workbuddy/projects"]


def match_logs(root, exp, platform, state):
    """Exact session id or unique benchmark marker, never newest-file guessing."""
    candidates = []
    session = state.get("session_id")
    submitted = timestamp(exp.get("created_at"))
    for base in roots_for(exp, platform):
        if not base.exists():
            continue
        for path in base.rglob("*.jsonl"):
            if submitted and path.stat().st_mtime * 1000 < submitted - 3000:
                continue
            if session and session in path.name:
                candidates.append(path)
                continue
            if platform != "workbuddy":
                continue
            # Only inspect user records for correlation; output echoes do not match.
            with path.open("r", encoding="utf-8", errors="replace") as stream:
                for line in stream:
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue
                    if obj.get("role") == "user" and exp["id"] in workbuddy_user_text(obj.get("content")):
                        candidates.append(path)
                        break
    return list(dict.fromkeys(p.resolve() for p in candidates))


def snapshot(source, dest):
    """Freeze complete lines only; tolerate one in-progress final line."""
    source = Path(source)
    hasher, total, pending = hashlib.sha256(), 0, False
    target = Path(dest)
    temp = target.with_name(target.name+"."+uuid.uuid4().hex+".tmp")
    try:
        with source.open("rb") as src, temp.open("wb") as out:
            for raw in src:
                if not raw.strip():
                    continue
                if not raw.endswith(b"\n"):
                    # A valid final JSON object without LF is complete; otherwise wait.
                    try:
                        json.loads(raw)
                    except (ValueError, UnicodeError):
                        pending = True
                        break
                    raw += b"\n"
                try:
                    json.loads(raw)
                except (ValueError, UnicodeError) as exc:
                    raise ValueError("日志完整行中存在非法 JSON；未用错误数据更新指标") from exc
                out.write(raw); hasher.update(raw); total += 1
        temp.replace(target)
        return {"sha256": hasher.hexdigest(), "complete_lines": total, "pending_last_line": pending}
    finally:
        temp.unlink(missing_ok=True)


def find_run(runs, selector, exp, session):
    if selector:
        matches = [r for r in runs if r["selector"] == selector]
    else:
        matches = [r for r in runs if exp["id"] in r["prompt"]]
        if not matches and session:
            matches = [r for r in runs if r["session_id"] == session]
    if len(matches) != 1:
        raise ValueError(f"日志匹配 {len(matches)} 个任务片段，需 bind 精确 selector；未混合其他任务")
    return matches[0]


def observe(root, exp, platform):
    dest = run_dir(root, platform)
    state = read(dest / "state.json")
    result = {**state, "tracking": "waiting", "metrics": None, "final_answer": "", "evaluation_status": "pending"}
    result["interaction_policy"] = exp["providers"][platform].get("interaction_policy", {})
    interactions = []
    if (dest / "events.jsonl").exists():
        with (dest / "events.jsonl").open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("type") == "interaction.resolved":
                    interactions.append(event)
    result["interaction_summary"] = {"recommended_answers": sum(e.get("kind") == "question" for e in interactions),
                                     "approved_permissions": sum(e.get("kind") == "permission" for e in interactions),
                                     "events": interactions}
    if state["status"] == "prepared" and (dest / "preflight-error.json").exists():
        result.update(status="needs_configuration", error=read(dest / "preflight-error.json")["error"])
    if state.get("receipt", {}).get("workspace_binding"):
        result["note"] = state["receipt"]["workspace_binding"]
    binding = read(dest / "binding.json", {})
    if binding:
        result["binding_mode"] = binding.get("matching")
        if not binding.get("prompt_matches"):
            result["note"] = "使用人工绑定记录，未发现本实验任务编号；需要人工核对是否为同一任务"
    desktop_jahead = platform == "jahead" and exp["providers"][platform].get("driver") == "jahead_desktop"
    if platform == "jahead" and not binding and not desktop_jahead:
        result["tracking"] = "manual_import_required"
        result["note"] = "Jahead 无实时日志接入；提交与执行状态不可从本地自动推断"
        return result
    source, selector = binding.get("path"), binding.get("selector")
    if desktop_jahead and not binding and (dest / "events.jsonl").exists():
        source, selector = str(dest / "events.jsonl"), {"session_id": state.get("session_id")}
    if platform in ("mesh", "epsilon") and not binding and (dest / "events.jsonl").exists():
        source, selector = str(dest / "events.jsonl"), state.get("receipt", {}).get("selector")
    if not source:
        previous = read(dest / "observed-binding.json", {})
        is_exec_fallback = previous and Path(previous["path"]) == dest / "events.jsonl" and state.get("session_id")
        if previous and Path(previous["path"]).is_file() and not is_exec_fallback:
            source, selector = previous["path"], previous["selector"]
    if not source and state.get("receipt", {}).get("log_path"):
        # A receipt path must reside in a configured log root or this experiment.
        candidate = resolve(state["receipt"]["log_path"])
        if not any(candidate.is_relative_to(p.resolve()) for p in roots_for(exp, platform) + [root]):
            result["tracking"] = "binding_required"
            result["note"] = "发布回执日志路径不在已配置日志目录，请显式 bind 核验"
            return result
        source = str(candidate)
        selector = state["receipt"].get("selector")
    if not source and state.get("submitted_at"):
        matches = match_logs(root, exp, platform, state)
        if len(matches) == 1:
            source = str(matches[0])
        elif len(matches) > 1:
            result.update(tracking="ambiguous", candidates=[str(p) for p in matches], note="多个日志匹配任务，需显式 bind")
            return result
    if not source and platform == "codex" and (dest / "events.jsonl").exists():
        source = str(dest / "events.jsonl")
    if not source:
        return result
    try:
        st = Path(source).stat()
        signature = {"path": str(resolve(source)), "mtime_ns": st.st_mtime_ns, "bytes": st.st_size, "inode": st.st_ino, "selector": selector, "parser_version": VERSION}
        cached = read(dest / "normalized.json", {})
        if cached.get("_cache_signature") == signature:
            run, info = cached, cached["_snapshot_info"]
        else:
            info = snapshot(source, dest / "snapshot.jsonl")
            runs = parse(dest / "snapshot.jsonl", platform)
            run = find_run(runs, selector, exp, state.get("session_id"))
            run["_cache_signature"], run["_snapshot_info"] = signature, info
            save(dest / "normalized.json", run)
        save(dest / "observed-binding.json", {"path": str(resolve(source)), "selector": run["selector"], "snapshot_sha256": info["sha256"]})
        result.update(tracking="live" if platform != "jahead" or desktop_jahead else "imported", source=str(resolve(source)), selector=run["selector"], snapshot=info,
                      final_answer=run["final_answer"], final_kind=run["final_kind"], native_execution_status=run["execution_status"],
                      metrics={"total_tokens": run["tokens"]["total_tokens"], "tool_calls": run["tool_call_count"],
                               "tool_results": run["tool_results_count"], "duration": run["duration"], "tool_latency": run["tool_latency"], "llm_latency": run["llm_latency"],
                               "first_token": run["first_token"], "llm_first_token": run.get("llm_first_token"), "tool_error_count": run.get("tool_error_count"), "cache_rate": run.get("cache_rate"), "tool_extra_tokens": run.get("tool_extra_tokens")},
                      warnings=run["warnings"])
        if run["execution_status"] == "completed" and result["status"] not in ("failed", "cancelled", "timed_out"):
            result["status"] = "completed"
        elif run["execution_status"] == "aborted" and result["status"] not in TERMINAL:
            result["status"] = "cancelled"
        elif run["execution_status"] == "failed" and result["status"] not in ("cancelled", "timed_out"):
            result["status"] = "failed"
        elif platform == "workbuddy" and run["final_answer"] and result["status"] not in TERMINAL:
            result["status"] = "answer_observed"
            result["note"] = "观察到回答候选；没有明确任务结束事件，不将静默超时当作完成"
        if run.get("execution_error"):
            result["error"] = run["execution_error"]
        if platform == "jahead" and not desktop_jahead:
            result["status"] = "imported"
        if desktop_jahead:
            result["metrics"]["billed_token_count"] = run.get("billed_token_count")
        if platform == "workbuddy":
            db_state = workbuddy_session_status(exp, run["session_id"])
            if db_state:
                result["native_session_status"] = db_state
                mapped = {"completed": "completed", "error": "failed", "failed": "failed", "running": "running", "working": "running",
                          "pending": "needs_input", "waiting_for_approval": "needs_input"}.get(db_state)
                if mapped and result["status"] not in ("cancelled", "timed_out"):
                    result["status"] = mapped
        evaluation = read(dest / "evaluation.json", {})
        if evaluation:
            artifacts_current = all(Path(a["path"]).is_file() and hash_file(Path(a["path"])) == a["sha256"] for a in evaluation.get("artifacts", []))
            if evaluation.get("fingerprint") == run["fingerprint"] and evaluation.get("rubric_hash") == digest(exp["criteria"]) and artifacts_current:
                result["evaluation_status"] = evaluation["verdict"]
                result["evaluation_summary"] = evaluation
            else:
                result["evaluation_status"] = "stale"
    except (OSError, ValueError, KeyError, TypeError) as exc:
        result.update(tracking="parse_pending_or_error", note=str(exc))
    heartbeat = timestamp(state.get("heartbeat_at"))
    if heartbeat and time.time()*1000-heartbeat > 15000 and state["status"] in ("running", "dispatching"):
        result["worker_health"] = "heartbeat_stale"
        result["note"] = "发布进程心跳过期；日志可继续观察，禁止自动重发"
    return result


def workbuddy_session_status(exp, session_id):
    """Optional read-only native status; unknown enum values remain unknown."""
    for base in roots_for(exp, "workbuddy"):
        database = base.parent / "workbuddy.db"
        if not database.is_file():
            continue
        try:
            con = sqlite3.connect(database.resolve().as_uri()+"?mode=ro", uri=True, timeout=.5)
            try:
                row = con.execute("SELECT status FROM sessions WHERE id=?", (session_id,)).fetchone()
                if row:
                    return row[0]
            finally:
                con.close()
        except sqlite3.Error:
            pass
    return None


def status(root, exp):
    data = {"schema_version": 2, "id": exp["id"], "title": exp["title"], "updated_at": now(),
            "published_prompt": exp["published_prompt"], "prompt_sha256": exp["prompt_sha256"],
            "runs": [observe(root, exp, p) for p in exp["providers"]]}
    save(root / "live-status.json", data)
    return data


def bind(root, exp, platform, path, selector):
    source = resolve(path)
    # Explicit user mapping supports imports without the benchmark marker.
    runs = parse(source, platform)
    matches = [r for r in runs if r["selector"] == selector]
    if len(matches) != 1:
        raise ValueError("selector 必须唯一匹配一个日志片段")
    save(run_dir(root, platform) / "binding.json", {"path": str(source), "selector": selector, "bound_at": now(),
                                                  "matching": "explicit_user_binding", "prompt_matches": exp["id"] in matches[0]["prompt"]})
    return status(root, exp)


def export_report(root, exp, out):
    observations = {r["platform"]: r for r in status(root, exp)["runs"]}
    runs, missing = [], []
    frozen = resolve(out)
    if frozen.exists():
        raise ValueError("报告快照目录必须是新目录，避免覆盖已评价证据")
    frozen.mkdir(parents=True)
    for platform in exp["providers"]:
        dest = run_dir(root, platform)
        observed = observations[platform]
        if not observed.get("source") or not observed.get("selector"):
            missing.append(platform)
            continue
        source = frozen / (platform + ".jsonl")
        snapshot(observed["source"], source)
        runs.append({"id": exp["id"]+":"+platform, "label": platform, "platform": platform,
                     "path": str(source), "selector": observed["selector"], "artifacts": [],
                     "interaction_policy": exp["providers"][platform].get("interaction_policy", {}),
                     "interaction_summary": observed.get("interaction_summary", {})})
    if not runs:
        raise ValueError("尚无可规范化日志；未生成空对比")
    manifest = {"schema_version": 1, "title": exp["title"], "tasks": [{"id": exp["id"], "title": exp["title"],
                "prompt": exp["prompt"], "context_id": exp["context_id"], "alignment_confirmed": exp["alignment_confirmed"],
                "alignment_notes": "同文发布；仍需核验不同平台的输入、权限与任务范围。缺少平台："+", ".join(missing),
                "criteria": exp["criteria"], "runs": runs}]}
    save(frozen / "manifest.json", manifest)
    from compare import apply_reviews, load_report, render_report
    report = load_report(frozen / "manifest.json")
    if missing:
        report["tasks"][0]["warnings"].append("尚未导入/绑定平台："+", ".join(missing)+"；不计入已判定完成率")
    apply_reviews(report, [])
    render_report(report, frozen / "report")
    return {"manifest": str(frozen/"manifest.json"), "report": str(frozen/"report/index.html"), "missing_platforms": missing}


def link_evaluation(root, exp, report_path):
    from compare import apply_reviews
    report = read(resolve(report_path))
    reviews = [rv for t in report["tasks"] for r in t["runs"] for rv in r.get("reviews", [])]
    for task in report["tasks"]:
        if task["id"] != exp["id"] or digest(task["criteria"]) != digest(exp["criteria"]):
            raise ValueError("评测报告不是本实验或验收标准已变化")
        for run in task["runs"]:
            run["reviews"] = []
    apply_reviews(report, reviews)
    status(root, exp)
    pending = []
    for task in report["tasks"]:
        for run in task["runs"]:
            platform = run["platform"]
            if platform not in exp["providers"] or run["id"] != exp["id"]+":"+platform:
                raise ValueError("报告运行 ID 不匹配本实验")
            native = read(run_dir(root, platform)/"normalized.json")
            if native["fingerprint"] != run.get("log_fingerprint", run["fingerprint"]):
                raise ValueError("评测后日志证据已变化；请重新冻结并评价，不能更新实时分数")
            artifacts = [{"path": a["path"], "sha256": a["sha256"]} for a in run.get("artifacts", [])]
            if not all(Path(a["path"]).is_file() and hash_file(Path(a["path"])) == a["sha256"] for a in artifacts):
                raise ValueError("评测产物已变化，不能套用旧评价")
            outcome = run["outcome"]
            pending.append((platform, {"fingerprint": native["fingerprint"], "evidence_fingerprint": run["fingerprint"], "artifacts": artifacts, "rubric_hash": digest(exp["criteria"]),
                                      "verdict": outcome["verdict"], "required_passed": outcome["required_passed"],
                                      "required_total": outcome["required_total"], "report": str(resolve(report_path)),
                                      "reviewers": list(dict.fromkeys(rv["reviewer"] for rv in run["reviews"])), "linked_at": now()}))
    for platform, evaluation in pending:
        save(run_dir(root, platform)/"evaluation.json", evaluation)
    return {"linked_platforms": [p for p, _ in pending]}


def serve(root, exp, port):
    lock = threading.Lock()
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.headers.get("Host", "").split(":")[0] not in ("127.0.0.1", "localhost"):
                self.send_error(403); return
            if self.path.split("?")[0] == "/":
                payload = (ROOT/"assets/live.html").read_bytes(); mime = "text/html; charset=utf-8"
            elif self.path.split("?")[0] == "/snapshot":
                with lock:
                    payload = json.dumps(status(root, exp), ensure_ascii=False).encode()
                mime = "application/json; charset=utf-8"
            else:
                self.send_error(404); return
            self.send_response(200); self.send_header("Content-Type", mime)
            self.send_header("Cache-Control", "no-store"); self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(len(payload))); self.end_headers(); self.wfile.write(payload)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"http://127.0.0.1:{server.server_port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare"); prep.add_argument("--task", required=True); prep.add_argument("--out", required=True)
    for name in ("launch", "status", "watch", "serve", "bind", "cancel", "submitted", "export", "link-evaluation", "_worker"):
        p = sub.add_parser(name); p.add_argument("--experiment", required=True)
        if name in ("launch", "bind", "cancel", "submitted", "_worker"):
            p.add_argument("--platform", choices=PLATFORMS, required=name != "launch", action="append" if name == "launch" else "store")
        if name == "launch":
            p.add_argument("--foreground", action="store_true")
        if name == "watch":
            p.add_argument("--seconds", type=float, default=300); p.add_argument("--interval", type=float, default=2)
        if name == "serve":
            p.add_argument("--port", type=int, default=18744)
        if name == "bind":
            p.add_argument("--path", required=True)
            selectors = p.add_mutually_exclusive_group(required=True)
            selectors.add_argument("--selector", help="JSON，例如 {\"record_id\":\"...\"}")
            selectors.add_argument("--selector-file", help="UTF-8 JSON 选择器文件，避免 Windows 命令行引号问题")
        if name == "submitted":
            p.add_argument("--external-id", required=True)
        if name == "export":
            p.add_argument("--out", required=True)
        if name == "link-evaluation":
            p.add_argument("--report", required=True)
    args = ap.parse_args()
    try:
        if args.command == "prepare":
            root, exp = prepare(args.task, args.out)
            result = {"experiment": str(root), "id": exp["id"], "status": "prepared", "platforms": list(exp["providers"])}
        else:
            root, exp = load_exp(args.experiment)
            if isinstance(getattr(args, "platform", None), str) and args.platform not in exp["providers"]:
                raise ValueError("实验没有该平台")
            if args.command == "launch":
                result = []
                for platform in args.platform or exp["providers"]:
                    if platform not in exp["providers"]:
                        raise ValueError("实验没有该平台")
                    try:
                        result.append(launch(root, exp, platform, not args.foreground))
                    except ConfigurationError as exc:
                        save(run_dir(root, platform)/"preflight-error.json", {"error": str(exc), "at": now()})
                        result.append({"platform": platform, "status": "needs_configuration", "error": str(exc)})
            elif args.command == "_worker":
                worker(root, args.platform); return 0
            elif args.command == "status":
                result = status(root, exp)
            elif args.command == "bind":
                selector = read(resolve(args.selector_file)) if args.selector_file else json.loads(args.selector)
                result = bind(root, exp, args.platform, args.path, selector)
            elif args.command == "submitted":
                dest = run_dir(root, args.platform); state = read(dest/"state.json")
                if exp["providers"][args.platform]["driver"] != "manual":
                    raise ValueError("submitted 仅用于人工提交渠道")
                if state["status"] not in ("prepared", "awaiting_manual_submission"):
                    raise ValueError("该任务已经记录提交，不覆盖旧回执")
                update_state(dest, state, status="submitted", submitted_at=now(), external_id=args.external_id)
                result = state
            elif args.command == "cancel":
                dest = run_dir(root, args.platform); state = read(dest/"state.json")
                if state["status"] not in ("running", "needs_input", "dispatching"):
                    raise ValueError("没有本发布器正在持有的执行进程；请在目标平台停止任务")
                (dest/"cancel.request").write_text(now(), encoding="utf-8")
                result = {"status": "cancel_requested", "note": "等待持有子进程的工作进程确认，不按 PID 杀死其他进程"}
            elif args.command == "export":
                result = export_report(root, exp, args.out)
            elif args.command == "link-evaluation":
                result = link_evaluation(root, exp, args.report)
            elif args.command == "serve":
                serve(root, exp, args.port); return 0
            elif args.command == "watch":
                if args.seconds <= 0 or args.interval < .25:
                    raise ValueError("seconds 必须 >0，interval 必须 >=0.25")
                deadline, previous = time.monotonic()+args.seconds, None
                while time.monotonic() < deadline:
                    snapshot_data = status(root, exp)
                    compact = [{k: r.get(k) for k in ("platform", "status", "tracking", "metrics", "worker_health")} for r in snapshot_data["runs"]]
                    key = digest(compact)
                    if key != previous:
                        print(json.dumps(compact, ensure_ascii=False), flush=True); previous = key
                    if all(r["status"] in TERMINAL for r in snapshot_data["runs"]):
                        break
                    time.sleep(min(args.interval, max(0, deadline-time.monotonic())))
                return 0
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except KeyboardInterrupt:
        print("追踪已停止；目标任务没有自动取消。", file=sys.stderr); return 130
    except (OSError, ValueError, KeyError, ConfigurationError) as exc:
        print("ERROR: "+redact(str(exc)), file=sys.stderr); return 2


if __name__ == "__main__":
    raise SystemExit(main())
