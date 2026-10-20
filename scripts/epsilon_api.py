#!/usr/bin/env python3
"""Local Epsilon HTTP/SSE publisher. No replay after an uncertain submission."""
from __future__ import annotations
import argparse
import datetime as dt
import json
import ipaddress
import os
from pathlib import Path
import shutil
import time
import urllib.request
import urllib.parse
import uuid

from common import digest, redact
from compare import save
from mesh_api import NoRedirect
from epsilon_protocol import sanitize_event

DEFAULT_URL = "http://127.0.0.1:30100"
DEFAULT_USER = "local-user"
DEFAULT_COMPANY = "local-company"
DEFAULT_PROJECT = "local-project"


def validate_epsilon_url(value):
    if not isinstance(value, str):
        raise ValueError("Epsilon API 地址必须为文本")
    parsed = urllib.parse.urlsplit(value)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or ".." in urllib.parse.unquote(parsed.path).split("/")):
        raise ValueError("Epsilon 地址必须为无凭据、查询参数和片段的 HTTP(S) 地址")
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        raise ValueError("Epsilon API 端口无效")
    if parsed.hostname != "localhost":
        try:
            address = ipaddress.ip_address(parsed.hostname)
        except ValueError as exc:
            raise ValueError("Epsilon 远端地址请使用明确的内网 IP") from exc
        private_ranges = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7")
        if not address.is_loopback and not any(address in ipaddress.ip_network(n) for n in private_ranges):
            raise ValueError("Epsilon 仅支持本机和内网 IP 地址")
    return value.rstrip("/")


def is_local_url(value):
    host = urllib.parse.urlsplit(value).hostname
    return host == "localhost" or ipaddress.ip_address(host).is_loopback


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def emit(item):
    print(json.dumps({"observed_at": now(), **item}, ensure_ascii=False), flush=True)


class EpsilonClient:
    def __init__(self, config):
        self.config = config
        self.base_url = validate_epsilon_url(config.get("base_url", DEFAULT_URL))
        self.scope = {k: config.get(k, v) for k, v in (("user_id", DEFAULT_USER),
                      ("company_id", DEFAULT_COMPANY), ("project_id", DEFAULT_PROJECT))}
        if any(not isinstance(v, str) or not v.strip() or len(v) > 1024 for v in self.scope.values()):
            raise ValueError("Epsilon 身份范围无效")
        self.scope["agent_id"] = config.get("agent_id") or None
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        self.headers = {"Accept": "application/json"}
        if config.get("token_env"):
            token = os.environ.get(config["token_env"])
            if not token:
                raise ValueError("Epsilon 凭据环境变量未设置")
            self.headers["Authorization"] = "Bearer " + token

    def open(self, method, path, body=None, stream=False):
        headers = {**self.headers, "Accept": "text/event-stream" if stream else "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode()
            headers["Content-Type"] = "application/json"
        return self.opener.open(urllib.request.Request(self.base_url+path, data=data, headers=headers, method=method),
                                timeout=75 if stream else 20)

    def request(self, method, path, body=None):
        with self.open(method, path, body) as response:
            data = response.read(4*1024*1024+1)
        if len(data) > 4*1024*1024:
            raise ValueError("Epsilon 响应超出限制")
        return json.loads(data)

    def doctor(self, model=None):
        if self.request("GET", "/health").get("status") != "ok":
            raise ValueError("Epsilon 服务未就绪")
        models = self.request("GET", "/models")
        if not isinstance(models, list) or not all(isinstance(m, dict) and isinstance(m.get("name"), str) for m in models):
            raise ValueError("Epsilon 模型列表无效")
        if model and model not in {m["name"] for m in models}:
            raise ValueError("Epsilon 模型不在当前服务列表中")
        return {"ok": True, "base_url": self.base_url, "scope": self.scope,
                "models": [{k: m[k] for k in ("name", "label") if k in m} for m in models]}


def sse_frames(response):
    data = []
    size = 0
    while True:
        line = response.readline(4*1024*1024+1)
        if not line:
            raise ValueError("Epsilon SSE 在 [DONE] 前断开；不重发任务")
        size += len(line)
        if size > 4*1024*1024:
            raise ValueError("Epsilon SSE 单帧超过限制")
        text = line.decode("utf-8").rstrip("\r\n")
        if not text:
            if data:
                value = "\n".join(data)
                if value == "[DONE]":
                    yield None
                    return
                yield json.loads(value)
            data, size = [], 0
        elif text.startswith("data:"):
            data.append(text[5:].lstrip(" "))


def cancel(client, receipt):
    if not receipt.get("submitted") or not receipt.get("run_id"):
        raise ValueError("缺少准确的 Epsilon run_id；不猜测中断对象")
    return client.request("POST", "/agent/interrupt",
                          {**client.scope, "session_id": receipt["session_id"],
                           "run_id": receipt["run_id"], "reason": "benchmark_cancel"})


def submit(client, config, prompt, receipt_file, workspace, sink=emit):
    receipt_file = Path(receipt_file).resolve()
    fingerprint = digest({"prompt": prompt, "config": config, "workspace": str(workspace)})
    if receipt_file.exists():
        receipt = json.loads(receipt_file.read_text())
        if receipt.get("fingerprint") != fingerprint:
            raise ValueError("Epsilon 回执属于其他任务，不覆盖或重发")
        sink({**receipt, "reused": True})
        return receipt
    receipt_file.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(receipt_file)+".lock", os.O_CREAT|os.O_EXCL|os.O_WRONLY, 0o600)
    os.close(fd)
    sid = "arc-epsilon-"+uuid.uuid4().hex
    journal = receipt_file.with_suffix(".events.jsonl")
    receipt = {"type": "submission.receipt", "transport": "epsilon_api", "submitted": False,
               "phase": "dispatch_requested", "status": "dispatch_unknown", "created_at": now(),
               "session_id": sid, "selector": {"session_id": sid}, "connection": config,
               "fingerprint": fingerprint, "journal_path": str(journal),
               "workspace_binding": ("Epsilon 本机 API 使用本次独立工作目录。" if is_local_url(client.base_url)
                                     else "Epsilon 使用远端服务配置的工作目录；本机文件不上传。")}
    save(receipt_file, receipt)
    start, monotonic = time.time()*1000, time.monotonic()
    body = {**client.scope, "session_id": sid, "user_query": prompt,
            "create_time": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "options": {"cwd": str(workspace)} if is_local_url(client.base_url) else {}}
    if config.get("model"):
        body["model"] = config["model"]
    if config.get("mode"):
        body["mode"] = config["mode"]
    with journal.open("x", encoding="utf-8") as out:
        written_bytes = 0
        def record(item):
            nonlocal written_bytes
            line = json.dumps(item, ensure_ascii=False)+"\n"
            written_bytes += len(line.encode())
            if written_bytes > 40*1024*1024:
                raise ValueError("Epsilon 公开 SSE 日志超过 40 MB")
            out.write(line);out.flush();sink(item)
        record({"type": "agent_run_compare.prompt", "prompt": prompt})
        record({"type": "epsilon.session", "session_id": sid, "started_ms": start,
                "capture_capabilities": {"tool_usage": True}})
        pending, final, error, suspended, cancelled, seq = set(), False, False, False, False, 0
        error_message = None
        fatal_error = False
        try:
            with client.open("POST", "/agent/response", body, stream=True) as response:
                if "text/event-stream" not in response.headers.get("Content-Type", ""):
                    raise ValueError("Epsilon 未返回 SSE；提交结果需核查")
                receipt.update(submitted=True, phase="streaming", status="running")
                save(receipt_file, receipt);sink(receipt)
                for raw in sse_frames(response):
                    stamp = start+(time.monotonic()-monotonic)*1000
                    if raw is None:
                        status = "cancelled" if cancelled else "needs_input" if suspended else "failed" if error else "completed" if final and not pending else "dispatch_unknown"
                        record({"type": "epsilon.done", "session_id": sid, "time_ms": stamp, "status": status})
                        receipt.update(status=status, phase="stream_ended", finished_at=now())
                        if error_message:
                            receipt["error"] = error_message
                        save(receipt_file, receipt)
                        if status == "dispatch_unknown":
                            sink({"type": "cli.error", "error": "SSE 已结束但缺少最终回答或工具结果，未确认完成", "receipt": receipt})
                        else:
                            sink({"type": "execution."+status, "session_id": sid,
                                  **({"error": error_message} if error_message else {})})
                        return receipt
                    seq += 1
                    event = sanitize_event(raw)
                    if event is None:
                        continue
                    kind, p = event["type"], event["payload"]
                    if kind == "run_started":
                        if receipt.get("run_id") and receipt["run_id"] != p.get("run_id"):
                            raise ValueError("Epsilon SSE 运行身份冲突")
                        if not p.get("run_id"):
                            raise ValueError("Epsilon 缺少原生 run_id")
                        receipt["run_id"] = p["run_id"];save(receipt_file, receipt);sink(receipt)
                    elif kind == "tool_exec_started":
                        pending.add(p.get("tool_use_id"));final = False
                    elif kind == "tool_exec_completed":
                        pending.discard(p.get("tool_use_id"))
                    elif kind == "turn_completed":
                        final = bool(p.get("text")) and not p.get("has_tool_calls")
                        if not fatal_error:
                            error, error_message = False, None
                    elif kind == "error":
                        error = True
                        fatal_error = fatal_error or not p.get("recoverable")
                        error_message = p.get("message", "Epsilon 返回错误")
                    elif kind == "agent_suspended":
                        suspended = True
                    elif kind == "response_cancelled":
                        cancelled = True
                    record({"type": "epsilon.event", "session_id": sid, "seq": seq,
                            "time_ms": stamp, "event": event})
        except Exception as exc:
            receipt.update(status="dispatch_unknown", phase="stream_interrupted", error=redact(str(exc)))
            save(receipt_file, receipt)
            raise


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=("doctor", "models", "submit", "status", "export", "cancel"))
    p.add_argument("--base-url", default=DEFAULT_URL)
    for key, default in (("user-id", DEFAULT_USER), ("company-id", DEFAULT_COMPANY), ("project-id", DEFAULT_PROJECT)):
        p.add_argument("--"+key, default=default)
    for key in ("agent-id", "model", "mode", "token-env", "prompt-file", "receipt-file", "workspace", "output"):
        p.add_argument("--"+key)
    args = p.parse_args();receipt = None
    try:
        config = {k: v for k, v in vars(args).items() if k in ("base_url", "user_id", "company_id", "project_id",
                  "agent_id", "model", "mode", "token_env") and v is not None}
        if args.command in ("status", "export", "cancel"):
            receipt = json.loads(Path(args.receipt_file).read_text());config = receipt["connection"]
        client = EpsilonClient(config)
        if args.command in ("doctor", "models"):
            emit(client.doctor(args.model));return 0
        if args.command == "submit":
            if not args.prompt_file or not args.receipt_file or not args.workspace:
                raise ValueError("需要 prompt-file、receipt-file、workspace")
            prompt = Path(args.prompt_file).read_text()
            if not prompt.strip() or len(prompt.encode()) > 1_000_000:
                raise ValueError("任务正文必须为 1 字节至 1 MB")
            workspace = Path(args.workspace).resolve();workspace.mkdir(parents=True, exist_ok=True)
            client.doctor(args.model)
            receipt = submit(client, config, prompt, args.receipt_file, workspace)
            return 0 if receipt["status"] in ("completed", "needs_input") else 4
        if args.command == "cancel":
            emit({"cancel_result": cancel(client, receipt)});return 0
        if args.command == "export":
            if not args.output:
                raise ValueError("需要 output")
            with Path(receipt["journal_path"]).open("rb") as source, Path(args.output).open("xb") as target:
                shutil.copyfileobj(source, target)
            emit({"exported": str(Path(args.output).resolve())});return 0
        emit({"receipt": receipt, "note": "状态来自本次 SSE 回执；当前服务没有独立运行查询接口，不自动重放请求"})
        return 0
    except Exception as exc:
        if receipt is None and args.receipt_file and Path(args.receipt_file).is_file():
            receipt = json.loads(Path(args.receipt_file).read_text())
        emit({"type": "cli.error", "error": redact(str(exc)), **({"receipt": receipt} if receipt else {})})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
