"""Изолированный файловый worker: JSON-задание → Codex → JSON-результат."""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

from .ai_analyst import run_codex_analysis


log = logging.getLogger("hermes.ai_worker")


def spool_root() -> Path:
    return Path(os.environ.get("AI_SPOOL_DIR", "/var/lib/hermes-ai/spool"))


def ensure_spool() -> tuple[Path, Path, Path]:
    root = spool_root()
    jobs = root / "jobs"
    processing = root / "processing"
    results = root / "results"
    for path in (jobs, processing, results):
        path.mkdir(parents=True, exist_ok=True)
        # setgid: файлы от бота (hermes) наследуют группу hermes-ai, поэтому
        # worker читает задания без расширения доступа к основному приложению.
        try:
            os.chmod(path, 0o2770)
        except PermissionError:
            pass
    return jobs, processing, results


def _atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, default=str), encoding="utf-8")
    os.chmod(temporary, 0o660)
    temporary.replace(path)


def process_one() -> bool:
    jobs, processing, results = ensure_spool()
    candidates = sorted(jobs.glob("*.json"), key=lambda p: p.stat().st_mtime)
    if not candidates:
        return False
    source = candidates[0]
    active = processing / source.name
    try:
        source.replace(active)
    except FileNotFoundError:
        return True

    run_id = active.stem
    try:
        job = json.loads(active.read_text(encoding="utf-8"))
        run_id = str(job["run_id"])
        result = run_codex_analysis(job["payload"])
        result.update({
            "run_id": run_id,
            "chat_id": str(job.get("chat_id") or ""),
            "mode": job.get("mode", "shadow"),
            "report_type": job["payload"].get("report_type"),
            "report_id": job["payload"].get("report_id"),
        })
    except Exception as exc:
        log.exception("AI job %s failed", run_id)
        result = {
            "run_id": run_id,
            "chat_id": "",
            "mode": "shadow",
            "status": "failed",
            "error": f"{type(exc).__name__}: {exc}",
        }
    _atomic_json(results / f"{run_id}.json", result)
    active.unlink(missing_ok=True)
    return True


def run_forever() -> None:
    ensure_spool()
    log.info("AI worker started: %s", spool_root())
    while True:
        if not process_one():
            time.sleep(float(os.environ.get("AI_WORKER_POLL_SECONDS", "2")))


if __name__ == "__main__":
    run_forever()
