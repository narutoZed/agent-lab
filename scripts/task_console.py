#!/usr/bin/env python3
"""Local XLSX task picker and persistent, per-platform publication queue."""
from __future__ import annotations

import argparse
import base64
import copy
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
from pathlib import Path
import secrets
import threading
import time
from urllib.parse import urlsplit
import uuid
import zipfile

from common import digest
from dispatch import ConfigurationError
from orchestrate import PLATFORMS, ROOT, launch, load_exp, now, observe, prepare, read, run_dir, save
from xlsx_tasks import MAX_UPLOAD, read_xlsx
from console_comparison import build_comparison
from mesh_api import DEFAULT_URL, DEFAULT_COMPANY, DEFAULT_PROJECT, MeshClient, validate_base_url
from epsilon_api import EpsilonClient, validate_epsilon_url, DEFAULT_URL as EPSILON_URL, DEFAULT_USER as EPSILON_USER

DONE = {"completed", "failed", "cancelled", "needs_configuration", "dispatch_failed", "launch_failed"}
DRIVERS = {"codex": "codex_cli", "workbuddy": "workbuddy_desktop", "jahead": "jahead_desktop", "mesh": "mesh_api", "epsilon": "epsilon_api"}
PARALLELISM = {p: 10 if p in ("mesh", "epsilon") else 1 for p in PLATFORMS}
QUEUE_BLOCKERS = {"needs_configuration", "dispatch_unknown", "needs_confirmation", "needs_input",
                  "awaiting_manual_submission", "answer_observed", "exited_unconfirmed", "timed_out"}


def clean_text(value, name, maximum, required=False):
    if not isinstance(value, str) or len(value) > maximum or "\x00" in value:
        raise ValueError(f"{name} 必须为不超过 {maximum} 字符的文本")
    value = value.strip()
    if required and not value:
        raise ValueError(f"{name} 不能为空")
    return value


def validate_request(payload):
    if not isinstance(payload, dict):
        raise ValueError("请求必须为 JSON 对象")
    try:
        request_id = str(uuid.UUID(payload.get("request_id", "")))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("发布请求缺少有效的防重复 ID") from exc
    title = clean_text(payload.get("title", "批量任务"), "批次名称", 120, True)
    raw_tasks = payload.get("tasks")
    if not isinstance(raw_tasks, list) or not 1 <= len(raw_tasks) <= 100:
        raise ValueError("每批请选择 1–100 条任务")
    tasks, task_ids = [], set()
    for index, task in enumerate(raw_tasks, 1):
        if not isinstance(task, dict):
            raise ValueError("任务格式无效")
        task_id = clean_text(task.get("id", str(index)), "任务编号", 120, True)
        if task_id in task_ids:
            raise ValueError(f"任务编号重复：{task_id}")
        task_ids.add(task_id)
        prompt = clean_text(task.get("prompt"), f"第 {index} 条任务正文", 100000, True)
        task_title = clean_text(task.get("title", ""), "任务名称", 200) or prompt[:60]
        criteria = clean_text(task.get("criteria", ""), "验收要求", 10000)
        if len([s for s in criteria.splitlines() if s.strip()]) > 50:
            raise ValueError("单条任务最多支持 50 项验收要求")
        source = clean_text(task.get("source", "网页输入"), "来源", 240)
        tasks.append({"id": task_id, "title": task_title, "prompt": prompt, "criteria": criteria, "source": source})
    if sum(len(t["prompt"]) for t in tasks) > 2_000_000:
        raise ValueError("每批任务正文合计不能超过 200 万字符")
    raw_providers = payload.get("providers")
    if not isinstance(raw_providers, dict) or not raw_providers or set(raw_providers) - set(PLATFORMS):
        raise ValueError("请选择 Codex、WorkBuddy、即先、Mesh 或 Epsilon 中的至少一个平台")
    providers = {}
    for platform, config in raw_providers.items():
        allowed = {"model", "mode", "module", "timeout_seconds"} | ({"base_url", "company_id", "project_id", "agent_id"} if platform in ("mesh", "epsilon") else set()) | ({"user_id"} if platform == "epsilon" else set())
        if not isinstance(config, dict) or set(config) - allowed:
            raise ValueError("网页只接受模型、模式、模块和超时配置")
        timeout = config.get("timeout_seconds", 1800)
        if type(timeout) is not int or not 60 <= timeout <= 7200:
            raise ValueError("单任务超时应为 60–7200 秒")
        normalized = {"driver": DRIVERS[platform], "timeout_seconds": timeout}
        if platform in ("codex", "workbuddy"):
            if config.get("module") or (platform == "codex" and config.get("mode")):
                raise ValueError("该平台不支持所选模块或模式")
            model = clean_text(config.get("model", ""), "模型 ID", 160)
            if model:
                normalized["model"] = model
            if platform == "workbuddy":
                mode = clean_text(config.get("mode", ""), "WorkBuddy 模式", 80)
                if mode:
                    normalized["mode"] = mode
        elif platform == "epsilon":
            if config.get("module") or config.get("mode") not in (None, "", "fast", "expert"):
                raise ValueError("Epsilon 模式仅支持 fast/expert，模块由服务配置")
            normalized["base_url"] = validate_epsilon_url(config.get("base_url", EPSILON_URL))
            for key, default in (("user_id", EPSILON_USER), ("company_id", DEFAULT_COMPANY),
                                 ("project_id", DEFAULT_PROJECT), ("agent_id", ""), ("model", ""), ("mode", "")):
                value = clean_text(config.get(key, default), "Epsilon "+key, 1024, key in ("user_id", "company_id", "project_id"))
                if value:
                    normalized[key] = value
        elif platform == "mesh":
            if config.get("module") or config.get("mode"):
                raise ValueError("Mesh 网页入口使用服务默认运行模式，不接受其他平台的模块配置")
            normalized["base_url"] = validate_base_url(config.get("base_url", DEFAULT_URL))
            for key, default in (("company_id", DEFAULT_COMPANY), ("project_id", DEFAULT_PROJECT), ("agent_id", ""), ("model", "")):
                value = clean_text(config.get(key, default), "Mesh " + key, 1024, key in ("company_id", "project_id"))
                if value:
                    normalized[key] = value
        else:
            if config.get("model"):
                raise ValueError("即先入口当前不支持指定模型 ID")
            module = config.get("module", "report")
            if module not in ("report", "create", "search", "developer"):
                raise ValueError("即先模块无效")
            normalized["module"] = module
            if module == "report":
                mode = config.get("mode", "expert")
                if mode not in ("expert", "fast"):
                    raise ValueError("报告模式应为 expert 或 fast")
                normalized.update(mode=mode, **{"web-search": "true"})
            elif config.get("mode"):
                raise ValueError("报告以外的即先模块不接受报告模式")
            if module == "developer":
                normalized["stage"] = "requirement"
        providers[platform] = normalized
    policy = payload.get("interaction_policy", {"questions": "recommended", "permissions": "approve"})
    if not isinstance(policy, dict) or set(policy) != {"questions", "permissions"} or policy["questions"] not in ("recommended", "manual") or policy["permissions"] not in ("approve", "manual"):
        raise ValueError("交互策略无效")
    return {"request_id": request_id, "title": title, "tasks": tasks,
            "providers": providers, "interaction_policy": policy}


class ConsoleStore:
    def __init__(self, root, launcher=None, observer=None):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.comparison_lock = threading.Lock()
        self.tick_locks = {p: threading.Lock() for p in PLATFORMS}
        self.stop = threading.Event()
        self.launcher = launcher or launch
        self.observer = observer or observe
        self.batches = {}
        for file in sorted(self.root.glob("*/batch.json"), key=lambda p: read(p).get("created_at", "")):
            batch = read(file)
            if str(uuid.UUID(batch["id"])) != file.parent.name:
                raise ValueError("批次目录与 ID 不一致")
            # Freeze legacy batches at their original single-job policy.
            batch.setdefault("parallelism", {p: 1 for p in batch["providers"]})
            # Work already sent is observed, but a restart never silently sends queued work.
            if any(j["status"] == "queued" for j in batch["jobs"]):
                batch["paused"] = True
                batch["queue_note"] = "控制台重新启动，未发布任务已暂停；确认后可继续队列。"
            elif all(j["status"] == "completed" for j in batch["jobs"]):
                batch["queue_note"] = "本批次已全部执行结束，可查看横向指标对比。"
            self.batches[batch["id"]] = batch
            self._save(batch)

    def _save(self, batch):
        batch["updated_at"] = now()
        save(self.root / batch["id"] / "batch.json", batch)

    def create(self, payload):
        spec = validate_request(payload)
        fingerprint = digest(spec)
        batch_id = spec["request_id"]
        with self.lock:
            existing = self.batches.get(batch_id)
            if existing:
                if existing["fingerprint"] != fingerprint:
                    raise ValueError("该发布 ID 已绑定另一组任务；未重复提交")
                return {"id": batch_id, "reused": True}
            folder = self.root / batch_id
            if folder.exists():
                raise ValueError("该批次的准备过程曾中断；未自动发布，请检查本地记录")
            folder.mkdir()
            jobs = []
            for index, task in enumerate(spec["tasks"]):
                descriptions = [s.strip() for s in task["criteria"].splitlines() if s.strip()]
                if len(descriptions) > 50:
                    raise ValueError("单条任务最多支持 50 项验收要求")
                criteria = [{"id": f"criterion-{i + 1}", "description": s, "required": True, "verification": "answer"}
                            for i, s in enumerate(descriptions or ["按任务正文中的要求完成任务；具体效果另行核验。"])]
                task_file = folder / f"task-{index + 1}.json"
                save(task_file, {"title": task["title"], "prompt": task["prompt"], "criteria": criteria,
                                 "context_id": "网页批量发布：同一任务正文，无附件；平台与模型配置独立。",
                                 "providers": copy.deepcopy(spec["providers"]), "interaction_policy": spec["interaction_policy"]})
                experiment = folder / f"experiment-{index + 1}"
                prepare(task_file, experiment)
                for platform in spec["providers"]:
                    jobs.append({"task_index": index, "platform": platform, "status": "queued", "snapshot": {}})
            batch = {"schema_version": 1, "id": batch_id, "fingerprint": fingerprint, "created_at": now(),
                     "title": spec["title"], "tasks": spec["tasks"], "providers": spec["providers"],
                     "interaction_policy": spec["interaction_policy"], "jobs": jobs, "paused": False}
            batch["parallelism"] = {p: PARALLELISM[p] for p in spec["providers"]}
            self._save(batch)
            self.batches[batch_id] = batch
            return {"id": batch_id, "reused": False}

    def set_paused(self, batch_id, paused):
        with self.lock:
            batch = self.batches[batch_id]
            batch["paused"] = paused
            batch["queue_note"] = "后续任务已暂停，已发出的任务继续执行。" if paused else "队列已继续。"
            self._save(batch)
        return {"id": batch_id, "paused": paused}

    def retry_unsubmitted(self, batch_id):
        """Retry only preflight failures with positive evidence no publisher ever started."""
        with self.lock:
            batch = self.batches[batch_id]
            retried, blocked = [], []
            for job in batch["jobs"]:
                if job["status"] != "needs_configuration":
                    continue
                root, exp = self._experiment(batch, job)
                dest = run_dir(root, job["platform"])
                state = read(dest / "state.json")
                markers = ("dispatch.lock", "worker.lock", "desktop-receipt.json", "mesh-receipt.json", "epsilon-receipt.json", "receipt.json", "events.jsonl")
                if state.get("status") != "prepared" or any((dest / name).exists() for name in markers):
                    blocked.append({"task_index": job["task_index"], "platform": job["platform"]})
                    continue
                job.setdefault("recovery_history", []).append({"at": now(), "previous_error": job.get("error"),
                                                              "reason": "仅重试确认未发送的连接检查失败任务"})
                job.update(status="queued", snapshot={})
                for key in ("error", "observation_error", "finished_at", "observed_at"):
                    job.pop(key, None)
                retried.append({"task_index": job["task_index"], "platform": job["platform"]})
            self._save(batch)
            return {"id": batch_id, "requeued": len(retried), "jobs": retried, "blocked": blocked}

    def _experiment(self, batch, job):
        return load_exp(self.root / batch["id"] / f"experiment-{job['task_index'] + 1}")

    def tick(self, platform):
        # One scheduler per platform; also protects explicit/concurrent tick calls.
        with self.tick_locks[platform]:
            self._tick_platform(platform)

    @staticmethod
    def _active(job):
        return (job["status"] not in DONE | {"queued"}
                or job["status"] == "completed" and not job.get("observed_at")
                or job["status"] == "needs_configuration")

    @staticmethod
    def _limit(batch, platform):
        value = (batch.get("parallelism") or {}).get(platform, 1)
        return min(value, PARALLELISM[platform]) if type(value) is int and value >= 1 else 1

    def _tick_platform(self, platform):
        with self.lock:
            active = [(b, j) for b in self.batches.values() for j in b["jobs"]
                      if j["platform"] == platform and self._active(j)]
        # Observe every running slot, even when a different slot needs attention.
        for batch, job in active:
            if job["status"] != "needs_configuration":
                self._advance_job(batch, job, dispatch=False)
        for _ in range(PARALLELISM[platform]):
            with self.lock:
                active = [(b, j) for b in self.batches.values() for j in b["jobs"]
                          if j["platform"] == platform and self._active(j)]
                if any(j["status"] in QUEUE_BLOCKERS or j.get("observation_error") for _, j in active):
                    return
                queued = next(((b, j) for b in self.batches.values() if not b["paused"] for j in b["jobs"]
                               if j["platform"] == platform and j["status"] == "queued"), None)
                if queued is None:
                    return
                batch, job = queued
                # Limits apply across batches, not ten slots for every batch.
                # A still-active legacy batch retains its previous serial policy.
                capacity = min([self._limit(batch, platform)] +
                               [self._limit(b, platform) for b, _ in active])
                if len(active) >= capacity:
                    return
                job.update(status="dispatching", started_at=now())
                self._save(batch)
            self._advance_job(batch, job, dispatch=True)

    def _advance_job(self, batch, job, dispatch):
        platform = job["platform"]
        try:
            root, exp = self._experiment(batch, job)
        except Exception as exc:
            with self.lock:
                if dispatch:
                    job.update(status="dispatch_unknown", error=str(exc))
                else:
                    job["observation_error"] = str(exc)
                self._save(batch)
            return
        if dispatch:
            try:
                outcome = self.launcher(root, exp, platform)
                with self.lock:
                    job["status"] = outcome["status"]
                    if job["status"] == "prepared":
                        job["status"] = "dispatch_unknown"
                    self._save(batch)
            except ConfigurationError as exc:
                with self.lock:
                    job.update(status="needs_configuration", error=str(exc))
                    self._save(batch)
            except Exception as exc:
                with self.lock:
                    job.update(status="dispatch_unknown", error=str(exc))
                    self._save(batch)
            return
        try:
            result = self.observer(root, exp, platform)
            status = result.get("status", "dispatch_unknown")
            if status in ("prepared", "dispatch_locked", "timed_out", "exited_unconfirmed"):
                status = "needs_confirmation"
            if status == "cancelled" and result.get("native_execution_status") != "aborted" and result.get("last_event") != "execution.cancelled":
                status = "needs_confirmation"
            if result.get("worker_health") == "heartbeat_stale" and status not in DONE:
                status = "needs_confirmation"
            normalized = read(run_dir(root, platform) / "normalized.json", {})
            snapshot = {k: result[k] for k in ("metrics", "final_answer", "note", "error", "tracking", "worker_health", "evaluation_status", "session_id") if k in result}
            snapshot["models"] = normalized.get("models", [])
            requested = exp["providers"][platform].get("model")
            if requested and snapshot["models"] and requested not in snapshot["models"]:
                snapshot["model_note"] = "日志中的模型与请求 ID 不同；可能存在别名或回退，请核验。"
            with self.lock:
                job.pop("observation_error", None)
                job.update(status=status, snapshot=snapshot, observed_at=now())
                if status in DONE:
                    job["finished_at"] = now()
                self._save(batch)
        except Exception as exc:
            with self.lock:
                job["observation_error"] = str(exc)
                self._save(batch)

    def _loop(self, platform):
        while not self.stop.is_set():
            try:
                self.tick(platform)
            except Exception as exc:
                # A single broken record must not silently kill the scheduler thread.
                print(f"{platform} queue: {exc}", flush=True)
            self.stop.wait(3)

    def start(self):
        for platform in PLATFORMS:
            threading.Thread(target=self._loop, args=(platform,), daemon=True).start()

    def listing(self):
        with self.lock:
            result = []
            for batch in reversed(list(self.batches.values())):
                item = {k: batch[k] for k in ("id", "title", "created_at", "updated_at", "paused", "providers")}
                item["parallelism"] = {p: self._limit(batch, p) for p in batch["providers"]}
                item["queue_note"] = batch.get("queue_note", "")
                item["tasks"] = [{k: task[k] for k in ("id", "title", "source")} for task in batch["tasks"]]
                item["jobs"] = [{**job, "snapshot": {k: v for k, v in job.get("snapshot", {}).items() if k != "final_answer"}} for job in batch["jobs"]]
                result.append(copy.deepcopy(item))
            return {"batches": result}

    def detail(self, batch_id, index):
        with self.lock:
            batch = self.batches[batch_id]
            if not 0 <= index < len(batch["tasks"]):
                raise KeyError(index)
            return copy.deepcopy({"task": batch["tasks"][index], "providers": batch["providers"],
                                  "experiment": str(self.root / batch_id / f"experiment-{index + 1}"),
                                  "jobs": [j for j in batch["jobs"] if j["task_index"] == index]})

    def comparison(self, batch_id):
        with self.comparison_lock:
            with self.lock:
                batch = copy.deepcopy(self.batches[batch_id])
            result = build_comparison(self.root, batch)
            report = read(Path(result["report_path"]).with_name("results.json"))
            with self.lock:
                current = self.batches[batch_id]
                current["comparison"] = result
                for index, task in enumerate(report["tasks"]):
                    for run in task["runs"]:
                        job = next(j for j in current["jobs"] if j["task_index"] == index and j["platform"] == run["platform"])
                        snapshot = job.setdefault("snapshot", {})
                        snapshot["metrics"] = {"total_tokens": run["tokens"]["total_tokens"], "tool_calls": run["tool_call_count"],
                                               "tool_results": run["tool_results_count"], "duration": run["duration"],
                                               "tool_latency": run["tool_latency"], "llm_latency": run["llm_latency"],
                                               "first_token": run["first_token"], "llm_first_token": run.get("llm_first_token"), "tool_error_count": run.get("tool_error_count"), "cache_rate": run.get("cache_rate"), "tool_extra_tokens": run.get("tool_extra_tokens")}
                        if run.get("billed_token_count"):
                            snapshot["metrics"]["billed_token_count"] = run["billed_token_count"]
                        snapshot.update(models=run["models"], final_answer=run["final_answer"])
                        if run.get("execution_error"):
                            snapshot["error"] = run["execution_error"]
                self._save(current)
            return result

    def comparison_file(self, batch_id, revision, filename):
        if batch_id not in self.batches or not re.fullmatch(r"[0-9a-f]{32}", revision) or filename not in ("index.html", "results.json"):
            raise KeyError("未知对比报告")
        return self.root / batch_id / "comparisons" / revision / "report" / filename


def make_handler(store, csrf_token):
    class Handler(BaseHTTPRequestHandler):
        def _host_ok(self):
            port = self.server.server_port
            return self.headers.get("Host") in (f"127.0.0.1:{port}", f"localhost:{port}")

        def _send(self, status, value, mime="application/json; charset=utf-8", report=False):
            payload = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "SAMEORIGIN" if report else "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            if report:
                scripts = re.findall(rb"<script[^>]*>(.*?)</script>", payload, re.S)
                hashes = " ".join("'sha256-" + base64.b64encode(hashlib.sha256(s).digest()).decode() + "'" for s in scripts)
                policy = f"default-src 'none'; script-src {hashes}; style-src 'unsafe-inline'; img-src data:; connect-src 'none'; frame-ancestors 'self'; base-uri 'none'; form-action 'none'"
            else:
                policy = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
            self.send_header("Content-Security-Policy", policy)
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            if not self._host_ok():
                self._send(403, {"error": "仅允许本机访问"}); return
            path = urlsplit(self.path).path
            try:
                if path == "/":
                    html = (ROOT / "assets/console.html").read_text(encoding="utf-8").replace("__CSRF_TOKEN__", csrf_token)
                    self._send(200, html.encode(), "text/html; charset=utf-8")
                elif path in ("/console.js", "/console.css"):
                    mime = "text/javascript; charset=utf-8" if path.endswith(".js") else "text/css; charset=utf-8"
                    self._send(200, (ROOT / "assets" / path[1:]).read_bytes(), mime)
                elif path == "/template.xlsx":
                    self._send(200, (ROOT / "assets/task-list-template.xlsx").read_bytes(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
                elif path == "/api/batches":
                    self._send(200, store.listing())
                elif path.startswith("/api/tasks/"):
                    parts = path.split("/")
                    if len(parts) != 5:
                        raise KeyError(path)
                    self._send(200, store.detail(parts[3], int(parts[4])))
                elif path.startswith("/comparisons/"):
                    parts = path.split("/")
                    if len(parts) != 5:
                        raise KeyError(path)
                    file = store.comparison_file(parts[2], parts[3], parts[4])
                    is_html = file.suffix == ".html"
                    self._send(200, file.read_bytes(), "text/html; charset=utf-8" if is_html else "application/json; charset=utf-8", report=is_html)
                else:
                    self._send(404, {"error": "未找到页面"})
            except (KeyError, ValueError, FileNotFoundError):
                self._send(404, {"error": "未找到记录或文件"})

        def do_POST(self):
            if not self._host_ok() or self.headers.get("Origin") != "http://" + self.headers.get("Host", "") or not secrets.compare_digest(self.headers.get("X-Console-Token", ""), csrf_token):
                self._send(403, {"error": "请求来源校验失败；请从本地控制台操作"}); return
            path = urlsplit(self.path).path
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= MAX_UPLOAD or self.headers.get("Transfer-Encoding"):
                    raise ValueError("请求体必须为 1 字节至 10 MB")
                self.connection.settimeout(20)
                body = self.rfile.read(length)
                if len(body) != length:
                    raise ValueError("请求体未完整上传")
                if path == "/api/import":
                    self._send(200, read_xlsx(body))
                else:
                    if self.headers.get_content_type() != "application/json":
                        raise ValueError("需要 application/json")
                    value = json.loads(body)
                    if path == "/api/batches":
                        self._send(200, store.create(value))
                    elif path == "/api/epsilon/models":
                        if not isinstance(value, dict) or set(value) - {"base_url", "user_id", "company_id", "project_id", "agent_id"}:
                            raise ValueError("Epsilon 连接参数无效")
                        self._send(200, EpsilonClient(value).doctor())
                    elif path == "/api/mesh/models":
                        if not isinstance(value, dict) or set(value) - {"base_url", "company_id", "project_id", "agent_id"}:
                            raise ValueError("Mesh 连接参数无效")
                        self._send(200, MeshClient.from_config(value).doctor())
                    elif path.startswith("/api/batches/") and path.endswith("/pause"):
                        parts = path.split("/")
                        if len(parts) != 5 or not isinstance(value, dict) or type(value.get("paused")) is not bool:
                            raise ValueError("暂停参数无效")
                        self._send(200, store.set_paused(parts[3], value["paused"]))
                    elif path.startswith("/api/batches/") and path.endswith("/retry-unsubmitted"):
                        parts = path.split("/")
                        if len(parts) != 5 or value != {"retry": True}:
                            raise ValueError("重试参数无效")
                        self._send(200, store.retry_unsubmitted(parts[3]))
                    elif path.startswith("/api/batches/") and path.endswith("/comparison"):
                        parts = path.split("/")
                        if len(parts) != 5 or value != {}:
                            raise ValueError("对比请求无效")
                        self._send(200, store.comparison(parts[3]))
                    else:
                        self._send(404, {"error": "接口不存在"})
            except KeyError:
                self._send(404, {"error": "批次不存在"})
            except (ValueError, TypeError, zipfile.BadZipFile, RuntimeError) as exc:
                self._send(400, {"error": str(exc)})
            except Exception as exc:
                self._send(500, {"error": f"操作未确认成功，请检查批次记录：{exc}"})

        def log_message(self, *args):
            pass
    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, help="批次与独立工作目录的保存位置")
    parser.add_argument("--port", type=int, default=18749)
    args = parser.parse_args()
    root = Path(args.root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    # OS advisory lock is released on exit, including crashes. No stale-lock deletion.
    lockfile = (root / ".console.lock").open("a+b")
    try:
        if __import__("os").name == "nt":
            import msvcrt
            lockfile.seek(0); lockfile.write(b"0"); lockfile.flush(); lockfile.seek(0)
            msvcrt.locking(lockfile.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        parser.error("该任务目录已有控制台运行，请使用已有窗口")
    store = ConsoleStore(root)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(store, secrets.token_urlsafe(32)))
    store.start()
    print(f"http://127.0.0.1:{server.server_port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        store.stop.set()
        server.server_close()
        lockfile.close()


if __name__ == "__main__":
    main()
