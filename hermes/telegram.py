"""Отправка сообщений в Telegram через Bot API (только стандартная библиотека)."""
from __future__ import annotations

import json
import urllib.error
import urllib.request


def send_message(bot_token: str, chat_id: str, text: str) -> None:
    """Отправить текстовое сообщение в Telegram-чат.

    Telegram ограничивает длину до 4096 символов — длинные сообщения режем на части.
    """
    for chunk in _split(text, 4096):
        _post(bot_token, "sendMessage", {"chat_id": chat_id, "text": chunk})


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
