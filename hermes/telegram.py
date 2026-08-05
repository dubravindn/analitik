"""Отправка сообщений и работа с Bot API (только стандартная библиотека)."""
from __future__ import annotations

import json
import urllib.error
import urllib.request

# ─── отправка сообщений ───────────────────────────────────────────────────────

def send_message(
    bot_token: str,
    chat_id: str,
    text: str,
    reply_markup: dict | None = None,
    parse_mode: str | None = None,
) -> dict | None:
    """Отправить текст. Длинные сообщения режем на части по 4096 символов.
    reply_markup (inline_keyboard или keyboard) прикрепляется только к последней части.
    Возвращает result последнего запроса (или None при пустом тексте).
    """
    chunks = _split(text, 4096)
    result = None
    for i, chunk in enumerate(chunks):
        payload: dict = {"chat_id": chat_id, "text": chunk}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_markup and i == len(chunks) - 1:
            payload["reply_markup"] = reply_markup
        result = _post(bot_token, "sendMessage", payload)
    return result


def edit_message_text(
    bot_token: str,
    chat_id: str,
    message_id: int,
    text: str,
    reply_markup: dict | None = None,
) -> dict:
    """Редактировать существующее сообщение (для ответа на callback_query)."""
    payload: dict = {"chat_id": chat_id, "message_id": message_id, "text": text[:4096]}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return _post(bot_token, "editMessageText", payload)


def answer_callback_query(
    bot_token: str,
    callback_query_id: str,
    text: str = "",
    show_alert: bool = False,
) -> dict:
    """Закрыть «часики» после нажатия inline-кнопки."""
    return _post(bot_token, "answerCallbackQuery", {
        "callback_query_id": callback_query_id,
        "text": text[:200] if text else "",
        "show_alert": show_alert,
    })


# ─── клавиатуры ───────────────────────────────────────────────────────────────

def main_reply_keyboard() -> dict:
    """Постоянная клавиатура снизу экрана — главное меню."""
    return {
        "keyboard": [
            [{"text": "📊 Продажи"},    {"text": "📦 Остатки"}],
            [{"text": "🚨 Залежалые"},  {"text": "🗑 Списания"}],
            [{"text": "📥 Закупки"},    {"text": "💰 ДДС"}],
            [{"text": "👥 Сотрудники"}, {"text": "❓ Помощь"}],
        ],
        "resize_keyboard": True,
        "persistent": True,
        "is_persistent": True,
    }


def period_inline_keyboard(section: str) -> dict:
    """Inline-кнопки выбора периода, прикреплённые к сообщению."""
    if section == "sales":
        rows = [[
            {"text": "Сегодня",  "callback_data": "sales:today"},
            {"text": "Вчера",    "callback_data": "sales:yesterday"},
            {"text": "7 дней",   "callback_data": "sales:7"},
            {"text": "Месяц",    "callback_data": "sales:month"},
        ]]
    elif section == "stock":
        rows = [[
            {"text": "Сегодня",  "callback_data": "stock:today"},
            {"text": "Вчера",    "callback_data": "stock:yesterday"},
        ]]
    elif section == "stale":
        rows = [[
            {"text": "СРЕЗКА 3 / прочие 30",  "callback_data": "stale:3:30"},
            {"text": "СРЕЗКА 1 / прочие 14",  "callback_data": "stale:1:14"},
            {"text": "СРЕЗКА 7 / прочие 60",  "callback_data": "stale:7:60"},
        ]]
    elif section == "loss":
        rows = [[
            {"text": "7 дней",   "callback_data": "loss:7"},
            {"text": "30 дней",  "callback_data": "loss:30"},
            {"text": "Месяц",    "callback_data": "loss:month"},
        ]]
    elif section == "supply":
        rows = [[
            {"text": "7 дней",   "callback_data": "supply:7"},
            {"text": "30 дней",  "callback_data": "supply:30"},
            {"text": "Месяц",    "callback_data": "supply:month"},
        ]]
    elif section == "cashflow":
        rows = [[
            {"text": "7 дней",   "callback_data": "cashflow:7"},
            {"text": "30 дней",  "callback_data": "cashflow:30"},
            {"text": "Месяц",    "callback_data": "cashflow:month"},
        ]]
    elif section == "employees":
        rows = [[
            {"text": "7 дней",   "callback_data": "employees:7"},
            {"text": "30 дней",  "callback_data": "employees:30"},
            {"text": "Месяц",    "callback_data": "employees:month"},
        ]]
    else:
        rows = []
    return {"inline_keyboard": rows}


# ─── низкоуровневые утилиты ───────────────────────────────────────────────────

def _split(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    parts = []
    while text:
        parts.append(text[:limit])
        text = text[limit:]
    return parts


def _post(bot_token: str, method: str, payload: dict) -> dict:
    url = f"https://api.telegram.org/bot{bot_token}/{method}"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise RuntimeError(f"Telegram HTTP {e.code}: {body}") from e
    if not result.get("ok"):
        raise RuntimeError(f"Telegram API вернул ошибку: {result}")
    return result
