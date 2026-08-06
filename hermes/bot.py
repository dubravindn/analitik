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
from . import synclock
from . import telegram as tg

log = logging.getLogger("hermes.bot")

# ─── описание секций и их шагов диалога ──────────────────────────────────────

# Для каждой секции: список шагов ["period", "store"]
_DIALOG_STEPS: dict[str, list[str]] = {
    "sales":    ["period", "store"],
    "stock":    ["period", "store"],             # группа СРЕЗКА — всегда фиксирована
    "stale":    ["period", "store", "group"],
    "loss":     ["period", "store"],
    "reserves": ["period", "store"],
    "expenses": ["period", "store"],  # фильтр по складу через project_name
    "move":     ["period", "store"],  # перемещения между складами
    "audit":    ["period"],           # удалённые/изменённые документы из МойСклад
    "clients":  ["period", "store"], # клиентская аналитика (топ + отток)
    "forecast": [],                  # прогноз закупки — без диалога, запускается сразу
    "pdf":      ["period", "store"],
}

_SECTION_TITLE = {
    "sales":    "📊 Продажи",
    "stock":    "📦 Остатки",
    "stale":    "🚨 Залежалые",
    "loss":     "🗑 Списания",
    "reserves": "🎯 Резервы",
    "expenses": "💸 Расходы",
    "move":     "🔄 Перемещения",
    "audit":    "🔍 Изменения",
    "clients":  "👥 Клиенты",
    "forecast": "🛒 Прогноз",
    "pdf":      "📄 Отчёт PDF",
}

_BUTTON_TO_SECTION = {
    "📊 продажи":    "sales",
    "📦 остатки":    "stock",
    "🚨 залежалые":  "stale",
    "🗑 списания":   "loss",
    "🎯 резервы":    "reserves",
    "💸 расходы":    "expenses",
    "🔄 перемещения": "move",
    "🔍 изменения":  "audit",
    "👥 клиенты":    "clients",
    "🛒 прогноз":    "forecast",
    "📄 отчёт pdf":  "pdf",
    "❓ помощь":     "help",
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

Кнопки меню (все с выбором периода и склада):
  📊 Продажи    — выручка, прибыль, топ позиций
  📦 Остатки    — свободный остаток на дату
  🚨 Залежалые  — позиции без движения
  🗑 Списания   — все документы с позициями
  🎯 Резервы    — товары отложены под клиента
  💸 Расходы    — все платежи за период
  🔄 Перемещения — движение товара между складами
  📄 Отчёт PDF  — полный отчёт одним файлом

Клавиатуру можно скрыть стрелкой ↓ внизу
и вернуть касанием иконки клавиатуры.

Команды без меню:
  /деньги [д1] [д2]       — ДДС (сводка)
  /закупки [д1] [д2]      — поставки
  /сотрудники [д1] [д2]   — по сотрудникам
  /меню                   — показать клавиатуру\
"""

# ─── состояние диалога (in-memory, один пользователь) ────────────────────────

_dialog: dict[str, dict] = {}  # chat_id → state
_restarted: bool = False       # был ли рестарт бота (для сообщения о сбросе сессии)


def _is_dialog_button(norm: str) -> bool:
    """Текст — это sub-кнопка диалога (период/склад/группа), а не команда/раздел."""
    return (
        norm in _PERIOD_BUTTONS
        or norm in _STORE_BUTTONS
        or norm == "✏️ ввести период"
        or norm == "📦 все группы"
        or norm.startswith("📁 ")
    )


def _get_state(chat_id: str) -> dict | None:
    return _dialog.get(chat_id)


def _set_state(chat_id: str, section: str, step: str, params: dict) -> None:
    _dialog[chat_id] = {"section": section, "step": step, "params": params}


def _clear_state(chat_id: str) -> None:
    _dialog.pop(chat_id, None)


# ─── главный цикл ─────────────────────────────────────────────────────────────

def run(conn_factory, client_factory, bot_token: str, chat_id: str) -> None:
    log.info("Бот запущен (long-polling)")
    # При старте диалоги пусты (in-memory). Помечаем, что был рестарт — первое
    # «висячее» нажатие в несуществующий диалог получит понятный ответ.
    _dialog.clear()
    globals()["_restarted"] = True
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

    if norm in ("/меню", "меню", "/start"):
        _clear_state(chat_id)
        tg.send_message(bot_token, chat_id,
                        "📋 Меню восстановлено.", tg.main_reply_keyboard())
        return

    # ── Снять залипший флаг выгрузки вручную ──
    if norm in ("/unlock", "/разблокировать"):
        synclock.clear()
        tg.send_message(bot_token, chat_id,
                        "🔓 Флаг выгрузки снят. Отчёты доступны.", tg.main_reply_keyboard())
        return

    # ── Помощь ──
    if norm in ("❓ помощь", "/помощь", "/help", "помощь"):
        _clear_state(chat_id)
        tg.send_message(bot_token, chat_id, _HELP_TEXT, tg.main_reply_keyboard())
        return

    # ── Кнопка главного меню → начать диалог (или выполнить сразу) ──
    section = _BUTTON_TO_SECTION.get(norm)
    if section in _DIALOG_STEPS:
        _clear_state(chat_id)
        if not _DIALOG_STEPS[section]:
            # Секция без шагов — выполняем немедленно
            _execute(section, {}, chat_id, conn_factory, client_factory, bot_token)
        else:
            _start_dialog(section, chat_id, bot_token)
        return

    # ── Продолжение диалога ──
    state = _get_state(chat_id)
    if state:
        _continue_dialog(state, text, norm, chat_id, conn_factory, client_factory, bot_token)
        return

    # ── Висячее нажатие sub-кнопки без активного диалога (напр. бот перезапускался) ──
    if _is_dialog_button(norm):
        note = " (бот перезапускался)" if globals().get("_restarted") else ""
        globals()["_restarted"] = False
        tg.send_message(bot_token, chat_id,
                        f"⚠️ Сессия сброшена{note}. Начни заново — выбери раздел из меню.",
                        tg.main_reply_keyboard())
        return

    # ── Прямые команды (без диалога) ──
    _dispatch_command(text, conn_factory, client_factory, bot_token, chat_id)


# ─── диалог: начало ───────────────────────────────────────────────────────────

def _start_dialog(section: str, chat_id: str, bot_token: str) -> None:
    steps = _DIALOG_STEPS[section]
    first_step = steps[0]
    _set_state(chat_id, section, first_step, {})
    _ask_step(section, first_step, chat_id, bot_token, {})


def _ask_step(
    section: str, step: str, chat_id: str, bot_token: str,
    params: dict | None = None,
) -> None:
    title = _SECTION_TITLE[section]
    if step == "period":
        if section in ("stale", "stock", "reserves"):
            prompt = f"{title}\n\n📅 На какую дату показать снимок остатков?"
        elif section == "audit":
            prompt = f"{title}\n\n📅 За какой период?"
        elif section == "pdf":
            prompt = f"{title}\n\n📅 За какой период сформировать отчёт?"
        else:
            prompt = f"{title}\n\n📅 За какой период?"
        tg.send_message(bot_token, chat_id, prompt, tg.period_keyboard())

    elif step == "store":
        prompt = f"{title}\n\n📍 По какому складу?"
        tg.send_message(bot_token, chat_id, prompt, tg.store_keyboard(config.STORES))

    elif step == "group":
        groups = (params or {}).get("_groups", [])
        prompt = (
            f"{title}\n\n📁 По какой группе товаров?\n"
            f"(«Все группы» — без фильтра)"
        )
        tg.send_message(bot_token, chat_id, prompt, tg.group_keyboard(groups))


# ─── диалог: продолжение ─────────────────────────────────────────────────────

def _continue_dialog(state, text, norm, chat_id, conn_factory, client_factory, bot_token):
    section = state["section"]
    step    = state["step"]
    params  = state["params"]
    steps   = _DIALOG_STEPS[section]

    def _next_step_or_execute(current_step: str) -> None:
        idx = steps.index(current_step) + 1
        if idx < len(steps):
            next_step = steps[idx]
            # Перед шагом "group" — подгружаем список групп из базы
            if next_step == "group":
                try:
                    conn = conn_factory()
                    snap_date = params.get("d_to") or params.get("d_from")
                    from .report_stock import fetch_groups
                    params["_groups"] = fetch_groups(conn, snap_date) if snap_date else []
                except Exception:
                    params["_groups"] = []
            _set_state(chat_id, section, next_step, params)
            _ask_step(section, next_step, chat_id, bot_token, params)
        else:
            _clear_state(chat_id)
            _execute(section, params, chat_id, conn_factory, client_factory, bot_token)

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
        _next_step_or_execute("period")

    # ── Шаг: склад ──
    elif step == "store":
        if norm in _STORE_BUTTONS:
            params["store_name"] = _STORE_BUTTONS[norm]
            _next_step_or_execute("store")
        else:
            tg.send_message(bot_token, chat_id,
                            "Выберите склад из кнопок.",
                            tg.store_keyboard(config.STORES))

    # ── Шаг: группа товаров ──
    elif step == "group":
        groups = params.get("_groups") or []
        norm_map = {f"📁 {g}".lower(): g for g in groups}
        if norm == "📦 все группы":
            params["folder_group"] = None
            _clear_state(chat_id)
            _execute(section, params, chat_id, conn_factory, client_factory, bot_token)
        elif norm in norm_map:
            params["folder_group"] = norm_map[norm]
            _clear_state(chat_id)
            _execute(section, params, chat_id, conn_factory, client_factory, bot_token)
        else:
            tg.send_message(bot_token, chat_id,
                            "Выберите группу из кнопок.",
                            tg.group_keyboard(groups))


# ─── выполнение отчёта ────────────────────────────────────────────────────────

def _execute(section, params, chat_id, conn_factory, client_factory, bot_token):
    d_from       = params.get("d_from")
    d_to         = params.get("d_to")
    store_name   = params.get("store_name")    # None = все склады
    folder_group = params.get("folder_group")  # None = все группы

    # Идёт фоновая выгрузка? Тяжёлые секции (PDF, прогноз — контенция с синком)
    # не запускаем; лёгкие (продажи/остатки/клиенты — читают готовые таблицы)
    # пускаем с пометкой о возможной неполноте.
    sc = synclock.active()
    if sc:
        name, mins = sc
        if section in ("pdf", "forecast"):
            tg.send_message(
                bot_token, chat_id,
                f"⏳ Идёт перевыгрузка данных ({name}, уже ~{mins} мин). "
                f"«{section}» пока не собираю, чтобы не зависнуть — повтори чуть позже "
                f"(или /unlock, если синк точно завершён).",
                tg.main_reply_keyboard(),
            )
            return
        tg.send_message(
            bot_token, chat_id,
            f"⚠️ Данные могут быть неполными — идёт обновление ({name}, ~{mins} мин).",
        )

    # Для audit — нет фильтра по складу
    if section == "audit":
        tg.send_message(bot_token, chat_id,
                        f"⏳ Запрашиваю данные…\nПериод: {_fmt_period(d_from, d_to)}")
    elif section == "pdf":
        tg.send_message(bot_token, chat_id,
                        f"⏳ Генерирую PDF-отчёт…\n"
                        f"Период: {_fmt_period(d_from, d_to)}\n"
                        f"Склад: {store_name or 'Все склады'}")
    else:
        tg.send_message(bot_token, chat_id,
                        f"⏳ Готовлю отчёт…\n"
                        f"Период: {_fmt_period(d_from, d_to)}\n"
                        f"Склад: {store_name or 'Все склады'}")

    # PDF — отдельный путь: handler сам отправляет файл и клавиатуру
    if section == "pdf":
        _run_pdf(conn_factory, client_factory, d_from, d_to, store_name,
                 bot_token, chat_id)
        return

    try:
        if section == "sales":
            text = _run_sales(conn_factory, client_factory, d_from, d_to, store_name,
                              bot_token, chat_id)
        elif section == "stock":
            text = _run_stock(conn_factory, client_factory, d_from, store_name,
                              folder_group, bot_token, chat_id)
        elif section == "stale":
            text = _run_stale(conn_factory, client_factory, d_from, store_name,
                              folder_group, bot_token, chat_id)
        elif section == "loss":
            text = _run_loss(conn_factory, client_factory, d_from, d_to, store_name,
                             bot_token, chat_id)
        elif section == "reserves":
            text = _run_reserves(conn_factory, client_factory, d_from, store_name,
                                 bot_token, chat_id)
        elif section == "expenses":
            text = _run_expenses(conn_factory, client_factory, d_from, d_to, store_name,
                                 bot_token, chat_id)
        elif section == "move":
            text = _run_move(conn_factory, client_factory, d_from, d_to, store_name,
                             bot_token, chat_id)
        elif section == "clients":
            text = _run_clients(conn_factory, client_factory, d_from, d_to, store_name,
                                bot_token, chat_id)
        elif section == "forecast":
            text = _run_forecast(conn_factory, client_factory, bot_token, chat_id)
        elif section == "audit":
            text = _run_audit(client_factory, d_from, d_to, bot_token, chat_id)
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


def _run_stock(conn_factory, client_factory, snap_date, store_name, folder_group,
               bot_token, chat_id):
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
    return build_stock_by_qty(conn, snap_date, store_name, "СРЕЗКА")


def _run_stale(conn_factory, client_factory, snap_date, store_name, folder_group,
               bot_token, chat_id):
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
    return build_stock_report(conn, snap_date, store_name, folder_group)


def _run_reserves(conn_factory, client_factory, snap_date, store_name, bot_token, chat_id):
    from .etl_stock import run as etl_stock
    from .report_stock import build_reserve_report
    conn   = conn_factory()
    client = client_factory()
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM stock_snapshot WHERE day=%s", (snap_date,))
        if cur.fetchone()[0] == 0:
            tg.send_message(bot_token, chat_id,
                            f"⏳ Снимаю остатки на {snap_date.strftime('%d.%m.%Y')}…")
            etl_stock(client, conn, snap_date)
    return build_reserve_report(conn, snap_date, store_name)


def _run_forecast(conn_factory, client_factory, bot_token, chat_id):
    from .report_forecast import build_forecast_report
    conn   = conn_factory()
    client = client_factory()
    tg.send_message(bot_token, chat_id, "⏳ Запрашиваю заказы и остатки…")
    return build_forecast_report(client, conn)


def _run_clients(conn_factory, client_factory, d_from, d_to, store_name, bot_token, chat_id):
    from .etl_clients import run as etl_clients
    from .report_clients import build_clients_report
    conn   = conn_factory()
    client = client_factory()
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM sales_doc WHERE day BETWEEN %s AND %s", (d_from, d_to))
        if cur.fetchone()[0] == 0:
            tg.send_message(bot_token, chat_id, "⏳ Подгружаю отгрузки из МойСклад…")
            etl_clients(client, conn, d_from, d_to)
    return build_clients_report(conn, d_from, d_to, store_name)


def _run_audit(client_factory, d_from, d_to, bot_token, chat_id):
    from .report_audit import build_audit_report
    client = client_factory()
    return build_audit_report(client, d_from, d_to)


def _run_expenses(conn_factory, client_factory, d_from, d_to, store_name, bot_token, chat_id):
    from .etl_cashflow import run as etl_cashflow
    from .report_cashflow import build_expenses_report
    conn   = conn_factory()
    client = client_factory()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM cashflow_event WHERE day BETWEEN %s AND %s",
            (d_from, d_to),
        )
        if cur.fetchone()[0] == 0:
            tg.send_message(bot_token, chat_id, "⏳ Подгружаю платежи из МойСклад…")
            etl_cashflow(client, conn, d_from, d_to)
    return build_expenses_report(conn, d_from, d_to, store_name)


def _run_move(conn_factory, client_factory, d_from, d_to, store_name, bot_token, chat_id):
    from .etl_move import run as etl_move
    from .report_move import build_move_report
    conn   = conn_factory()
    client = client_factory()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM move_doc WHERE day BETWEEN %s AND %s",
            (d_from, d_to),
        )
        if cur.fetchone()[0] == 0:
            tg.send_message(bot_token, chat_id, "⏳ Подгружаю перемещения из МойСклад…")
            etl_move(client, conn, d_from, d_to)
    return build_move_report(conn, d_from, d_to, store_name)


def _run_pdf(conn_factory, client_factory, d_from, d_to, store_name, bot_token, chat_id):
    """Собираем PDF в отдельном потоке с таймаутом — чтобы бот не завис навсегда
    (сборка делает ленивую догрузку и может встать на контенции с синком)."""
    import threading
    done = threading.Event()

    def _worker():
        try:
            _build_and_send_pdf(conn_factory, client_factory,
                                d_from, d_to, store_name, bot_token, chat_id)
        finally:
            done.set()

    threading.Thread(target=_worker, daemon=True).start()
    if not done.wait(timeout=180):
        tg.send_message(
            bot_token, chat_id,
            "⏳ Сборка PDF заняла дольше 3 минут (возможно, идёт перевыгрузка данных). "
            "Если файл так и не пришёл — повтори позже.",
            tg.main_reply_keyboard(),
        )


def _build_and_send_pdf(conn_factory, client_factory, d_from, d_to, store_name, bot_token, chat_id):
    from .etl_sales import run as etl_sales
    from .etl_stock import run as etl_stock
    from .etl_loss import run as etl_loss
    from .etl_cashflow import run as etl_cashflow
    from .etl_clients import run as etl_clients
    from .etl_move import run as etl_move
    from .report_pdf import build_pdf
    try:
        conn   = conn_factory()
        client = client_factory()

        # Догружаем данные по каждой секции если нет
        missing = _missing_days(conn, d_from, d_to)
        if missing:
            tg.send_message(bot_token, chat_id,
                            f"⏳ Загружаю продажи за {len(missing)} дн.…")
            etl_sales(client, conn, min(missing), max(missing))

        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM stock_snapshot WHERE day=%s", (d_to,))
            if cur.fetchone()[0] == 0:
                tg.send_message(bot_token, chat_id, "⏳ Снимаю остатки…")
                etl_stock(client, conn, d_to)

        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM loss_doc WHERE day BETWEEN %s AND %s", (d_from, d_to)
            )
            if cur.fetchone()[0] == 0:
                tg.send_message(bot_token, chat_id, "⏳ Загружаю списания…")
                etl_loss(client, conn, d_from, d_to)

        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM cashflow_event WHERE day BETWEEN %s AND %s",
                (d_from, d_to),
            )
            if cur.fetchone()[0] == 0:
                tg.send_message(bot_token, chat_id, "⏳ Загружаю платежи…")
                etl_cashflow(client, conn, d_from, d_to)

        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM sales_doc WHERE day BETWEEN %s AND %s", (d_from, d_to)
            )
            if cur.fetchone()[0] == 0:
                tg.send_message(bot_token, chat_id, "⏳ Загружаю отгрузки (клиенты)…")
                etl_clients(client, conn, d_from, d_to)

        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM move_doc WHERE day BETWEEN %s AND %s", (d_from, d_to)
            )
            if cur.fetchone()[0] == 0:
                tg.send_message(bot_token, chat_id, "⏳ Загружаю перемещения…")
                etl_move(client, conn, d_from, d_to)

        tg.send_message(bot_token, chat_id, "📝 Формирую PDF…")
        pdf_bytes = build_pdf(conn, client, d_from, d_to, store_name)

        period_safe = f"{d_from.strftime('%Y%m%d')}-{d_to.strftime('%Y%m%d')}"
        filename    = f"hermes_{period_safe}.pdf"   # только ASCII, без кириллицы
        caption     = (
            f"Otchet {_fmt_period(d_from, d_to)}"
            + (f" | {store_name}" if store_name else "")
        )
        tg.send_document(bot_token, chat_id, pdf_bytes, filename, caption)
        tg.send_message(bot_token, chat_id, "✅ PDF готов.", tg.main_reply_keyboard())
    except Exception as e:
        log.exception("Ошибка PDF: %s", e)
        tg.send_message(bot_token, chat_id,
                        f"⚠️ Ошибка при генерации PDF: {e}", tg.main_reply_keyboard())


def _run_loss(conn_factory, client_factory, d_from, d_to, store_name, bot_token, chat_id):
    from .etl_loss import run as etl_loss
    from .etl_enter import run as etl_enter
    from .report_loss import build_loss_report
    conn   = conn_factory()
    client = client_factory()
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM loss_doc WHERE day BETWEEN %s AND %s", (d_from, d_to))
        loss_empty = cur.fetchone()[0] == 0
        # Оприходования (J2: «+»-сторона инвентаризации Базы) подгружаем вместе.
        cur.execute("SELECT COUNT(*) FROM enter_doc WHERE day BETWEEN %s AND %s", (d_from, d_to))
        enter_empty = cur.fetchone()[0] == 0
    if loss_empty or enter_empty:
        tg.send_message(bot_token, chat_id, "⏳ Подгружаю списания и оприходования из МойСклад…")
        if loss_empty:
            etl_loss(client, conn, d_from, d_to)
        if enter_empty:
            etl_enter(client, conn, d_from, d_to)
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
    today = config.msk_today()
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
        today = config.msk_today()
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
    today = config.msk_today()
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
