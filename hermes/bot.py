"""Telegram-бот Hermes: long-polling, меню с кнопками, интерактивные отчёты.

Навигация:
  • Reply-keyboard  — постоянные кнопки снизу экрана (главное меню)
  • Inline-keyboard — кнопки выбора периода, прикреплённые к отчёту
  • Ввод текстом   — /команда [дата1] [дата2] для произвольного периода
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta

from . import telegram as tg

log = logging.getLogger("hermes.bot")

# ─── маппинг текста кнопок → секции ──────────────────────────────────────────

_BUTTON_MAP: dict[str, str] = {
    "📊 продажи":     "sales",
    "📦 остатки":     "stock",
    "🚨 залежалые":   "stale",
    "🗑 списания":    "loss",
    "📥 закупки":     "supply",
    "💰 ддс":         "cashflow",
    "👥 сотрудники":  "employees",
    "❓ помощь":      "help",
}

_HELP_TEXT = """\
📋 Команды Hermes

📊 Продажи
  /продажи — вчера
  /продажи 2026-07-20 — за день
  /продажи 2026-07-01 2026-07-31 — за период

📦 Остатки
  /остатки — сегодня
  /остатки 2026-08-01 — на дату

🚨 Залежалые
  /залежалые — СРЕЗКА ≥3 д., прочие ≥30 д.
  /залежалые 5 60 — свои пороги

🗑 Списания
  /списания — последние 30 дней
  /списания 2026-07-01 2026-07-31 — период

📥 Закупки
  /закупки — последние 30 дней
  /закупки 2026-07-01 2026-07-31 — период

💰 ДДС (деньги)
  /деньги — последние 30 дней
  /деньги 2026-07-01 2026-07-31 — период

👥 Сотрудники
  /сотрудники — последние 30 дней
  /сотрудники 2026-07-01 2026-07-31 — период

❓ /помощь — этот список\
"""


# ─── главный цикл ─────────────────────────────────────────────────────────────

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
            try:
                _handle_update(upd, conn_factory, client_factory, bot_token, chat_id)
            except Exception as e:
                log.exception("Необработанная ошибка в update: %s", e)


# ─── диспетчер обновлений ─────────────────────────────────────────────────────

def _handle_update(upd, conn_factory, client_factory, bot_token, chat_id):
    # ── сообщение ──
    if "message" in upd:
        msg = upd["message"]
        from_chat = str(msg.get("chat", {}).get("id", ""))
        text = (msg.get("text") or "").strip()
        if from_chat != chat_id or not text:
            return

        log.info("Сообщение: %s", text[:80])
        reply, markup = _dispatch_text(text, conn_factory, client_factory, bot_token, chat_id)
        if reply:
            # Всегда добавляем главную клавиатуру к ответу
            merged_markup = markup or tg.main_reply_keyboard()
            tg.send_message(bot_token, chat_id, reply, reply_markup=merged_markup)

    # ── нажатие inline-кнопки ──
    elif "callback_query" in upd:
        cq = upd["callback_query"]
        from_chat = str(cq.get("message", {}).get("chat", {}).get("id", ""))
        if from_chat != chat_id:
            return

        cq_id      = cq["id"]
        cq_data    = cq.get("data", "")
        message_id = cq["message"]["message_id"]

        log.info("Callback: %s", cq_data)
        tg.answer_callback_query(bot_token, cq_id, "⏳ Готовлю отчёт…")

        try:
            reply, markup = _dispatch_callback(
                cq_data, conn_factory, client_factory, bot_token, chat_id
            )
        except Exception as e:
            log.exception("Ошибка callback %r: %s", cq_data, e)
            reply, markup = f"⚠️ Ошибка: {e}", None

        if reply:
            # Отправляем новым сообщением (не редактируем, т.к. длина может меняться)
            tg.send_message(
                bot_token, chat_id, reply,
                reply_markup=markup or tg.main_reply_keyboard()
            )


# ─── текстовые команды ────────────────────────────────────────────────────────

def _dispatch_text(text, conn_factory, client_factory, bot_token, chat_id):
    """Обработать текстовое сообщение. Возвращает (reply_text, markup|None)."""
    # Кнопки главного меню — нормализуем и ищем в маппинге
    normalized = text.lower().strip()
    section = _BUTTON_MAP.get(normalized)
    if section:
        return _section_default(section, conn_factory, client_factory, bot_token, chat_id)

    # Текстовые команды (/продажи, продажи, ...)
    parts = text.strip().split()
    cmd = parts[0].lower().lstrip("/").split("@")[0]
    args = parts[1:]

    if cmd in ("start", "помощь", "help"):
        return _HELP_TEXT, tg.main_reply_keyboard()

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

    if cmd == "сотрудники":
        return _cmd_employees(args, client_factory)

    return "Неизвестная команда. Нажмите ❓ Помощь.", tg.main_reply_keyboard()


def _section_default(section, conn_factory, client_factory, bot_token, chat_id):
    """Кнопка главного меню → отчёт за дефолтный период + inline-кнопки периодов."""
    if section == "help":
        return _HELP_TEXT, tg.main_reply_keyboard()

    if section == "sales":
        text, _ = _cmd_sales([], conn_factory, client_factory, bot_token, chat_id)
    elif section == "stock":
        text, _ = _cmd_stock([], conn_factory, client_factory, bot_token, chat_id)
    elif section == "stale":
        text, _ = _cmd_stale([], conn_factory, bot_token, chat_id)
    elif section == "loss":
        text, _ = _cmd_loss([], conn_factory, client_factory, bot_token, chat_id)
    elif section == "supply":
        text, _ = _cmd_supply([], conn_factory, client_factory, bot_token, chat_id)
    elif section == "cashflow":
        text, _ = _cmd_cashflow([], conn_factory, client_factory, bot_token, chat_id)
    elif section == "employees":
        text, _ = _cmd_employees([], client_factory)
    else:
        return "Раздел не найден.", tg.main_reply_keyboard()

    # Сначала отправляем главное меню (чтобы не потерять), потом отчёт с inline
    return text, tg.period_inline_keyboard(section)


# ─── inline-кнопки периода ────────────────────────────────────────────────────

def _dispatch_callback(data, conn_factory, client_factory, bot_token, chat_id):
    """Обработать нажатие inline-кнопки. Возвращает (reply_text, markup|None)."""
    parts = data.split(":")
    section = parts[0]
    sub = parts[1] if len(parts) > 1 else ""

    d_from, d_to = _period_from_shortcut(sub)

    if section == "sales":
        reply, _ = _cmd_sales_period(d_from, d_to, conn_factory, client_factory, bot_token, chat_id)
    elif section == "stock":
        reply, _ = _cmd_stock_date(d_from, conn_factory, client_factory, bot_token, chat_id)
    elif section == "stale":
        s_days = int(parts[1]) if len(parts) > 1 else 3
        o_days = int(parts[2]) if len(parts) > 2 else 30
        reply = _do_stale_report(s_days, o_days, conn_factory)
    elif section == "loss":
        reply, _ = _cmd_loss_period(d_from, d_to, conn_factory, client_factory, bot_token, chat_id)
    elif section == "supply":
        reply, _ = _cmd_supply_period(d_from, d_to, conn_factory, client_factory, bot_token, chat_id)
    elif section == "cashflow":
        reply, _ = _cmd_cashflow_period(d_from, d_to, conn_factory, client_factory, bot_token, chat_id)
    elif section == "employees":
        reply, _ = _cmd_employees_period(d_from, d_to, client_factory)
    else:
        reply = "Неизвестное действие."

    return reply, tg.period_inline_keyboard(section)


def _period_from_shortcut(sub: str):
    today = date.today()
    if sub == "today":
        return today, today
    if sub == "yesterday":
        y = today - timedelta(days=1)
        return y, y
    if sub == "month":
        first = today.replace(day=1)
        return first, today
    try:
        n = int(sub)
        return today - timedelta(days=n - 1), today
    except (ValueError, TypeError):
        return today - timedelta(days=29), today


# ─── /продажи ────────────────────────────────────────────────────────────────

def _cmd_sales(args, conn_factory, client_factory, bot_token, chat_id):
    if not args:
        # дефолт — вчера
        yesterday = date.today() - timedelta(days=1)
        return _cmd_sales_period(yesterday, yesterday, conn_factory, client_factory, bot_token, chat_id)
    d_from, d_to = _parse_period(args)
    return _cmd_sales_period(d_from, d_to, conn_factory, client_factory, bot_token, chat_id)


def _cmd_sales_period(d_from, d_to, conn_factory, client_factory, bot_token, chat_id):
    from .etl_sales import run as etl_sales
    from .report_sales import build_day_report, build_period_report

    conn = conn_factory()
    client = client_factory()
    missing = _missing_days(conn, d_from, d_to)
    if missing:
        tg.send_message(bot_token, chat_id,
                        f"⏳ Подгружаю данные за {len(missing)} дн. из МойСклад…")
        etl_sales(client, conn, min(missing), max(missing))

    text = build_day_report(conn, d_from) if d_from == d_to else build_period_report(conn, d_from, d_to)
    return text, None


# ─── /остатки ────────────────────────────────────────────────────────────────

def _cmd_stock(args, conn_factory, client_factory, bot_token, chat_id):
    snap_date = _parse_single_date(args) if args else date.today()
    return _cmd_stock_date(snap_date, conn_factory, client_factory, bot_token, chat_id)


def _cmd_stock_date(snap_date, conn_factory, client_factory, bot_token, chat_id):
    from .etl_stock import run as etl_stock
    from .report_stock import build_stock_report

    conn = conn_factory()
    client = client_factory()
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM stock_snapshot WHERE day=%s", (snap_date,))
        has_snap = cur.fetchone()[0] > 0

    if not has_snap:
        tg.send_message(bot_token, chat_id,
                        f"⏳ Снимаю остатки на {snap_date.strftime('%d.%m.%Y')}…")
        etl_stock(client, conn, snap_date)

    return build_stock_report(conn, snap_date), None


# ─── /залежалые ──────────────────────────────────────────────────────────────

def _cmd_stale(args, conn_factory, bot_token, chat_id):
    from .report_stock import STALE_SREZKA_DAYS, STALE_OTHER_DAYS
    s_days = STALE_SREZKA_DAYS
    o_days = STALE_OTHER_DAYS
    try:
        if len(args) >= 1:
            s_days = int(args[0])
        if len(args) >= 2:
            o_days = int(args[1])
    except ValueError:
        pass
    return _do_stale_report(s_days, o_days, conn_factory), None


def _do_stale_report(s_days, o_days, conn_factory):
    import hermes.report_stock as rs
    conn = conn_factory()
    orig_s, orig_o = rs.STALE_SREZKA_DAYS, rs.STALE_OTHER_DAYS
    rs.STALE_SREZKA_DAYS = s_days
    rs.STALE_OTHER_DAYS  = o_days
    try:
        return rs.build_stock_report(conn, date.today())
    finally:
        rs.STALE_SREZKA_DAYS = orig_s
        rs.STALE_OTHER_DAYS  = orig_o


# ─── /списания ───────────────────────────────────────────────────────────────

def _cmd_loss(args, conn_factory, client_factory, bot_token, chat_id):
    d_from, d_to = _parse_last_n_days(args)
    return _cmd_loss_period(d_from, d_to, conn_factory, client_factory, bot_token, chat_id)


def _cmd_loss_period(d_from, d_to, conn_factory, client_factory, bot_token, chat_id):
    from .etl_loss import run as etl_loss
    from .report_loss import build_loss_report

    conn = conn_factory()
    client = client_factory()
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM loss_doc WHERE day BETWEEN %s AND %s", (d_from, d_to))
        has_data = cur.fetchone()[0] > 0

    if not has_data:
        tg.send_message(bot_token, chat_id, "⏳ Подгружаю списания из МойСклад…")
        etl_loss(client, conn, d_from, d_to)

    return build_loss_report(conn, d_from, d_to), None


# ─── /закупки ────────────────────────────────────────────────────────────────

def _cmd_supply(args, conn_factory, client_factory, bot_token, chat_id):
    d_from, d_to = _parse_last_n_days(args)
    return _cmd_supply_period(d_from, d_to, conn_factory, client_factory, bot_token, chat_id)


def _cmd_supply_period(d_from, d_to, conn_factory, client_factory, bot_token, chat_id):
    from .etl_supply import run as etl_supply
    from .report_supply import build_supply_report

    conn = conn_factory()
    client = client_factory()
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM supply_doc WHERE day BETWEEN %s AND %s", (d_from, d_to))
        has_data = cur.fetchone()[0] > 0

    if not has_data:
        tg.send_message(bot_token, chat_id, "⏳ Подгружаю поставки из МойСклад…")
        etl_supply(client, conn, d_from, d_to)

    return build_supply_report(conn, d_from, d_to), None


# ─── /деньги (ДДС) ───────────────────────────────────────────────────────────

def _cmd_cashflow(args, conn_factory, client_factory, bot_token, chat_id):
    d_from, d_to = _parse_last_n_days(args)
    return _cmd_cashflow_period(d_from, d_to, conn_factory, client_factory, bot_token, chat_id)


def _cmd_cashflow_period(d_from, d_to, conn_factory, client_factory, bot_token, chat_id):
    from .etl_cashflow import run as etl_cashflow
    from .report_cashflow import build_cashflow_report

    conn = conn_factory()
    client = client_factory()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM cashflow_event WHERE day BETWEEN %s AND %s", (d_from, d_to)
        )
        has_data = cur.fetchone()[0] > 0

    if not has_data:
        tg.send_message(bot_token, chat_id, "⏳ Подгружаю платежи из МойСклад…")
        etl_cashflow(client, conn, d_from, d_to)

    return build_cashflow_report(conn, d_from, d_to), None


# ─── /сотрудники ─────────────────────────────────────────────────────────────

def _cmd_employees(args, client_factory):
    d_from, d_to = _parse_last_n_days(args)
    return _cmd_employees_period(d_from, d_to, client_factory)


def _cmd_employees_period(d_from, d_to, client_factory):
    from .report_employees import build_employee_report
    client = client_factory()
    return build_employee_report(client, d_from, d_to), None


# ─── вспомогательные ─────────────────────────────────────────────────────────

def _parse_period(args: list[str]) -> tuple[date, date]:
    if not args:
        y = date.today() - timedelta(days=1)
        return y, y
    if len(args) == 1:
        d = _parse_single_date(args)
        return d, d
    d_from = datetime.strptime(args[0], "%Y-%m-%d").date()
    d_to   = datetime.strptime(args[1], "%Y-%m-%d").date()
    if d_from > d_to:
        d_from, d_to = d_to, d_from
    return d_from, d_to


def _parse_last_n_days(args: list[str], n: int = 30) -> tuple[date, date]:
    if not args:
        today = date.today()
        return today - timedelta(days=n - 1), today
    return _parse_period(args)


def _parse_single_date(args: list[str]) -> date:
    return datetime.strptime(args[0], "%Y-%m-%d").date()


def _missing_days(conn, d_from: date, d_to: date) -> list[date]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT day FROM sales_by_store_day WHERE day BETWEEN %s AND %s",
            (d_from, d_to),
        )
        have = {r[0] for r in cur.fetchall()}
    result, d = [], d_from
    while d <= d_to:
        if d not in have:
            result.append(d)
        d += timedelta(days=1)
    return result


def _get_updates(bot_token: str, offset: int, timeout: int = 30) -> list[dict]:
    import json, urllib.request
    url = (
        f"https://api.telegram.org/bot{bot_token}/getUpdates"
        f"?offset={offset}&timeout={timeout}"
        f"&allowed_updates=%5B%22message%22%2C%22callback_query%22%5D"
    )
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=timeout + 10) as resp:
        data = json.loads(resp.read())
    if not data.get("ok"):
        raise RuntimeError(f"getUpdates: {data}")
    return data.get("result", [])
