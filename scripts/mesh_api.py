#!/usr/bin/env python3
"""Mesh HTTP publisher and paginated public session-log collector. Standard library only."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from common import digest, redact
from compare import save
from mesh_protocol import TERMINAL_EVENTS, sanitize_event
from mesh_timing import safe_metrics

DEFAULT_URL = "http://127.0.0.1:18004"
DEFAULT_COMPANY = "local-company"
DEFAULT_PROJECT = "local-project"


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def emit(value):
    print(json.dumps({"observed_at": now(), **value}, ensure_ascii=False), flush=True)


def validate_base_url(value):
    if not isinstance(value, str):
        raise ValueError("Mesh API 地址必须为文本")
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in ("http", "https") or parsed.hostname not in ("127.0.0.1", "localhost", "::1") or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("本机 Mesh 适配器仅连接 localhost / 127.0.0.1 / ::1，地址不能包含账号、查询参数或片段")
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        raise ValueError("Mesh API 端口无效")
    if ".." in urllib.parse.unquote(parsed.path).split("/"):
        raise ValueError("Mesh API 基础路径不能越界")
    return value.rstrip("/")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError("Mesh API 不允许跳转到其他地址")


class MeshClient:
    def __init__(self, base_url=DEFAULT_URL, company_id=DEFAULT_COMPANY, project_id=DEFAULT_PROJECT, agent_id=None, token_env=None):
        self.base_url = validate_base_url(base_url)
        self.scope = {"company_id": company_id, "project_id": project_id}
        if agent_id:
            self.scope["agent_id"] = agent_id
        if any(not isinstance(v, str) or not v.strip() or len(v) > 1024 for v in self.scope.values()):
            raise ValueError("Mesh 公司、项目和 Agent ID 无效")
        self.headers = {"Accept": "application/json"}
        if token_env:
            token = os.environ.get(token_env)
            if not token:
                raise ValueError("Mesh API 凭据环境变量未设置")
            self.headers["Authorization"] = "Bearer " + token
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    @classmethod
    def from_config(cls, config):
        return cls(config.get("base_url", DEFAULT_URL), config.get("company_id", DEFAULT_COMPANY),
                   config.get("project_id", DEFAULT_PROJECT), config.get("agent_id"), config.get("token_env"))

    def request(self, method, path, query=None, body=None):
        url = self.base_url + path + ("?" + urllib.parse.urlencode(query) if query else "")
        headers = dict(self.headers)
        if body is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, headers=headers, method=method,
                                     data=json.dumps(body, ensure_ascii=False).encode() if body is not None else None)
        try:
            with self.opener.open(req, timeout=20) as response:
                data = response.read(16 * 1024 * 1024 + 1)
        except urllib.error.HTTPError as exc:
            raise ValueError(f"Mesh {method} {path}: HTTP {exc.code}；请检查接口、模型或身份范围") from exc
        if len(data) > 16 * 1024 * 1024:
            raise ValueError("Mesh 单页响应超过 16 MB")
        return json.loads(data)

    def doctor(self, model=None):
        readiness = self.request("GET", "/ready")
        if readiness.get("status") != "ok":
            raise ValueError("Mesh API 尚未就绪")
        models = self.request("GET", "/models")
        if not isinstance(models, list) or not all(isinstance(m, dict) and isinstance(m.get("name"), str) for m in models):
            raise ValueError("Mesh 模型列表格式无效")
        if model and model not in {m["name"] for m in models}:
            raise ValueError("请求的 Mesh 模型 ID 不在服务返回的模型列表中")
        self.request("GET", "/sessions", {**self.scope, "limit": 1})
        return {"ok": True, "base_url": self.base_url, "models": [{k: m[k] for k in ("name", "label") if k in m} for m in models],
                "isolation": readiness.get("isolation"), "scope": self.scope}

    def events(self, session_id, after=-1, generation=0):
        path = "/sessions/" + urllib.parse.quote(session_id, safe="") + "/events"
        page = self.request("GET", path, {**self.scope, "generation": generation, "after": after, "limit": 100})
        if not isinstance(page, dict) or not isinstance(page.get("events"), list) or type(page.get("next_after")) is not int:
            raise ValueError("Mesh 会话事件分页格式无效")
        previous = after
        for event in page["events"]:
            if not isinstance(event, dict) or type(event.get("seq")) is not int or event["seq"] <= previous or event.get("session_generation") != generation:
                raise ValueError("Mesh 事件游标倒退、重复或会话代次不匹配")
            previous = event["seq"]
        if page["next_after"] != previous:
            raise ValueError("Mesh next_after 与最后事件序号不一致")
        return page


def submit(client, config, prompt, receipt_file):
    receipt_file = Path(receipt_file).resolve()
    fingerprint = digest({"prompt": prompt, "connection": config})
    if receipt_file.exists():
        receipt = json.loads(receipt_file.read_text())
        if receipt.get("fingerprint") != fingerprint:
            raise ValueError("Mesh 回执属于另一个任务；不覆盖或重复发布")
        return {**receipt, "reused": True}
    receipt_file.parent.mkdir(parents=True, exist_ok=True)
    lock = receipt_file.with_suffix(receipt_file.suffix + ".lock")
    fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    session_id, request_id = "arc-" + uuid.uuid4().hex, "arc-request-" + uuid.uuid4().hex
    body = {**client.scope, "session_id": session_id, "request_id": request_id, "user_query": prompt, "create_time": now()}
    if config.get("model"):
        body["model"] = config["model"]
    receipt = {"type": "submission.receipt", "transport": "mesh_api", "submitted": False,
               "session_id": session_id, "request_id": request_id, "generation": 0, "created_at": now(),
               "fingerprint": fingerprint, "connection": config, "phase": "preparing",
               "workspace_binding": "Mesh 由 API 运行时分配会话工作区，本地文件不会自动上传。"}
    save(receipt_file, receipt)
    try:
        # Change only this new session's file-access mode, never a global Agent setting.
        permission_path = "/sessions/" + session_id + "/permissions"
        permission = client.request("GET", permission_path, client.scope)
        mode = permission.get("mode")
        if mode != "workspace-write":
            revision = permission.get("revision")
            if type(revision) is not int:
                raise ValueError("Mesh 权限接口缺少 revision；未发送任务")
            permission = client.request("PUT", permission_path, client.scope, {"mode": "workspace-write", "revision": revision})
            if permission.get("mode") != "workspace-write":
                raise ValueError("Mesh 未确认新会话的 workspace-write 权限")
        receipt["phase"] = "dispatch_requested"
        save(receipt_file, receipt)
        result = client.request("POST", "/agent/runs", body=body)
        if not isinstance(result, dict) or result.get("session_id") != session_id or result.get("request_id") != request_id or not result.get("run_id") or any(result.get(k) != v for k, v in client.scope.items()):
            raise ValueError("Mesh 返回的运行回执与请求身份不一致")
        receipt.update(submitted=True, phase="submitted", submitted_at=now(), run_id=result["run_id"],
                       user_id=result.get("user_id"), agent_id=result.get("agent_id"), selector={"run_id": result["run_id"]})
        save(receipt_file, receipt)
        return receipt
    except Exception as exc:
        receipt.update(phase="dispatch_unknown" if receipt["phase"] == "dispatch_requested" else "prepare_failed", error=redact(str(exc)))
        save(receipt_file, receipt)
        raise


def cancel(client, receipt, receipt_file):
    if not receipt.get("run_id") or not receipt.get("submitted"):
        raise ValueError("没有已确认的 Mesh run_id，不猜测取消对象")
    if not receipt.get("cancel_request_id"):
        receipt["cancel_request_id"] = "arc-cancel-" + uuid.uuid4().hex
        save(receipt_file, receipt)
    return client.request("POST", "/agent/runs/" + urllib.parse.quote(receipt["run_id"], safe="") + "/cancel",
                          body={**client.scope, "session_id": receipt["session_id"], "request_id": receipt["cancel_request_id"], "reason": "benchmark_cancel"})


def recover_receipt(client, receipt, receipt_file):
    """A new random session belongs to one submitted request; recovery only reads events."""
    if receipt.get("run_id"):
        return receipt
    cursor, run_ids = -1, set()
    for _ in range(1000):
        page = client.events(receipt["session_id"], cursor, receipt.get("generation", 0))
        for event in page["events"]:
            if event.get("type") == "mesh/run-started" and isinstance(event.get("data", {}).get("run_id"), str):
                run_ids.add(event["data"]["run_id"])
        if not page["events"]:
            break
        cursor = page["next_after"]
    else:
        raise ValueError("Mesh 回执恢复超过分页上限")
    if len(run_ids) > 1:
        raise ValueError("Mesh 会话出现多个运行，不能猜测原请求归属")
    if run_ids:
        run_id = next(iter(run_ids))
        receipt.update(run_id=run_id, submitted=True, phase="recovered_from_session_log", selector={"run_id": run_id})
        save(receipt_file, receipt)
    return receipt


def collect(client, receipt, timeout=0, sink=emit):
    sink({"type": "mesh.session", **{k: receipt[k] for k in ("session_id", "run_id", "generation", "created_at", "connection") if k in receipt}})
    cursor, started, state, total_bytes, final = -1, time.monotonic(), "running", 0, ""
    active_run = None
    while True:
        page = client.events(receipt["session_id"], cursor, receipt.get("generation", 0))
        for raw in page["events"]:
            event = sanitize_event(raw)
            cursor = raw["seq"]
            if event is None:
                continue
            total_bytes += len(json.dumps(event, ensure_ascii=False))
            if total_bytes > 40 * 1024 * 1024:
                raise ValueError("Mesh 公开会话日志超过 40 MB")
            sink({"type": "mesh.event", "session_id": receipt["session_id"], "event": event})
            kind, data = event["type"], event["data"]
            if kind == "mesh/run-started":
                active_run = data.get("run_id")
            if active_run != receipt.get("run_id"):
                continue
            if kind in TERMINAL_EVENTS and data.get("run_id") == receipt.get("run_id"):
                state = TERMINAL_EVENTS[kind]
            elif kind == "mesh/interaction-requested" and state not in TERMINAL_EVENTS.values():
                state = "needs_input"
            elif kind == "mesh/interaction-resolved" and state not in TERMINAL_EVENTS.values():
                state = "running"
            elif kind == "assistant/message" and data.get("text") and not data.get("has_tool_calls"):
                final = data["text"]
        if page["events"]:
            continue  # A short page is not end-of-log; drain to an empty page.
        result = {"status": state, "session_id": receipt["session_id"], "run_id": receipt.get("run_id"),
                  "after_seq": cursor, "final_answer": final if state == "completed" else ""}
        if state in TERMINAL_EVENTS.values():
            try:
                metrics = client.request("GET", "/sessions/" + receipt["session_id"] + "/token-metrics", client.scope)
                sink({"type": "mesh.metrics", "session_id": receipt["session_id"],
                      "run_id": receipt.get("run_id"), "generation": receipt.get("generation", 0),
                      "after_seq": cursor, "metrics": safe_metrics(metrics)})
            except (OSError, ValueError):
                pass  # Canonical reported usage remains the source; estimates are never substituted.
            sink({"type": "execution." + state, **result})
            return result
        sink({"type": "execution.needs_input" if state == "needs_input" else "execution.running", **result})
        if timeout <= 0:
            return result
        if time.monotonic() - started >= timeout:
            sink({"type": "observation.timeout", **result, "note": "停止观察；Mesh 原任务未自动重发或取消"})
            return result
        time.sleep(1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("doctor", "models", "submit", "status", "wait", "export", "cancel"))
    parser.add_argument("--base-url", default=DEFAULT_URL)
    parser.add_argument("--company-id", default=DEFAULT_COMPANY)
    parser.add_argument("--project-id", default=DEFAULT_PROJECT)
    parser.add_argument("--agent-id")
    parser.add_argument("--token-env")
    parser.add_argument("--model")
    parser.add_argument("--prompt-file")
    parser.add_argument("--receipt-file")
    parser.add_argument("--output")
    parser.add_argument("--wait", action="store_true")
    parser.add_argument("--timeout", type=float, default=1800)
    args = parser.parse_args()
    config = {k: v for k, v in vars(args).items() if k in ("base_url", "company_id", "project_id", "agent_id", "token_env", "model") and v is not None}
    receipt = None
    try:
        if args.timeout <= 0:
            raise ValueError("timeout 必须为正数")
        if args.command in ("status", "wait", "export", "cancel"):
            if not args.receipt_file:
                raise ValueError("需要 --receipt-file 定位准确的 Mesh 会话和运行")
            receipt = json.loads(Path(args.receipt_file).read_text())
            config = receipt["connection"]
        client = MeshClient.from_config(config)
        if args.command in ("doctor", "models"):
            emit(client.doctor(args.model)); return 0
        if receipt is not None and not receipt.get("run_id"):
            receipt = recover_receipt(client, receipt, args.receipt_file)
            if not receipt.get("run_id"):
                emit({"type": "execution.status", "status": "awaiting_request", "session_id": receipt["session_id"], "note": "尚未查到运行记录；没有重发任务"}); return 3
        if args.command == "submit":
            if not args.receipt_file or not args.prompt_file:
                raise ValueError("需要 --prompt-file 和 --receipt-file")
            prompt = Path(args.prompt_file).read_text(encoding="utf-8")
            if not prompt.strip() or len(prompt.encode()) > 1_000_000:
                raise ValueError("任务正文必须为 1 字节至 1 MB")
            client.doctor(args.model)
            receipt = submit(client, config, prompt, args.receipt_file)
            emit(receipt)
            if not receipt.get("submitted") or not args.wait:
                return 0 if receipt.get("submitted") else 3
        elif args.command == "cancel":
            cancel(client, receipt, args.receipt_file); emit({"type": "cancel.requested", "session_id": receipt["session_id"]}); return 0
        if args.command == "export":
            if not args.output:
                raise ValueError("需要 --output")
            with Path(args.output).open("x", encoding="utf-8") as stream:
                collect(client, receipt, sink=lambda item: stream.write(json.dumps({"observed_at": now(), **item}, ensure_ascii=False) + "\n"))
            emit({"exported": str(Path(args.output).resolve())}); return 0
        result = collect(client, receipt, args.timeout if args.command in ("submit", "wait") else 0)
        return 0 if result["status"] == "completed" else 4 if result["status"] in ("failed", "cancelled") else 3
    except Exception as exc:
        if receipt is None and args.receipt_file and Path(args.receipt_file).exists():
            receipt = json.loads(Path(args.receipt_file).read_text())
        emit({"type": "cli.error", "error": redact(str(exc)), **({"receipt": receipt} if receipt else {})})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
