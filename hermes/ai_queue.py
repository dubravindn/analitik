"""Очередь AI-задач, хранение запусков и доставка проверенной сводки."""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from .ai_analyst import PROMPT_VERSION, render_telegram_summary
from .ai_worker import ensure_spool


log = logging.getLogger("hermes.ai_queue")
_monitor_started = False
_monitor_lock = threading.Lock()


def mode() -> str:
    value = os.environ.get("AI_ANALYST_MODE", "shadow").strip().lower()
    return value if value in {"off", "shadow", "live"} else "shadow"


def _atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, default=str), encoding="utf-8")
    os.chmod(temporary, 0o660)
    temporary.replace(path)


def enqueue_analysis(conn, payload: dict[str, Any], chat_id: str, *, force_mode: str | None = None) -> str | None:
    run_mode = force_mode or mode()
    if run_mode == "off":
        return None
    run_id = str(uuid.uuid4())
    jobs, _processing, _results = ensure_spool()
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO ai_analysis_run
                (id, report_type, report_id, chat_id, payload_hash, prompt_version,
                 model, mode, status, payload_json, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'queued', %s::jsonb, now())
        """, (
            run_id, payload.get("report_type"), payload.get("report_id"), str(chat_id),
            payload.get("payload_hash"), PROMPT_VERSION,
            os.environ.get("AI_CODEX_MODEL", "subscription-default"), run_mode,
            json.dumps(payload, ensure_ascii=False, default=str),
        ))
    conn.commit()
    _atomic_json(jobs / f"{run_id}.json", {
        "run_id": run_id,
        "chat_id": str(chat_id),
        "mode": run_mode,
        "payload": payload,
    })
    log.info("AI job queued: %s %s %s", run_id, payload.get("report_type"), run_mode)
    return run_id


def _feedback_markup(run_id: str) -> dict:
    return {
        "inline_keyboard": [[
            {"text": "👍 Полезно", "callback_data": f"ai_feedback:{run_id}:up"},
            {"text": "👎 Неважно", "callback_data": f"ai_feedback:{run_id}:down"},
        ]]
    }


def store_feedback(conn, run_id: str, chat_id: str, value: str) -> bool:
    if value not in {"up", "down"}:
        return False
    with conn.cursor() as cur:
        cur.execute("""
            SELECT 1 FROM ai_analysis_run
            WHERE id=%s
              AND POSITION(',' || %s || ',' IN ',' || chat_id || ',') > 0
        """, (run_id, str(chat_id)))
        if cur.fetchone() is None:
            return False
        cur.execute("""
            INSERT INTO ai_feedback (run_id, chat_id, value, created_at)
            VALUES (%s, %s, %s, now())
            ON CONFLICT (run_id, chat_id) DO UPDATE SET value=EXCLUDED.value, created_at=now()
        """, (run_id, str(chat_id), value))
    conn.commit()
    return True


def _consume_result(conn_factory: Callable, bot_token: str, path: Path) -> None:
    from . import telegram as tg

    result = json.loads(path.read_text(encoding="utf-8"))
    run_id = str(result.get("run_id") or path.stem)
    conn = conn_factory()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE ai_analysis_run
                SET status=%s, raw_response=%s, validated_json=%s::jsonb,
                    error=%s, duration_ms=%s, attempts=%s, completed_at=now()
                WHERE id=%s
            """, (
                result.get("status", "failed"), result.get("raw", ""),
                json.dumps(result.get("validated"), ensure_ascii=False, default=str)
                if result.get("validated") is not None else None,
                result.get("error"), result.get("duration_ms"), result.get("attempts"), run_id,
            ))
        conn.commit()

        run_mode = result.get("mode", "shadow")
        if run_mode == "shadow":
            log.info("AI shadow result %s: %s", run_id, result.get("status"))
        elif run_mode == "live" and result.get("status") == "validated":
            for target in str(result.get("chat_id") or "").split(","):
                if target.strip():
                    tg.send_message(
                        bot_token, target.strip(), render_telegram_summary(result),
                        _feedback_markup(run_id),
                    )
        elif run_mode == "live":
            for target in str(result.get("chat_id") or "").split(","):
                if target.strip():
                    tg.send_message(
                        bot_token, target.strip(),
                        "⚠️ ИИ-анализ временно недоступен. Основной PDF сформирован корректно.",
                    )
    finally:
        try:
            conn.close()
        except Exception:
            pass
    path.unlink(missing_ok=True)


def start_result_monitor(conn_factory: Callable, bot_token: str) -> None:
    global _monitor_started
    if mode() == "off":
        return
    with _monitor_lock:
        if _monitor_started:
            return
        _monitor_started = True

    def _run() -> None:
        _jobs, _processing, results = ensure_spool()
        log.info("AI result monitor started: mode=%s", mode())
        while True:
            paths = sorted(results.glob("*.json"), key=lambda p: p.stat().st_mtime)
            if not paths:
                time.sleep(2)
                continue
            for path in paths:
                try:
                    _consume_result(conn_factory, bot_token, path)
                except Exception:
                    log.exception("Cannot consume AI result %s", path.name)
                    time.sleep(5)

    threading.Thread(target=_run, name="hermes-ai-results", daemon=True).start()
