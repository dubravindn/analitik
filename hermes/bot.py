"""Telegram-бот Hermes: пошаговые диалоги, фильтры по складу, скрытие меню.

Навигация:
  • Reply-keyboard    — главное меню (скрывается / восстанавливается)
  • Диалог            — период → склад → отчёт
  • Текстовые команды — /продажи, /деньги и т.д. для опытных пользователей
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta

from . import config
from . import telegram as tg

log = logging.getLogger("hermes.bot")

# ─── описание секций и их шагов диалога ──────────────────────────────────────

# Для каждой секции: список шагов ["period", "store"]
_DIALOG_STEPS: dict[str, list[str]] = {
    "sales":  ["period", "store"],
    "stock":  ["period", "store"],
    "stale":  ["period", "store"],
    "loss":   ["period", "store"],
}

_SECTION_TITLE = {
    "sales": "📊 Продажи",
    "stock": "📦 Остатки",
    "stale": "🚨 Залежалые",
    "loss":  "🗑 Списания",
}

_BUTTON_TO_SECTION = {
    "📊 продажи":    "sales",
    "📦 остатки":    "stock",
    "🚨 залежалые":  "stale",
    "🗑 списания":   "loss",
    "❓ помощь":     "help",
    "🙈 скрыть меню":"hide",
    "/меню":         "show",
    "меню":          "show",
}

# кнопки → store_name (None = все склады)
_STORE_BUTTONS: dict[str, str | None] = {
    f"🏪 {s['short']}".lower(): s["name"] for s in config.STORES
}
_STORE_BUTTONS["📍 все склады"] = None  # type: ignore[assignment]

# Период-кнопки → функция(today) → (d_from, d_to)
_PERIOD_BUTTONS: dict[str, str] = {
    "📅 сегодня":        "today",
    "📅 вчера":          "yesterday",
    "📅 7 дней":         "7",
    "📅 14 дней":        "14",
    "📅 30 дней":        "30",
    "📅 текущий месяц":  "month",
}

_HELP_TEXT = """\
📋 Hermes — аналитика

Нажмите кнопку меню или введите команду:

📊 Продажи  → выберите период и склад
📦 Остатки  → выберите дату и склад
🚨 Залежалые → выберите дату и склад
🗑 Списания  → выберите период и склад
   (все документы с полным раскрытием позиций)

Дополнительные команды (без меню):
  /деньги [д1] [д2]       — ДДС
  /закупки [д1] [д2]      — закупки
  /сотрудники [д1] [д2]   — по сотрудникам

🙈 Скрыть меню — убирает кнопки
/меню — восстановить кнопки\
"""

# ─── состояние диалога (in-memory, один пользователь) ────────────────────────

_dialog: dict[str, dict] = {}  # chat_id → state


def _get_state(chat_id: str) -> dict | None:
    return _dialog.get(chat_id)


def _set_state(chat_id: str, section: str, step: str, params: dict) -> None:
    _dialog[chat_id] = {"section": section, "step": step, "params": params}


def _clear_state(chat_id: str) -> None:
    _dialog.pop(chat_id, None)


# ─── главный цикл ─────────────────────────────────────────────────────────────

def run(conn_factory, client_factory, bot_token: str, chat_id: str) -> None:
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
                _handle(upd, conn_factory, client_factory, bot_token, chat_id)
            except Exception as e:
                log.exception("Ошибка update: %s", e)


# ─── диспетчер ────────────────────────────────────────────────────────────────

def _handle(upd, conn_factory, client_factory, bot_token, chat_id):
    if "message" not in upd:
        return
    msg = upd["message"]
    from_chat = str(msg.get("chat", {}).get("id", ""))
    text = (msg.get("text") or "").strip()
    if from_chat != chat_id or not text:
        return

    log.info("Сообщение: %s", text[:80])
    norm = text.lower().strip()

    # ── Отмена диалога ──
    if norm in ("🚫 отмена", "/отмена", "отмена"):
        _clear_state(chat_id)
        tg.send_message(bot_token, chat_id, "❌ Отменено.", tg.main_reply_keyboard())
        return

    # ── Скрыть / показать меню ──
    if norm == "🙈 скрыть меню":
        _clear_state(chat_id)
        tg.send_message(bot_token, chat_id,
                        "🙈 Меню скрыто. Отправьте /меню чтобы вернуть.",
                        tg.remove_keyboard())
        return

    if norm in ("/меню", "меню", "/start"):
        _clear_state(chat_id)
        tg.send_message(bot_token, chat_id,
                        "📋 Меню восстановлено.", tg.main_reply_keyboard())
        return

    # ── Помощь ──
    if norm in ("❓ помощь", "/помощь", "/help"):
        _clear_state(chat_id)
        tg.send_message(bot_token, chat_id, _HELP_TEXT, tg.main_reply_keyboard())
        return

    # ── Кнопка главного меню → начать диалог ──
    section = _BUTTON_TO_SECTION.get(norm)
    if section in _DIALOG_STEPS:
        _clear_state(chat_id)
        _start_dialog(section, chat_id, bot_token)
        return

    # ── Продолжение диалога ──
    state = _get_state(chat_id)
    if state:
        _continue_dialog(state, text, norm, chat_id, conn_factory, client_factory, bot_token)
        return

    # ── Прямые команды (без диалога) ──
    _dispatch_command(text, conn_factory, client_factory, bot_token, chat_id)


# ─── диалог: начало ───────────────────────────────────────────────────────────

def _start_dialog(section: str, chat_id: str, bot_token: str) -> None:
    steps = _DIALOG_STEPS[section]
    first_step = steps[0]
    _set_state(chat_id, section, first_step, {})
    _ask_step(section, first_step, chat_id, bot_token)


def _ask_step(section: str, step: str, chat_id: str, bot_token: str) -> None:
    title = _SECTION_TITLE[section]
    if step == "period":
        if section == "stale":
            prompt = f"{title}\n\n📅 На какую дату показать снимок остатков?"
        else:
            prompt = f"{title}\n\n📅 За какой период?"
        tg.send_message(bot_token, chat_id, prompt, tg.period_keyboard())

    elif step == "store":
        prompt = f"{title}\n\n📍 По какому складу?"
        tg.send_message(bot_token, chat_id, prompt, tg.store_keyboard(config.STORES))


# ─── диалог: продолжение ─────────────────────────────────────────────────────

def _continue_dialog(state, text, norm, chat_id, conn_factory, client_factory, bot_token):
    section = state["section"]
    step    = state["step"]
    params  = state["params"]
    steps   = _DIALOG_STEPS[section]

    # ── Шаг: период ──
    if step == "period":
        period = _parse_period_button(norm)
        if period:
            params["d_from"], params["d_to"] = period
        elif _looks_like_dates(text):
            parsed = _try_parse_dates(text)
            if parsed:
                params["d_from"], params["d_to"] = parsed
            else:
                tg.send_message(bot_token, chat_id,
                                "❗ Не удалось разобрать даты.\n"
                                "Формат: 2026-07-01 или 2026-07-01 2026-07-31",
                                tg.period_keyboard())
                return
        elif norm == "✏️ ввести период":
            tg.send_message(bot_token, chat_id,
                            "✏️ Введите даты:\n"
                            "• Один день: 2026-07-20\n"
                            "• Период: 2026-07-01 2026-07-31",
                            tg.input_dates_keyboard())
            return
        else:
            tg.send_message(bot_token, chat_id,
                            "Выберите период из кнопок или введите даты.",
                            tg.period_keyboard())
            return

        # Переходим к следующему шагу
        next_step_idx = steps.index("period") + 1
        if next_step_idx < len(steps):
            next_step = steps[next_step_idx]
            _set_state(chat_id, section, next_step, params)
            _ask_step(section, next_step, chat_id, bot_token)
        else:
            _execute(section, params, chat_id, conn_factory, client_factory, bot_token)

    # ── Шаг: склад ──
    elif step == "store":
        if norm in _STORE_BUTTONS:
            params["store_name"] = _STORE_BUTTONS[norm]
            _clear_state(chat_id)
            _execute(section, params, chat_id, conn_factory, client_factory, bot_token)
        else:
            tg.send_message(bot_token, chat_id,
                            "Выберите склад из кнопок.",
                            tg.store_keyboard(config.STORES))


# ─── выполнение отчёта ────────────────────────────────────────────────────────

def _execute(section, params, chat_id, conn_factory, client_factory, bot_token):
    d_from     = params.get("d_from")
    d_to       = params.get("d_to")
    store_name = params.get("store_name")   # None = все склады

    store_lbl = store_name or "Все склады"
    tg.send_message(bot_token, chat_id,
                    f"⏳ Готовлю отчёт…\n"
                    f"Период: {_fmt_period(d_from, d_to)}\n"
                    f"Склад: {store_lbl}")

    try:
        if section == "sales":
            text = _run_sales(conn_factory, client_factory, d_from, d_to, store_name,
                              bot_token, chat_id)
        elif section == "stock":
            text = _run_stock(conn_factory, client_factory, d_from, store_name,
                              bot_token, chat_id)
        elif section == "stale":
            text = _run_stale(conn_factory, client_factory, d_from, store_name,
                              bot_token, chat_id)
        elif section == "loss":
            text = _run_loss(conn_factory, client_factory, d_from, d_to, store_name,
                             bot_token, chat_id)
        else:
            text = "Неизвестная секция."
    except Exception as e:
        log.exception("Ошибка при формировании отчёта %s: %s", section, e)
        text = f"⚠️ Ошибка при формировании отчёта: {e}"

    tg.send_message(bot_token, chat_id, text, tg.main_reply_keyboard())


# ─── выполнение конкретных секций ────────────────────────────────────────────

def _run_sales(conn_factory, client_factory, d_from, d_to, store_name, bot_token, chat_id):
    from .etl_sales import run as etl_sales
    from .report_sales import build_sales_analytics
    conn   = conn_factory()
    client = client_factory()
    missing = _missing_days(conn, d_from, d_to)
    if missing:
        tg.send_message(bot_token, chat_id,
                        f"⏳ Подгружаю {len(missing)} дн. из МойСклад…")
        etl_sales(client, conn, min(missing), max(missing))
    return build_sales_analytics(conn, d_from, d_to, store_name)


def _run_stock(conn_factory, client_factory, snap_date, store_name, bot_token, chat_id):
    from .etl_stock import run as etl_stock
    from .report_stock import build_stock_by_qty
    conn   = conn_factory()
    client = client_factory()
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM stock_snapshot WHERE day=%s", (snap_date,))
        if cur.fetchone()[0] == 0:
            tg.send_message(bot_token, chat_id,
                            f"⏳ Снимаю остатки на {snap_date.strftime('%d.%m.%Y')}…")
            etl_stock(client, conn, snap_date)
    return build_stock_by_qty(conn, snap_date, store_name)


def _run_stale(conn_factory, client_factory, snap_date, store_name, bot_token, chat_id):
    from .etl_stock import run as etl_stock
    from .report_stock import build_stock_report
    conn   = conn_factory()
    client = client_factory()
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM stock_snapshot WHERE day=%s", (snap_date,))
        if cur.fetchone()[0] == 0:
            tg.send_message(bot_token, chat_id,
                            f"⏳ Снимаю остатки на {snap_date.strftime('%d.%m.%Y')}…")
            etl_stock(client, conn, snap_date)
    return build_stock_report(conn, snap_date, store_name)


def _run_loss(conn_factory, client_factory, d_from, d_to, store_name, bot_token, chat_id):
    from .etl_loss import run as etl_loss
    from .report_loss import build_loss_report
    conn   = conn_factory()
    client = client_factory()
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM loss_doc WHERE day BETWEEN %s AND %s", (d_from, d_to))
        if cur.fetchone()[0] == 0:
            tg.send_message(bot_token, chat_id, "⏳ Подгружаю списания из МойСклад…")
            etl_loss(client, conn, d_from, d_to)
    return build_loss_report(conn, d_from, d_to, store_name)


# ─── прямые команды (без диалога) ────────────────────────────────────────────

def _dispatch_command(text, conn_factory, client_factory, bot_token, chat_id):
    parts = text.strip().split()
    cmd   = parts[0].lower().lstrip("/").split("@")[0]
    args  = parts[1:]

    if cmd in ("деньги", "ддс"):
        d_from, d_to = _parse_last_n(args)
        _exec_cashflow(conn_factory, client_factory, d_from, d_to, bot_token, chat_id)
    elif cmd == "закупки":
        d_from, d_to = _parse_last_n(args)
        _exec_supply(conn_factory, client_factory, d_from, d_to, bot_token, chat_id)
    elif cmd == "сотрудники":
        d_from, d_to = _parse_last_n(args)
        _exec_employees(client_factory, d_from, d_to, bot_token, chat_id)
    else:
        tg.send_message(bot_token, chat_id,
                        "Нажмите кнопку меню или отправьте /помощь.",
                        tg.main_reply_keyboard())


def _exec_cashflow(conn_factory, client_factory, d_from, d_to, bot_token, chat_id):
    from .etl_cashflow import run as etl_cashflow
    from .report_cashflow import build_cashflow_report
    conn   = conn_factory()
    client = client_factory()
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM cashflow_event WHERE day BETWEEN %s AND %s", (d_from, d_to))
        if cur.fetchone()[0] == 0:
            tg.send_message(bot_token, chat_id, "⏳ Подгружаю платежи…")
            etl_cashflow(client, conn, d_from, d_to)
    tg.send_message(bot_token, chat_id,
                    build_cashflow_report(conn, d_from, d_to), tg.main_reply_keyboard())


def _exec_supply(conn_factory, client_factory, d_from, d_to, bot_token, chat_id):
    from .etl_supply import run as etl_supply
    from .report_supply import build_supply_report
    conn   = conn_factory()
    client = client_factory()
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM supply_doc WHERE day BETWEEN %s AND %s", (d_from, d_to))
        if cur.fetchone()[0] == 0:
            tg.send_message(bot_token, chat_id, "⏳ Подгружаю поставки…")
            etl_supply(client, conn, d_from, d_to)
    tg.send_message(bot_token, chat_id,
                    build_supply_report(conn, d_from, d_to), tg.main_reply_keyboard())


def _exec_employees(client_factory, d_from, d_to, bot_token, chat_id):
    from .report_employees import build_employee_report
    client = client_factory()
    tg.send_message(bot_token, chat_id,
                    build_employee_report(client, d_from, d_to), tg.main_reply_keyboard())


# ─── вспомогательные ─────────────────────────────────────────────────────────

def _parse_period_button(norm: str) -> tuple[date, date] | None:
    today = date.today()
    code  = _PERIOD_BUTTONS.get(norm)
    if not code:
        return None
    if code == "today":
        return today, today
    if code == "yesterday":
        y = today - timedelta(days=1)
        return y, y
    if code == "month":
        return today.replace(day=1), today
    try:
        n = int(code)
        return today - timedelta(days=n - 1), today
    except ValueError:
        return None


def _looks_like_dates(text: str) -> bool:
    import re
    return bool(re.search(r"\d{4}-\d{2}-\d{2}", text))


def _try_parse_dates(text: str) -> tuple[date, date] | None:
    import re
    found = re.findall(r"\d{4}-\d{2}-\d{2}", text)
    try:
        if len(found) >= 2:
            d1 = datetime.strptime(found[0], "%Y-%m-%d").date()
            d2 = datetime.strptime(found[1], "%Y-%m-%d").date()
            return (min(d1, d2), max(d1, d2))
        if len(found) == 1:
            d = datetime.strptime(found[0], "%Y-%m-%d").date()
            return d, d
    except ValueError:
        pass
    return None


def _fmt_period(d_from: date | None, d_to: date | None) -> str:
    if d_from is None:
        return "не задан"
    if d_from == d_to:
        return d_from.strftime("%d.%m.%Y")
    return f"{d_from.strftime('%d.%m.%Y')} – {d_to.strftime('%d.%m.%Y')}"


def _parse_last_n(args: list[str], n: int = 30) -> tuple[date, date]:
    if not args:
        today = date.today()
        return today - timedelta(days=n - 1), today
    import re
    found = re.findall(r"\d{4}-\d{2}-\d{2}", " ".join(args))
    try:
        if len(found) >= 2:
            d1 = datetime.strptime(found[0], "%Y-%m-%d").date()
            d2 = datetime.strptime(found[1], "%Y-%m-%d").date()
            return min(d1, d2), max(d1, d2)
        if len(found) == 1:
            d = datetime.strptime(found[0], "%Y-%m-%d").date()
            return d, d
    except ValueError:
        pass
    today = date.today()
    return today - timedelta(days=n - 1), today


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
        f"&allowed_updates=%5B%22message%22%5D"
    )
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=timeout + 10) as resp:
        data = json.loads(resp.read())
    if not data.get("ok"):
        raise RuntimeError(f"getUpdates: {data}")
    return data.get("result", [])
