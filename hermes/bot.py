"""Telegram-бот Hermes: long-polling, обработка команд пользователя.

Запускается как отдельный systemd-сервис (hermes-bot.service).
Блокирующий цикл — бот живёт пока работает процесс.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta

log = logging.getLogger("hermes.bot")

_HELP = """\
📋 Доступные команды:

/продажи — за вчера
/продажи 2026-07-20 — за день
/продажи 2026-07-01 2026-07-31 — за период

/остатки — текущий снимок (сегодня)
/остатки 2026-08-01 — на конкретную дату

/залежалые — СРЕЗКА ≥3 дн., прочие ≥30 дн.
/залежалые 5 14 — свои пороги (СРЕЗКА N дн., прочие M дн.)

/списания — за последние 30 дней
/списания 2026-07-01 2026-07-31 — за период

/закупки — за последние 30 дней
/закупки 2026-07-01 2026-07-31 — за период

/деньги — ДДС за последние 30 дней
/деньги 2026-07-01 2026-07-31 — за период

/помощь — этот список\
"""


def run(conn_factory, client_factory, bot_token: str, chat_id: str) -> None:
    """Запустить long-polling. Блокирует текущий поток."""
    log.info("Бот запущен (long-polling)")
    offset = 0
    while True:
        try:
            updates = _get_updates(bot_token, offset, timeout=30)
        except Exception as e:
            log.error("getUpdates: %s — пауза 10 с", e)
            time.sleep(10)
            continue

        for upd in updates:
            offset = upd["update_id"] + 1
            msg = upd.get("message", {})
            from_chat = str(msg.get("chat", {}).get("id", ""))
            text = (msg.get("text") or "").strip()

            # Принимаем команды только от авторизованного чата
            if from_chat != chat_id or not text:
                continue

            log.info("Команда: %s", text[:80])
            try:
                reply = _dispatch(text, conn_factory, client_factory, bot_token, chat_id)
            except Exception as e:
                log.exception("Ошибка команды %r: %s", text, e)
                reply = f"⚠️ Ошибка: {e}"

            if reply:
                _tg_send(bot_token, chat_id, reply)


# ─── диспетчер команд ────────────────────────────────────────────────────────

def _dispatch(text: str, conn_factory, client_factory, bot_token, chat_id) -> str:
    parts = text.strip().split()
    cmd = parts[0].lower().lstrip("/").split("@")[0]
    args = parts[1:]

    if cmd in ("помощь", "help", "start"):
        return _HELP

    if cmd == "продажи":
        return _cmd_sales(args, conn_factory, client_factory, bot_token, chat_id)

    if cmd == "остатки":
        return _cmd_stock(args, conn_factory, client_factory, bot_token, chat_id)

    if cmd == "залежалые":
        return _cmd_stale(args, conn_factory, bot_token, chat_id)

    if cmd == "списания":
        return _cmd_loss(args, conn_factory, client_factory, bot_token, chat_id)

    if cmd == "закупки":
        return _cmd_supply(args, conn_factory, client_factory, bot_token, chat_id)

    if cmd in ("деньги", "ддс"):
        return _cmd_cashflow(args, conn_factory, client_factory, bot_token, chat_id)

    return f"Неизвестная команда. Напиши /помощь"


# ─── команда /продажи ────────────────────────────────────────────────────────

def _cmd_sales(args, conn_factory, client_factory, bot_token, chat_id) -> str:
    from . import config, db
    from .etl_sales import run as etl_sales
    from .report_sales import build_day_report, build_period_report
    from .moysklad import MoyskladClient

    d_from, d_to = _parse_period(args, default_days=1)

    conn = conn_factory()
    client = client_factory()

    # Синхронизируем, если дней нет в БД
    missing = _missing_days(conn, d_from, d_to)
    if missing:
        _tg_send(bot_token, chat_id,
                 f"⏳ Подгружаю данные за {len(missing)} дн. из МойСклад...")
        etl_sales(client, conn, min(missing), max(missing))

    if d_from == d_to:
        return build_day_report(conn, d_from)
    return build_period_report(conn, d_from, d_to)


# ─── команда /остатки ────────────────────────────────────────────────────────

def _cmd_stock(args, conn_factory, client_factory, bot_token, chat_id) -> str:
    from . import db
    from .etl_stock import run as etl_stock
    from .report_stock import build_stock_report
    from .moysklad import MoyskladClient

    snap_date = _parse_single_date(args) if args else date.today()
    conn = conn_factory()
    client = client_factory()

    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM stock_snapshot WHERE day=%s", (snap_date,))
        has_snap = cur.fetchone()[0] > 0

    if not has_snap:
        _tg_send(bot_token, chat_id,
                 f"⏳ Снимаю остатки на {snap_date.strftime('%d.%m.%Y')}...")
        etl_stock(client, conn, snap_date)

    return build_stock_report(conn, snap_date)


# ─── команда /залежалые ──────────────────────────────────────────────────────

def _cmd_stale(args, conn_factory, bot_token, chat_id) -> str:
    from .report_stock import build_stock_report, STALE_SREZKA_DAYS, STALE_OTHER_DAYS
    import hermes.report_stock as rs

    # Парсим кастомные пороги: /залежалые 5 14
    srezka_days = STALE_SREZKA_DAYS
    other_days = STALE_OTHER_DAYS
    try:
        if len(args) >= 1:
            srezka_days = int(args[0])
        if len(args) >= 2:
            other_days = int(args[1])
    except ValueError:
        pass

    conn = conn_factory()

    # Временно переопределяем пороги в модуле
    orig_s, orig_o = rs.STALE_SREZKA_DAYS, rs.STALE_OTHER_DAYS
    rs.STALE_SREZKA_DAYS = srezka_days
    rs.STALE_OTHER_DAYS = other_days
    try:
        return build_stock_report(conn, date.today())
    finally:
        rs.STALE_SREZKA_DAYS = orig_s
        rs.STALE_OTHER_DAYS = orig_o


# ─── команда /списания ───────────────────────────────────────────────────────

def _cmd_loss(args, conn_factory, client_factory, bot_token, chat_id) -> str:
    from .etl_loss import run as etl_loss
    from .report_loss import build_loss_report

    d_from, d_to = _parse_last_n_days(args)
    conn = conn_factory()
    client = client_factory()

    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM loss_doc WHERE day BETWEEN %s AND %s", (d_from, d_to)
        )
        has_data = cur.fetchone()[0] > 0

    if not has_data:
        _tg_send(bot_token, chat_id, "⏳ Подгружаю списания из МойСклад...")
        etl_loss(client, conn, d_from, d_to)

    return build_loss_report(conn, d_from, d_to)


# ─── команда /закупки ────────────────────────────────────────────────────────

def _cmd_supply(args, conn_factory, client_factory, bot_token, chat_id) -> str:
    from .etl_supply import run as etl_supply
    from .report_supply import build_supply_report

    d_from, d_to = _parse_last_n_days(args)
    conn = conn_factory()
    client = client_factory()

    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM supply_doc WHERE day BETWEEN %s AND %s", (d_from, d_to)
        )
        has_data = cur.fetchone()[0] > 0

    if not has_data:
        _tg_send(bot_token, chat_id, "⏳ Подгружаю поставки из МойСклад...")
        etl_supply(client, conn, d_from, d_to)

    return build_supply_report(conn, d_from, d_to)


# ─── команда /деньги ─────────────────────────────────────────────────────────

def _cmd_cashflow(args, conn_factory, client_factory, bot_token, chat_id) -> str:
    from .etl_cashflow import run as etl_cashflow
    from .report_cashflow import build_cashflow_report

    d_from, d_to = _parse_last_n_days(args)
    conn = conn_factory()
    client = client_factory()

    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM cashflow_event WHERE day BETWEEN %s AND %s", (d_from, d_to)
        )
        has_data = cur.fetchone()[0] > 0

    if not has_data:
        _tg_send(bot_token, chat_id, "⏳ Подгружаю платежи из МойСклад...")
        etl_cashflow(client, conn, d_from, d_to)

    return build_cashflow_report(conn, d_from, d_to)


# ─── вспомогательные ─────────────────────────────────────────────────────────

def _parse_period(args: list[str], default_days: int = 1) -> tuple[date, date]:
    """Парсит 0, 1 или 2 даты из аргументов команды."""
    today = date.today()
    if not args:
        d = today - timedelta(days=default_days)
        return d, d
    if len(args) == 1:
        d = _parse_single_date(args)
        return d, d
    d_from = datetime.strptime(args[0], "%Y-%m-%d").date()
    d_to = datetime.strptime(args[1], "%Y-%m-%d").date()
    if d_from > d_to:
        d_from, d_to = d_to, d_from
    return d_from, d_to


def _parse_last_n_days(args: list[str], n: int = 30) -> tuple[date, date]:
    """По умолчанию — последние N дней (включая сегодня).
    Если переданы 1 или 2 даты — парсим как _parse_period."""
    if not args:
        today = date.today()
        return today - timedelta(days=n - 1), today
    return _parse_period(args)


def _parse_single_date(args: list[str]) -> date:
    return datetime.strptime(args[0], "%Y-%m-%d").date()


def _missing_days(conn, d_from: date, d_to: date) -> list[date]:
    """Дни в периоде, которых нет в sales_by_store_day."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT day FROM sales_by_store_day WHERE day BETWEEN %s AND %s",
            (d_from, d_to),
        )
        have = {r[0] for r in cur.fetchall()}
    all_days = []
    d = d_from
    while d <= d_to:
        if d not in have:
            all_days.append(d)
        d += timedelta(days=1)
    return all_days


def _get_updates(bot_token: str, offset: int, timeout: int = 30) -> list[dict]:
    url = (
        f"https://api.telegram.org/bot{bot_token}/getUpdates"
        f"?offset={offset}&timeout={timeout}&allowed_updates=%5B%22message%22%5D"
    )
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=timeout + 10) as resp:
        data = json.loads(resp.read())
    if not data.get("ok"):
        raise RuntimeError(f"getUpdates: {data}")
    return data.get("result", [])


def _tg_send(bot_token: str, chat_id: str, text: str) -> None:
    from .telegram import send_message
    send_message(bot_token, chat_id, text)
