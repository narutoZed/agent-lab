"""Freeze completed batch evidence and reuse the existing comparison report engine."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import uuid

from common import digest
from compare import VERSION, apply_reviews, load_report, render_report, save
from orchestrate import export_report, load_exp, now, read


def evidence_signature(root, batch):
    files = []
    for index in range(len(batch["tasks"])):
        folder = Path(root) / batch["id"] / f"experiment-{index + 1}"
        for name in ["experiment.json"]:
            path = folder / name
            files.append((str(path), hashlib.sha256(path.read_bytes()).hexdigest()))
        for platform in batch["providers"]:
            dest = folder / "runs" / platform
            binding = read(dest / "observed-binding.json", {})
            paths = {dest / "events.jsonl", dest / "state.json", dest / "post-completion.json"}
            if binding.get("path"):
                paths.add(Path(binding["path"]))
            for path in sorted(paths):
                if path.is_file():
                    files.append((str(path), hashlib.sha256(path.read_bytes()).hexdigest()))
    template = Path(__file__).resolve().parents[1] / "assets/report.html"
    return digest({"files": files, "template": hashlib.sha256(template.read_bytes()).hexdigest(), "engine": VERSION,
                   "parallelism": batch.get("parallelism", {p: 1 for p in batch["providers"]})})


def build_comparison(root, batch):
    unfinished = [j for j in batch["jobs"] if j["status"] not in {"completed", "failed", "cancelled", "needs_configuration", "dispatch_failed", "launch_failed"}]
    if unfinished:
        raise ValueError("该批次还有执行中或排队的任务；结束后可查看完整横向对比")
    signature = evidence_signature(root, batch)
    previous = batch.get("comparison") or {}
    if previous.get("signature") == signature and Path(previous.get("report_path", "")).is_file():
        return {**previous, "reused": True}
    revision = uuid.uuid4().hex
    folder = Path(root) / batch["id"] / "comparisons" / revision
    folder.mkdir(parents=True)
    tasks = []
    for index, task in enumerate(batch["tasks"]):
        exp_root, exp = load_exp(Path(root) / batch["id"] / f"experiment-{index + 1}")
        result = export_report(exp_root, exp, folder / f"task-{index + 1}")
        manifest = read(result["manifest"])
        item = manifest["tasks"][0]
        # A separately fetched final snapshot can contain late billing/final text. Preserve
        # the original receipt and first terminal event so refreshing does not inflate time.
        extra_file = exp_root / "runs/jahead/post-completion.json"
        if extra_file.is_file() and "jahead" in exp["providers"]:
            extra = read(extra_file)
            native_state = read(exp_root / "runs/jahead/state.json")
            if extra.get("type") != "jahead.desktop.snapshot" or extra.get("session_id") != native_state.get("session_id") or extra.get("status") != "completed":
                raise ValueError("结束后快照与原任务身份或终态不匹配")
            target = next((r for r in item["runs"] if r["platform"] == "jahead"), None)
            if target:
                with Path(target["path"]).open("a", encoding="utf-8") as stream:
                    stream.write("\n" + json.dumps(extra, ensure_ascii=False) + "\n")
        item["title"] = task["title"]
        item["batch_parallelism"] = batch.get("parallelism", {p: 1 for p in batch["providers"]})
        item["alignment_notes"] += " 本批次按平台独立排队；连接修复后的任务开始时间可能不同，不是同时起跑或同模型实验。数值和缺失值沿用原报告口径；执行结束不等于质量验收通过。"
        tasks.append(item)
    manifest_path = folder / "manifest.json"
    save(manifest_path, {"schema_version": 1, "title": batch["title"] + " · 横向指标对比", "tasks": tasks})
    report = load_report(manifest_path)
    apply_reviews(report, [])
    render_report(report, folder / "report")
    # Observing may bind a previously undiscovered source; cache the resulting identity.
    signature = evidence_signature(root, batch)
    base_url = f"/comparisons/{batch['id']}/{revision}"
    return {"revision": revision, "created_at": now(), "signature": signature,
            "url": base_url + "/index.html", "json_url": base_url + "/results.json",
            "report_path": str(folder / "report/index.html"), "task_count": len(tasks), "reused": False}
