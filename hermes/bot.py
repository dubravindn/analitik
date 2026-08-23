"""Telegram-бот Hermes: пошаговые диалоги, фильтры по складу, скрытие меню.

Навигация:
  • Reply-keyboard    — главное меню (скрывается / восстанавливается)
  • Диалог            — период → склад → отчёт
  • Текстовые команды — /продажи, /деньги и т.д. для опытных пользователей
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import date, datetime, timedelta

from . import config
from . import synclock
from . import telegram as tg

log = logging.getLogger("hermes.bot")

# Тяжёлый управленческий PDF строится в фоне. Один активный отчёт на чат
# защищает сервер от повторных нажатий, не блокируя Telegram long-polling.
_PERIOD_REPORT_JOBS: set[str] = set()
_PERIOD_REPORT_JOBS_LOCK = threading.Lock()

# ─── описание секций и их шагов диалога ──────────────────────────────────────

# Для каждой секции: список шагов ["period", "store"]
_DIALOG_STEPS: dict[str, list[str]] = {
    # ── новые кнопки ──────────────────────────────────────────────────────────
    "period_report":   ["period"],   # 📊 Отчёт за период — один PDF по всем разделам
    "current_state":   [],           # 📦 Состояние на сегодня — без диалога, на дату запроса
    # ── legacy (rollback, маппинги работают, кнопки скрыты) ──────────────────
    "sales":    ["period", "store"],
    "stock":    ["period", "store"],
    "stale":    ["period", "store", "group"],
    "loss":     ["period", "store"],
    "reserves": ["period", "store"],
    "expenses": ["period", "store"],
    "move":     ["period", "store"],
    "audit":    ["period"],
    "clients":  ["period", "store"],
    "forecast": [],
    "prices":   [],
    "pdf":      ["period", "store"],
}

_SECTION_TITLE = {
    "period_report":   "📊 Отчёт за период",
    "current_state":   "📦 Состояние на сегодня",
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
    "prices":   "🏷 Цены",
    "pdf":      "📄 Отчёт PDF",
}

_BUTTON_TO_SECTION = {
    # ── новые кнопки ──────────────────────────────────────────────────────────
    "📊 отчёт за период":      "period_report",
    "📦 состояние на сегодня": "current_state",
    # ── legacy (rollback) ─────────────────────────────────────────────────────
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
    "🏷 цены":       "prices",
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
📋 Hermes — аналитика цветочной базы

Основные возможности:

📊 Отчёт за период
  Выбираешь период → получаешь PDF:
  продажи, списания, расходы, перемещения, клиенты, изменения.

📦 Состояние на сегодня
  Без выбора периода → PDF по каждому складу:
  прогноз закупки, остатки, залежалые, резервы.

🧠 Спросить ИИ
  Напишите вопрос обычным сообщением, например:
  «Почему снизилась прибыль за эту неделю?»
  «Какой склад просел и за счёт чего?»
  «Что с клиентами БАЗЫ без заказов?»

ИИ отвечает только по фактам из базы, указывает период и подтверждение.

Клавиатуру можно скрыть стрелкой ↓ внизу
и вернуть касанием иконки клавиатуры.

Команды без меню:
  /деньги [д1] [д2]       — ДДС (сводка)
  /закупки [д1] [д2]      — поставки
  /сотрудники [д1] [д2]   — по сотрудникам
  /меню                   — показать клавиатуру\
"""

_GROUP_MENU_TEXT = """\
🏠 ОБЩЕЕ МЕНЮ РУКОВОДИТЕЛЕЙ

Выберите раздел кнопкой ниже:

📊 Аналитика — отчёты, период, прогноз и вопросы по бизнесу.
👥 Работа сотрудников — смены, сотрудники, нарушения и выплаты.\
"""

_ANALYTICS_BUTTON = "📊 аналитика"
_EMPLOYEES_BUTTON = "👥 работа сотрудников"
_ROOT_MENU_BUTTONS = {"🏠 общее меню", "⬅️ общее меню"}
_ANALYTICS_QUESTION_BUTTON = "🧠 задать вопрос аналитику"

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


def _configured_chats(value: str) -> set[str]:
    return {item.strip() for item in str(value).split(",") if item.strip()}


def _leader_ids(allowed_chats: set[str]) -> set[str]:
    """Положительные ID в конфигурации — личные аккаунты руководителей."""
    return {item for item in allowed_chats if not item.startswith("-")}


def _command_name(text: str) -> str:
    """Имя команды без ``/`` и ``@bot``; пустая строка для обычного текста."""
    first = text.strip().split(maxsplit=1)[0] if text.strip() else ""
    if not first.startswith("/"):
        return ""
    return first[1:].split("@", 1)[0].casefold()


def _command_target(text: str) -> str:
    """Username из адресной команды ``/name@bot`` или пустая строка."""
    first = text.strip().split(maxsplit=1)[0] if text.strip() else ""
    if not first.startswith("/") or "@" not in first:
        return ""
    return first.split("@", 1)[1].casefold()


def _message_access(msg: dict, configured: str) -> tuple[bool, str, str, bool]:
    """(разрешено, chat_id, user_id, группа) для Telegram message."""
    chat = msg.get("chat") or {}
    chat_id = str(chat.get("id", ""))
    user_id = str((msg.get("from") or {}).get("id", ""))
    is_group = chat.get("type") in {"group", "supergroup"} or chat_id.startswith("-")
    allowed = _configured_chats(configured)
    if is_group:
        return chat_id in allowed and user_id in _leader_ids(allowed), chat_id, user_id, True
    return chat_id in allowed, chat_id, user_id or chat_id, False


def _is_reply_to_bot(msg: dict, username: str) -> bool:
    sender = ((msg.get("reply_to_message") or {}).get("from") or {})
    return str(sender.get("username") or "").casefold() == username.casefold()


def _main_keyboard(chat_id: str) -> dict:
    return (
        tg.analytics_group_keyboard()
        if str(chat_id).startswith("-")
        else tg.main_reply_keyboard()
    )


# ─── главный цикл ─────────────────────────────────────────────────────────────

def run(conn_factory, client_factory, bot_token: str, chat_id: str) -> None:
    log.info("Бот запущен (long-polling)")
    try:
        commands = [
            ("menu", "Общее меню двух ботов"),
            ("ask", "Задать вопрос бизнес-аналитику"),
            ("period", "Сформировать PDF за период"),
            ("today", "Состояние БАЗЫ на сегодня"),
            ("forecast", "Прогноз закупки"),
            ("help", "Помощь"),
        ]
        tg.set_my_commands(bot_token, commands)
        tg.set_my_commands(bot_token, commands, {"type": "all_group_chats"})
    except Exception as exc:
        log.warning("Не удалось обновить меню команд Telegram: %s", exc)
    try:
        from .ai_queue import start_result_monitor
        start_result_monitor(conn_factory, bot_token)
    except Exception as exc:
        # AI — необязательный слой. Его сбой не имеет права остановить PDF-бота.
        log.warning("AI result monitor не запущен: %s", exc)
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
    if "callback_query" in upd:
        callback = upd["callback_query"]
        actor_id = str(callback.get("from", {}).get("id", ""))
        callback_chat = str(
            ((callback.get("message") or {}).get("chat") or {}).get("id", actor_id)
        )
        allowed_chats = _configured_chats(chat_id)
        is_group = callback_chat.startswith("-")
        authorized = (
            callback_chat in allowed_chats and actor_id in _leader_ids(allowed_chats)
            if is_group else actor_id in allowed_chats
        )
        data = str(callback.get("data") or "")
        if authorized and data.startswith("ai_feedback:"):
            try:
                _prefix, run_id, value = data.split(":", 2)
                from .ai_queue import store_feedback
                conn = conn_factory()
                ok = store_feedback(conn, run_id, callback_chat, value)
                try:
                    conn.close()
                except Exception:
                    pass
                tg.answer_callback_query(
                    bot_token, str(callback.get("id") or ""),
                    "Спасибо, учту при настройке приоритетов." if ok else "Не удалось сохранить оценку.",
                )
            except Exception as exc:
                log.warning("AI feedback failed: %s", exc)
            return
    if "message" not in upd:
        return
    msg = upd["message"]
    text = (msg.get("text") or "").strip()
    authorized, target_chat, user_id, is_group = _message_access(msg, chat_id)
    if not authorized or not text:
        return

    # В группе ответы идут в общий чат, а пошаговый диалог изолирован по
    # руководителю. История AI при этом остаётся общей, потому что её ключ — chat_id.
    chat_id = target_chat
    state_key = f"{chat_id}:{user_id}" if is_group else chat_id

    log.info("Сообщение: %s", text[:80])
    norm = text.lower().strip()
    command = _command_name(text)
    if is_group:
        target_bot = _command_target(text)
        if target_bot and target_bot != "cbdanalitik_bot":
            return

    # ── Отмена диалога ──
    if norm in ("🚫 отмена", "/отмена", "отмена"):
        _clear_state(state_key)
        tg.send_message(bot_token, chat_id, "❌ Отменено.", _main_keyboard(chat_id))
        return

    if (
        norm == "меню" or norm in _ROOT_MENU_BUTTONS
        or command in ("menu", "меню", "start")
    ):
        _clear_state(state_key)
        if is_group:
            tg.send_message(
                bot_token, chat_id, _GROUP_MENU_TEXT, tg.shared_group_keyboard(),
            )
        else:
            tg.send_message(bot_token, chat_id,
                            "📋 Меню восстановлено.", tg.main_reply_keyboard())
        return

    if is_group and norm == _EMPLOYEES_BUTTON:
        # Эту кнопку обработает @CBDOt4et_bot. Аналитик молчит.
        return

    if is_group and norm == _ANALYTICS_BUTTON:
        _clear_state(state_key)
        tg.send_message(
            bot_token, chat_id,
            "📊 АНАЛИТИКА\n\nВыберите отчёт или задайте вопрос.",
            tg.analytics_group_keyboard(),
        )
        return

    # ── Снять залипший флаг выгрузки вручную ──
    if norm in ("/unlock", "/разблокировать"):
        synclock.clear()
        tg.send_message(bot_token, chat_id,
                        "🔓 Флаг выгрузки снят. Отчёты доступны.", _main_keyboard(chat_id))
        return

    # ── Помощь ──
    if norm in ("❓ помощь", "помощь") or command in ("помощь", "help"):
        _clear_state(state_key)
        tg.send_message(
            bot_token, chat_id,
            _GROUP_MENU_TEXT if is_group else _HELP_TEXT,
            tg.shared_group_keyboard() if is_group else tg.main_reply_keyboard(),
        )
        return

    if norm in ("🧠 спросить ии", "спросить ии", _ANALYTICS_QUESTION_BUTTON) or (
        command in ("спросить", "ask") and len(text.split(maxsplit=1)) == 1
    ):
        _clear_state(state_key)
        if is_group:
            _set_state(state_key, "ai_question", "question", {})
        tg.send_message(
            bot_token, chat_id,
            "🧠 Напишите вопрос обычным сообщением. Можно спрашивать о продажах, "
            "прибыли, расходах, списаниях, складах, товарах, клиентах, остатках, "
            "закупках и прогнозе.",
            tg.question_force_reply() if is_group else _main_keyboard(chat_id),
        )
        return

    if command == "period":
        _clear_state(state_key)
        _start_dialog("period_report", chat_id, bot_token, state_key)
        return
    if command == "today":
        _clear_state(state_key)
        _execute("current_state", {}, chat_id, conn_factory, client_factory, bot_token)
        return
    if command == "forecast":
        _clear_state(state_key)
        _execute("forecast", {}, chat_id, conn_factory, client_factory, bot_token)
        return
    if command in ("group_status", "bind_group") and is_group:
        tg.send_message(
            bot_token, chat_id,
            f"✅ Закрытая группа подключена к @CBDanalitik_bot. ID: {chat_id}\n"
            "Доступ есть только у двух разрешённых руководителей.",
        )
        return


    state = _get_state(state_key)
    if state and state.get("section") == "ai_question":
        _clear_state(state_key)
        _run_ai_question(conn_factory, text, bot_token, chat_id)
        return

    if is_group and not command and not state:
        allowed_button = norm in _BUTTON_TO_SECTION
        direct_reply = _is_reply_to_bot(msg, "CBDanalitik_bot")
        if not allowed_button and not direct_reply:
            # При выключенной Group Privacy оба бота видят общий чат. Всё, что
            # не адресовано аналитику, он обязан молча пропустить.
            return

    # ── Кнопка главного меню → начать диалог (или выполнить сразу) ──
    section = _BUTTON_TO_SECTION.get(norm)
    if section in _DIALOG_STEPS:
        _clear_state(state_key)
        if not _DIALOG_STEPS[section]:
            # Секция без шагов — выполняем немедленно
            _execute(section, {}, chat_id, conn_factory, client_factory, bot_token)
        else:
            _start_dialog(section, chat_id, bot_token, state_key)
        return

    # ── Продолжение диалога ──
    state = _get_state(state_key)
    if state:
        _continue_dialog(
            state, text, norm, chat_id, conn_factory, client_factory, bot_token,
            state_key,
        )
        return

    # ── Висячее нажатие sub-кнопки без активного диалога (напр. бот перезапускался) ──
    if _is_dialog_button(norm):
        note = " (бот перезапускался)" if globals().get("_restarted") else ""
        globals()["_restarted"] = False
        tg.send_message(bot_token, chat_id,
                        f"⚠️ Сессия сброшена{note}. Начни заново — выбери раздел из меню.",
                        _main_keyboard(chat_id))
        return

    # ── Прямые команды (без диалога) ──
    _dispatch_command(text, conn_factory, client_factory, bot_token, chat_id)


# ─── диалог: начало ───────────────────────────────────────────────────────────

def _start_dialog(
    section: str, chat_id: str, bot_token: str, state_key: str | None = None,
) -> None:
    steps = _DIALOG_STEPS[section]
    first_step = steps[0]
    _set_state(state_key or chat_id, section, first_step, {})
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

def _continue_dialog(
    state, text, norm, chat_id, conn_factory, client_factory, bot_token,
    state_key: str | None = None,
):
    state_key = state_key or chat_id
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
            _set_state(state_key, section, next_step, params)
            _ask_step(section, next_step, chat_id, bot_token, params)
        else:
            _clear_state(state_key)
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
            _clear_state(state_key)
            _execute(section, params, chat_id, conn_factory, client_factory, bot_token)
        elif norm in norm_map:
            params["folder_group"] = norm_map[norm]
            _clear_state(state_key)
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
        if section in ("pdf", "forecast", "period_report", "current_state"):
            tg.send_message(
                bot_token, chat_id,
                f"⏳ Идёт перевыгрузка данных ({name}, уже ~{mins} мин). "
                f"«{section}» пока не собираю, чтобы не зависнуть — повтори чуть позже "
                f"(или /unlock, если синк точно завершён).",
                _main_keyboard(chat_id),
            )
            return
        tg.send_message(
            bot_token, chat_id,
            f"⚠️ Данные могут быть неполными — идёт обновление ({name}, ~{mins} мин).",
        )

    # Новые секции отправляют свои сообщения сами — не дублировать
    if section in ("period_report", "current_state", "forecast"):
        pass
    elif section == "audit":
        tg.send_message(bot_token, chat_id,
                        f"⏳ Запрашиваю данные…\nПериод: {_fmt_period(d_from, d_to)}")
    elif section == "pdf":
        tg.send_message(bot_token, chat_id,
                        f"⏳ Генерирую PDF-отчёт…\n"
                        f"Период: {_fmt_period(d_from, d_to)}\n"
                        f"Склад: {store_name or 'Все склады'}")
    elif section == "prices":
        tg.send_message(bot_token, chat_id, "⏳ Проверяю качество цен…")
    else:
        tg.send_message(bot_token, chat_id,
                        f"⏳ Готовлю отчёт…\n"
                        f"Период: {_fmt_period(d_from, d_to)}\n"
                        f"Склад: {store_name or 'Все склады'}")

    # PDF-секции — handler сам отправляет файл и клавиатуру
    if section == "pdf":
        _run_pdf(conn_factory, client_factory, d_from, d_to, store_name,
                 bot_token, chat_id)
        return
    if section == "sales":
        _run_sales(conn_factory, client_factory, d_from, d_to, store_name,
                   bot_token, chat_id)
        return
    if section == "forecast":
        _run_forecast(conn_factory, client_factory, bot_token, chat_id)
        return
    if section == "period_report":
        _run_period_report(conn_factory, client_factory, d_from, d_to,
                           bot_token, chat_id)
        return
    if section == "current_state":
        _run_current_state(conn_factory, client_factory, bot_token, chat_id)
        return

    try:
        if section == "stock":
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
        elif section == "prices":
            text = _run_prices(conn_factory, bot_token, chat_id)
        elif section == "audit":
            text = _run_audit(client_factory, d_from, d_to, bot_token, chat_id)
        else:
            text = "Неизвестная секция."
    except Exception as e:
        log.exception("Ошибка при формировании отчёта %s: %s", section, e)
        text = f"⚠️ Ошибка при формировании отчёта: {e}"

    tg.send_message(bot_token, chat_id, text, _main_keyboard(chat_id))


# ─── выполнение конкретных секций ────────────────────────────────────────────

def _run_sales(conn_factory, client_factory, d_from, d_to, store_name, bot_token, chat_id):
    from .etl_sales import run as etl_sales
    from .report_sales_pdf import build_sales_pdf
    conn   = conn_factory()
    client = client_factory()
    missing = _missing_days(conn, d_from, d_to)
    if missing:
        tg.send_message(bot_token, chat_id,
                        f"⏳ Подгружаю {len(missing)} дн. из МойСклад…")
        etl_sales(client, conn, min(missing), max(missing))
    try:
        pdf_bytes = build_sales_pdf(conn, d_from, d_to, store_name)
        period_safe = f"{d_from.strftime('%Y%m%d')}-{d_to.strftime('%Y%m%d')}"
        filename = f"hermes_sales_{period_safe}.pdf"
        caption  = "Продажи " + _fmt_period(d_from, d_to)
        if store_name:
            caption += f" | {store_name}"
        tg.send_document(bot_token, chat_id, pdf_bytes, filename, caption)
        tg.send_message(bot_token, chat_id, "✅ PDF готов.", _main_keyboard(chat_id))
    except Exception as e:
        log.exception("Ошибка PDF продаж: %s", e)
        tg.send_message(bot_token, chat_id,
                        f"⚠️ Ошибка при генерации PDF: {e}", _main_keyboard(chat_id))


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


def _queue_ai_background(conn_factory, chat_id: str, payload_builder, report_type: str) -> None:
    """Собрать факты после PDF и поставить AI-задачу, не блокируя Telegram."""
    import threading

    def _worker() -> None:
        conn = None
        try:
            from .ai_queue import enqueue_analysis, mode
            if mode() == "off":
                return
            conn = conn_factory()
            payload = payload_builder(conn)
            enqueue_analysis(conn, payload, chat_id)
        except Exception as exc:
            # Основной документ уже отправлен; AI не влияет на его корректность.
            log.exception("AI enqueue %s failed: %s", report_type, exc)
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    threading.Thread(
        target=_worker, name=f"hermes-ai-enqueue-{report_type}", daemon=True,
    ).start()


def _run_forecast(conn_factory, client_factory, bot_token, chat_id):
    import datetime as _dt
    from .calc_forecast import (
        build_forecast_new, build_forecast, summarize,
        FORECAST_ENGINE, SREZKA_STORE_CONFIGS,
    )
    from .report_forecast_pdf import build_forecast_pdf
    from .charts import chart_forecast_summary, chart_forecast_history

    conn   = conn_factory()
    client = client_factory()
    token  = config.MOYSKLAD_TOKEN()

    tg.send_message(bot_token, chat_id, "⏳ Считаю прогноз закупки…")

    today      = _dt.date.today()
    monday     = today - _dt.timedelta(days=today.weekday())
    sunday     = monday + _dt.timedelta(days=6)
    next_mon   = monday + _dt.timedelta(days=7)
    next_sun   = next_mon + _dt.timedelta(days=6)

    # ── запуск NEW engine ───────────────────────────────────────────────────────
    results_new = build_forecast_new(
        conn, token, SREZKA_STORE_CONFIGS,
        cutoff_date=today,
        horizon_from=next_mon,
        horizon_to=next_sun,
    )

    # ── OLD engine — только для subgroup / prev_demand ──────────────────────────
    old_rows = build_forecast(conn, token, SREZKA_STORE_CONFIGS,
                              cutoff_date=today,
                              horizon_from=next_mon,
                              horizon_to=next_sun)
    subgroups_by_pid = {r.product_id: (r.subgroup or "Другое") for r in old_rows}

    # ── краткая сводка ──────────────────────────────────────────────────────────
    total_order  = sum(r.recommended_order_qty or 0 for r in results_new)
    total_sku    = len({r.product_id for r in results_new if (r.recommended_order_qty or 0) > 0})
    zero_stock   = sum(1 for r in results_new if (r.available_stock or 0) <= 0)
    period_label = f"{next_mon.strftime('%d.%m')}–{next_sun.strftime('%d.%m.%Y')}"

    summary_lines = [
        f"📦 *Прогноз закупки СРЕЗКИ* — {period_label}",
        f"",
        f"К заказу: *{int(total_order)} шт.* ({total_sku} SKU)",
        f"Нулевой остаток: {zero_stock} позиций",
    ]
    if results_new:
        top5 = sorted(
            [(r.product_name, r.recommended_order_qty or 0) for r in results_new
             if (r.recommended_order_qty or 0) > 0],
            key=lambda x: x[1], reverse=True,
        )[:5]
        if top5:
            summary_lines.append("")
            summary_lines.append("Топ-5 к заказу:")
            for name, qty in top5:
                summary_lines.append(f"  • {name[:35]} — {int(qty)} шт.")

    tg.send_message(bot_token, chat_id, "\n".join(summary_lines))

    # ── сохранить в БД ──────────────────────────────────────────────────────────
    _save_forecast_run(conn, results_new, next_mon, next_sun)

    # ── график — обзор ──────────────────────────────────────────────────────────
    png_summary = chart_forecast_summary(results_new, subgroups_by_pid)
    if png_summary:
        tg.send_photo(bot_token, chat_id, png_summary,
                      caption=f"Обзор прогноза {period_label}")

    # ── график — история ────────────────────────────────────────────────────────
    history = _load_forecast_history(conn)
    if history:
        png_hist = chart_forecast_history(history)
        if png_hist:
            tg.send_photo(bot_token, chat_id, png_hist,
                          caption="Динамика прогноза по неделям")

    # ── PDF ─────────────────────────────────────────────────────────────────────
    try:
        pdf_bytes = build_forecast_pdf(conn, next_mon, next_sun, token=token)
        period_safe = f"{next_mon.strftime('%Y%m%d')}-{next_sun.strftime('%Y%m%d')}"
        tg.send_document(bot_token, chat_id, pdf_bytes,
                         f"forecast_{period_safe}.pdf",
                         f"Прогноз закупки {period_label}")
        _queue_ai_background(
            conn_factory, chat_id,
            lambda _conn: __import__(
                "hermes.analysis_payload", fromlist=["build_forecast_analysis_payload"]
            ).build_forecast_analysis_payload(results_new, next_mon, next_sun),
            "forecast",
        )
    except Exception as e:
        log.exception("Ошибка PDF прогноза: %s", e)
        tg.send_message(bot_token, chat_id, f"⚠️ PDF не сгенерирован: {e}")

    tg.send_message(bot_token, chat_id, "✅ Прогноз готов.", _main_keyboard(chat_id))


def _run_period_pdf_only(conn_factory, client_factory, d_from, d_to, bot_token, chat_id):
    """Запустить сборку PDF в фоне, сохранив отзывчивость Telegram-бота."""
    job_key = str(chat_id)
    with _PERIOD_REPORT_JOBS_LOCK:
        if job_key in _PERIOD_REPORT_JOBS:
            tg.send_message(
                bot_token, chat_id,
                "⏳ Отчёт за период уже формируется. Бот продолжает работать; "
                "готовый PDF придёт сюда автоматически.",
                _main_keyboard(chat_id),
            )
            return
        _PERIOD_REPORT_JOBS.add(job_key)

    tg.send_message(
        bot_token, chat_id,
        f"⏳ Формирую PDF в фоне за {_fmt_period(d_from, d_to)}. "
        "Можно продолжать пользоваться ботом — файл придёт автоматически.",
        _main_keyboard(chat_id),
    )

    def _worker():
        conn = None
        try:
            from .etl_sales import run as etl_sales
            from .etl_stock import run as etl_stock
            from .etl_loss import run as etl_loss
            from .etl_enter import run as etl_enter
            from .etl_cashflow import run as etl_cashflow
            from .etl_clients import run as etl_clients
            from .etl_move import run as etl_move
            from .report_sales_pdf import build_sales_pdf

            conn = conn_factory()
            client = client_factory()
            # Выбранный период всегда обновляем полностью. Проверка «есть хотя
            # бы одна строка» оставляла старый снимок, если после выгрузки
            # добавляли/меняли отгрузки, перемещения или платежи.
            etl_sales(client, conn, d_from, d_to)
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM stock_snapshot WHERE day=%s", (d_to,))
                if cur.fetchone()[0] == 0:
                    etl_stock(client, conn, d_to)
            etl_loss(client, conn, d_from, d_to)
            etl_enter(client, conn, d_from, d_to)
            etl_cashflow(client, conn, d_from, d_to)
            etl_clients(client, conn, d_from, d_to)
            etl_move(client, conn, d_from, d_to)

            # Данны для четырёх сравнений PDF: день, неделя, месяц, год к году.
            import calendar
            from datetime import timedelta

            month_start = d_to.replace(day=1)
            previous_month_last = month_start - timedelta(days=1)
            previous_month_start = previous_month_last.replace(day=1)
            previous_month_to = previous_month_start.replace(
                day=min(d_to.day, previous_month_last.day),
            )

            def _year_back(value):
                day = min(value.day, calendar.monthrange(value.year - 1, value.month)[1])
                return value.replace(year=value.year - 1, day=day)

            comparison_windows = [
                (d_to - timedelta(days=13), d_to),
                (month_start, d_to),
                (previous_month_start, previous_month_to),
                (_year_back(d_from), _year_back(d_to)),
            ]
            for cmp_from, cmp_to in comparison_windows:
                missing_cmp = _missing_days(conn, cmp_from, cmp_to)
                if missing_cmp:
                    etl_sales(client, conn, min(missing_cmp), max(missing_cmp))
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT COUNT(*) FROM loss_doc WHERE day BETWEEN %s AND %s",
                        (cmp_from, cmp_to),
                    )
                    if cur.fetchone()[0] == 0:
                        etl_loss(client, conn, cmp_from, cmp_to)
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT COUNT(*) FROM cashflow_event WHERE day BETWEEN %s AND %s",
                        (cmp_from, cmp_to),
                    )
                    if cur.fetchone()[0] == 0:
                        etl_cashflow(client, conn, cmp_from, cmp_to)
            pdf_bytes = build_sales_pdf(
                conn, d_from, d_to, None, include_management_sections=True,
                client=client,
            )
            period_safe = f"{d_from.strftime('%Y%m%d')}-{d_to.strftime('%Y%m%d')}"
            tg.send_document(
                bot_token, chat_id, pdf_bytes,
                f"period_report_{period_safe}.pdf",
                f"Отчёт за {_fmt_period(d_from, d_to)}",
            )
            _queue_ai_background(
                conn_factory, chat_id,
                lambda analysis_conn: __import__(
                    "hermes.analysis_payload",
                    fromlist=["build_period_analysis_payload"],
                ).build_period_analysis_payload(
                    analysis_conn, d_from, d_to, client_factory(),
                ),
                "period",
            )
            tg.send_message(
                bot_token, chat_id, "✅ Отчёт за период готов.",
                _main_keyboard(chat_id),
            )
        except Exception as exc:
            log.error("Ошибка PDF за период: %s", exc, exc_info=exc)
            tg.send_message(
                bot_token, chat_id, f"⚠️ Ошибка PDF: {exc}",
                _main_keyboard(chat_id),
            )
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            with _PERIOD_REPORT_JOBS_LOCK:
                _PERIOD_REPORT_JOBS.discard(job_key)

    threading.Thread(
        target=_worker,
        name=f"hermes-period-report-{job_key}",
        daemon=True,
    ).start()


def _run_current_state_pdf_only(conn_factory, bot_token, chat_id):
    """Build and send one current-state PDF without progress texts or charts."""
    import datetime as _dt
    import threading
    from .report_current_state import build_current_state_pdf, _latest_day

    done = threading.Event()
    result = [None]
    error = [None]
    snap_day = [_dt.date.today()]

    def _worker():
        try:
            conn = conn_factory()
            snap_day[0] = _latest_day(conn)
            result[0] = build_current_state_pdf(conn, config.MOYSKLAD_TOKEN())
        except Exception as exc:
            error[0] = exc
        finally:
            done.set()

    threading.Thread(target=_worker, daemon=True).start()
    if not done.wait(timeout=900):
        tg.send_message(bot_token, chat_id, "⚠️ PDF не удалось собрать за 15 минут.")
        return
    if error[0]:
        log.error("Ошибка PDF состояния: %s", error[0], exc_info=error[0])
        tg.send_message(bot_token, chat_id, f"⚠️ Ошибка PDF: {error[0]}")
        return
    tg.send_document(
        bot_token, chat_id, result[0],
        f"current_state_{snap_day[0].strftime('%Y%m%d')}.pdf",
        f"Состояние на {snap_day[0].strftime('%d.%m.%Y')}",
    )
    _queue_ai_background(
        conn_factory, chat_id,
        lambda conn: __import__(
            "hermes.analysis_payload", fromlist=["build_current_state_analysis_payload"]
        ).build_current_state_analysis_payload(conn, config.MOYSKLAD_TOKEN()),
        "current_state",
    )


def _run_period_report(conn_factory, client_factory, d_from, d_to, bot_token, chat_id):
    """📊 Отчёт за период — только PDF."""
    _run_period_pdf_only(conn_factory, client_factory, d_from, d_to, bot_token, chat_id)
    return
    import datetime as _dt
    from .report_pdf import _pdf_summary
    from .charts import chart_period_summary

    tg.send_message(
        bot_token, chat_id,
        f"⏳ Формирую управленческий отчёт…\nПериод: {_fmt_period(d_from, d_to)}",
    )

    conn       = conn_factory()
    period_len = (d_to - d_from).days + 1
    prev_to    = d_from - _dt.timedelta(days=1)
    prev_from  = prev_to - _dt.timedelta(days=period_len - 1)

    s = sp = None
    try:
        s  = _pdf_summary(conn, d_from,    d_to,    None)
        sp = _pdf_summary(conn, prev_from, prev_to, None)
    except Exception as _e:
        log.warning("period_report _pdf_summary failed: %s", _e)

    # ── Краткое текстовое сообщение ───────────────────────────────────────────
    if s:
        def _rr(kop):
            return f"{int(kop or 0) // 100:,}".replace(",", " ") + " ₽"

        def _dd(cur, prv):
            try:
                return f" ({(cur - prv) / abs(prv) * 100:+.0f}%)"
            except (ZeroDivisionError, TypeError):
                return ""

        profit_before = (s["profit"] or 0) - (s["op_expenses"] or 0)
        sp_profit_before = ((sp["profit"] or 0) - (sp["op_expenses"] or 0)) if sp else 0

        lines = [
            f"📊 *{_fmt_period(d_from, d_to)}*",
            f"_{_fmt_period(prev_from, prev_to)} — сравнение_",
            "",
            f"Выручка:                {_rr(s['rev'])}{_dd(s['rev'], sp['rev'] if sp else 0)}",
            f"Валовая прибыль:        {_rr(s['profit'])}{_dd(s['profit'], sp['profit'] if sp else 0)}",
            f"Прибыль до списаний:    {_rr(profit_before)}{_dd(profit_before, sp_profit_before)}",
            f"Прибыль после списаний: {_rr(s['result'])}{_dd(s['result'], sp['result'] if sp else 0)}",
            f"Списания:               {_rr(s['loss'])}",
            f"Расходы:                {_rr(s['op_expenses'])}",
            f"Чеков: {s['checks']}  ·  ср. {_rr(s['avg_check'])}",
            "",
            "График и полный отчёт ниже.",
        ]
        tg.send_message(bot_token, chat_id, "\n".join(lines))

    # ── График ────────────────────────────────────────────────────────────────
    if s and sp:
        try:
            png = chart_period_summary(
                s, sp,
                label_curr=_fmt_period(d_from, d_to),
                label_prev=_fmt_period(prev_from, prev_to),
            )
            if png:
                tg.send_photo(bot_token, chat_id, png,
                              caption=f"Сравнение: {_fmt_period(d_from, d_to)} vs {_fmt_period(prev_from, prev_to)}")
        except Exception as _e:
            log.warning("chart_period_summary failed: %s", _e)

    _run_pdf(conn_factory, client_factory, d_from, d_to, None, bot_token, chat_id)


def _run_current_state(conn_factory, client_factory, bot_token, chat_id):
    """📦 Состояние на сегодня — только PDF."""
    _run_current_state_pdf_only(conn_factory, bot_token, chat_id)
    return
    import threading
    import datetime as _dt
    from .report_current_state import (
        build_current_state_pdf, _latest_day, _stores_with_data,
    )
    from .calc_forecast import build_forecast_new, SREZKA_STORE_CONFIGS
    from .charts import chart_forecast_summary

    tg.send_message(bot_token, chat_id, "⏳ Формирую состояние на сегодня…")

    conn  = conn_factory()
    token = config.MOYSKLAD_TOKEN()
    today = _dt.date.today()

    # Прогноз для краткого текста + график
    try:
        monday   = today - _dt.timedelta(days=today.weekday())
        next_mon = monday   + _dt.timedelta(days=7)
        next_sun = next_mon + _dt.timedelta(days=6)
        fc_results = build_forecast_new(
            conn, token, SREZKA_STORE_CONFIGS,
            cutoff_date=today,
            horizon_from=next_mon,
            horizon_to=next_sun,
        )
        order_sku  = len({r.product_id for r in fc_results if (r.recommended_order_qty or 0) > 0})
        order_qty  = int(sum(r.recommended_order_qty or 0 for r in fc_results))
        zero_stock = sum(1 for r in fc_results if (r.available_stock or 0) <= 0)
    except Exception as _e:
        log.warning("forecast for current_state failed: %s", _e)
        fc_results = []
        order_sku = order_qty = zero_stock = 0

    try:
        snap_day   = _latest_day(conn)
        stores_str = ", ".join(_stores_with_data(conn, snap_day)) or "нет данных"
    except Exception:
        snap_day   = today
        stores_str = "данные запрашиваются"

    tg.send_message(bot_token, chat_id, "\n".join([
        f"📦 *Состояние на {snap_day.strftime('%d.%m.%Y')}*",
        "",
        f"Склады: {stores_str}",
        f"Нулевой остаток: {zero_stock} позиций",
        f"К заказу: {order_sku} SKU / {order_qty} шт.",
        "",
        "Полный отчёт по складам формируется…",
    ]))

    # График прогноза
    if fc_results:
        try:
            png = chart_forecast_summary(fc_results, {})
            if png:
                tg.send_photo(
                    bot_token, chat_id, png,
                    caption=(f"Прогноз закупки {next_mon.strftime('%d.%m')}–"
                             f"{next_sun.strftime('%d.%m.%Y')}"),
                )
        except Exception as _e:
            log.warning("chart_forecast_summary for current_state failed: %s", _e)

    # PDF — тяжёлая операция, до 2–3 мин
    done       = threading.Event()
    pdf_result = [None]
    pdf_error  = [None]

    def _worker():
        try:
            pdf_result[0] = build_current_state_pdf(conn, token)
        except Exception as exc:
            pdf_error[0]  = exc
        finally:
            done.set()

    threading.Thread(target=_worker, daemon=True).start()
    if not done.wait(timeout=360):
        tg.send_message(
            bot_token, chat_id,
            "⏳ PDF формируется дольше 6 минут, попробуй позже.",
            _main_keyboard(chat_id),
        )
        return

    if pdf_error[0]:
        log.exception("build_current_state_pdf: %s", pdf_error[0])
        tg.send_message(
            bot_token, chat_id,
            f"⚠️ Ошибка PDF: {pdf_error[0]}",
            _main_keyboard(chat_id),
        )
        return

    tg.send_document(
        bot_token, chat_id,
        pdf_result[0],
        f"current_state_{snap_day.strftime('%Y%m%d')}.pdf",
        f"Состояние на {snap_day.strftime('%d.%m.%Y')}",
    )
    tg.send_message(bot_token, chat_id, "✅ Готово.", _main_keyboard(chat_id))


def _save_forecast_run(conn, results, horizon_from, horizon_to):
    """Сохраняет итоги прогностического запуска в таблицу forecast_run."""
    if not results:
        return
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS forecast_run (
                id             SERIAL PRIMARY KEY,
                run_at         TIMESTAMPTZ DEFAULT NOW(),
                horizon_from   DATE NOT NULL,
                horizon_to     DATE NOT NULL,
                total_order    NUMERIC,
                total_demand   NUMERIC,
                total_stock    NUMERIC,
                sku_count      INT
            )
        """)
        total_order  = sum(r.recommended_order_qty or 0 for r in results)
        total_demand = sum(r.expected_demand or 0 for r in results)
        total_stock  = sum(max(r.available_stock or 0, 0) for r in results)
        sku_count    = len({r.product_id for r in results})
        cur.execute(
            """
            INSERT INTO forecast_run
              (horizon_from, horizon_to, total_order, total_demand, total_stock, sku_count)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (horizon_from, horizon_to,
             total_order, total_demand, total_stock, sku_count),
        )
    conn.commit()


def _load_forecast_history(conn):
    """Возвращает последние 8 запусков из forecast_run (хронологически)."""
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT horizon_from, total_order, total_demand, total_stock
                FROM forecast_run
                ORDER BY horizon_from DESC
                LIMIT 8
            """)
            rows = cur.fetchall()
        return list(reversed(rows)) if rows and len(rows) >= 2 else []
    except Exception:
        return []


def _run_prices(conn_factory, bot_token, chat_id):
    from .report_prices import build_price_quality_report
    return build_price_quality_report(conn_factory())


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
            _main_keyboard(chat_id),
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

        # Графики как фото после PDF (пропускаем если matplotlib не установлен)
        try:
            from . import charts as _charts
            if _charts._MPL_OK:
                _chart_specs = [
                    (
                        _charts.chart_revenue_by_day,
                        (conn, d_from, d_to, store_name),
                        "График: выручка и прибыль от продаж по дням",
                    ),
                    (
                        _charts.chart_stores_compare,
                        (conn, d_from, d_to),
                        "График: сравнение складов",
                    ),
                    (
                        _charts.chart_losses_vs_revenue,
                        (conn, d_from, d_to, store_name),
                        "График: выручка и списания по дням",
                    ),
                ]
                for _fn, _args, _cap in _chart_specs:
                    try:
                        _png = _fn(*_args)
                        if _png:
                            tg.send_photo(bot_token, chat_id, _png, _cap)
                    except Exception as _ex:
                        log.warning("Не удалось отправить график '%s': %s", _cap, _ex)
        except Exception as _ex:
            log.warning("Ошибка импорта charts: %s", _ex)

        tg.send_message(bot_token, chat_id, "✅ PDF готов.", _main_keyboard(chat_id))
    except Exception as e:
        log.exception("Ошибка PDF: %s", e)
        tg.send_message(bot_token, chat_id,
                        f"⚠️ Ошибка при генерации PDF: {e}", _main_keyboard(chat_id))


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
        question = text.strip()
        if cmd in ("ask", "спросить"):
            question = " ".join(args).strip()
        if question:
            _run_ai_question(conn_factory, question, bot_token, chat_id)
        else:
            tg.send_message(bot_token, chat_id,
                            "Напишите вопрос после команды или обычным сообщением.",
                            _main_keyboard(chat_id))


def _run_ai_question(conn_factory, question: str, bot_token: str, chat_id: str) -> None:
    """Build a read-only fact pack and enqueue one conversational AI answer."""
    import threading

    from .ai_queue import enqueue_analysis, mode

    if mode() != "live":
        tg.send_message(
            bot_token, chat_id,
            "⚠️ Диалог с ИИ сейчас выключен. PDF-отчёты продолжают работать.",
            _main_keyboard(chat_id),
        )
        return

    conn = conn_factory()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*) FROM ai_analysis_run
                WHERE report_type='question' AND chat_id=%s AND status='queued'
                  AND created_at > now() - interval '7 minutes'
                """,
                (str(chat_id),),
            )
            pending = int((cur.fetchone() or (0,))[0])
    finally:
        conn.close()
    if pending:
        tg.send_message(bot_token, chat_id, "⏳ Предыдущий вопрос ещё анализируется.")
        return

    tg.send_message(
        bot_token, chat_id,
        "🧠 Проверяю цифры и источники. Обычно ответ занимает до минуты.",
        _main_keyboard(chat_id),
    )

    def _worker() -> None:
        work_conn = None
        try:
            from .ai_question import build_question_analysis_payload
            work_conn = conn_factory()
            payload = build_question_analysis_payload(work_conn, question, chat_id)
            run_id = enqueue_analysis(work_conn, payload, chat_id, force_mode="live")
            log.info("AI question queued: %s chat=%s", run_id, chat_id)
        except Exception as exc:
            log.exception("AI question enqueue failed: %s", exc)
            try:
                tg.send_message(
                    bot_token, chat_id,
                    "⚠️ Не удалось подготовить данные для ответа. Попробуйте ещё раз чуть позже.",
                    _main_keyboard(chat_id),
                )
            except Exception:
                pass
        finally:
            if work_conn is not None:
                try:
                    work_conn.close()
                except Exception:
                    pass

    threading.Thread(
        target=_worker, name="hermes-ai-question", daemon=True,
    ).start()


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
                    build_cashflow_report(conn, d_from, d_to), _main_keyboard(chat_id))


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
                    build_supply_report(conn, d_from, d_to), _main_keyboard(chat_id))


def _exec_employees(client_factory, d_from, d_to, bot_token, chat_id):
    from .report_employees import build_employee_report
    client = client_factory()
    tg.send_message(bot_token, chat_id,
                    build_employee_report(client, d_from, d_to), _main_keyboard(chat_id))


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
        f"&allowed_updates=%5B%22message%22%2C%22callback_query%22%5D"
    )
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=timeout + 10) as resp:
        data = json.loads(resp.read())
    if not data.get("ok"):
        raise RuntimeError(f"getUpdates: {data}")
    return data.get("result", [])
