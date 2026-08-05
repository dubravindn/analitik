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
) -> dict | None:
    """Отправить текст. Длинные сообщения режем на части по 4096 символов.
    reply_markup прикрепляется только к последней части.
    """
    chunks = _split(text, 4096)
    result = None
    for i, chunk in enumerate(chunks):
        payload: dict = {"chat_id": chat_id, "text": chunk}
        if reply_markup and i == len(chunks) - 1:
            payload["reply_markup"] = reply_markup
        result = _post(bot_token, "sendMessage", payload)
    return result


def answer_callback_query(
    bot_token: str,
    callback_query_id: str,
    text: str = "",
    show_alert: bool = False,
) -> dict:
    return _post(bot_token, "answerCallbackQuery", {
        "callback_query_id": callback_query_id,
        "text": text[:200] if text else "",
        "show_alert": show_alert,
    })


# ─── клавиатуры ───────────────────────────────────────────────────────────────

def main_reply_keyboard() -> dict:
    """Главное меню — постоянная клавиатура снизу."""
    return {
        "keyboard": [
            [{"text": "📊 Продажи"},   {"text": "📦 Остатки"}],
            [{"text": "🚨 Залежалые"}, {"text": "🗑 Списания"}],
            [{"text": "❓ Помощь"},    {"text": "🙈 Скрыть меню"}],
        ],
        "resize_keyboard": True,
        "is_persistent": True,
    }


def remove_keyboard() -> dict:
    """Убрать клавиатуру снизу экрана."""
    return {"remove_keyboard": True}


def period_keyboard() -> dict:
    """Клавиатура выбора периода."""
    return {
        "keyboard": [
            [{"text": "📅 Сегодня"},        {"text": "📅 Вчера"}],
            [{"text": "📅 7 дней"},          {"text": "📅 14 дней"}],
            [{"text": "📅 30 дней"},         {"text": "📅 Текущий месяц"}],
            [{"text": "✏️ Ввести период"}],
            [{"text": "🚫 Отмена"}],
        ],
        "resize_keyboard": True,
        "one_time_keyboard": False,
    }


def store_keyboard(stores: list[dict]) -> dict:
    """Клавиатура выбора склада. stores — список {short, name}."""
    rows = []
    # Попарно располагаем кнопки складов
    shorts = [f"🏪 {s['short']}" for s in stores]
    for i in range(0, len(shorts), 2):
        pair = [{"text": shorts[i]}]
        if i + 1 < len(shorts):
            pair.append({"text": shorts[i + 1]})
        rows.append(pair)
    rows.append([{"text": "📍 Все склады"}])
    rows.append([{"text": "🚫 Отмена"}])
    return {"keyboard": rows, "resize_keyboard": True}


def detail_keyboard() -> dict:
    """Клавиатура: полные позиции или только итоги."""
    return {
        "keyboard": [
            [{"text": "📋 С наименованиями позиций"}],
            [{"text": "📊 Только итоги"}],
            [{"text": "🚫 Отмена"}],
        ],
        "resize_keyboard": True,
    }


def input_dates_keyboard() -> dict:
    """Подсказка при вводе дат вручную — только отмена."""
    return {
        "keyboard": [[{"text": "🚫 Отмена"}]],
        "resize_keyboard": True,
    }


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
