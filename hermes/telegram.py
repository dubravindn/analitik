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
    """Главное меню — постоянная клавиатура снизу.
    Telegram нативно скрывает стрелкой ↓ без команды.
    """
    return {
        "keyboard": [
            [{"text": "📊 Продажи"},   {"text": "📦 Остатки"}],
            [{"text": "🚨 Залежалые"}, {"text": "🗑 Списания"}],
            [{"text": "🎯 Резервы"},   {"text": "💸 Расходы"}],
            [{"text": "🔄 Перемещения"}, {"text": "👥 Клиенты"}],
            [{"text": "🔍 Изменения"}, {"text": "🛒 Прогноз"}],
            [{"text": "📄 Отчёт PDF"}, {"text": "❓ Помощь"}],
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


def group_keyboard(groups: list[str]) -> dict:
    """Клавиатура выбора группы товаров."""
    rows = []
    for i in range(0, len(groups), 2):
        pair = [{"text": f"📁 {groups[i]}"}]
        if i + 1 < len(groups):
            pair.append({"text": f"📁 {groups[i + 1]}"})
        rows.append(pair)
    rows.append([{"text": "📦 Все группы"}])
    rows.append([{"text": "🚫 Отмена"}])
    return {"keyboard": rows, "resize_keyboard": True}


def input_dates_keyboard() -> dict:
    """Подсказка при вводе дат вручную — только отмена."""
    return {
        "keyboard": [[{"text": "🚫 Отмена"}]],
        "resize_keyboard": True,
    }


def send_document(
    bot_token: str,
    chat_id: str,
    data: bytes,
    filename: str,
    caption: str = "",
) -> dict:
    """Отправить файл как документ (multipart/form-data).
    reply_markup НЕ передаём здесь — отправляем отдельным send_message.
    """
    import uuid
    boundary = "----HermesBoundary" + uuid.uuid4().hex

    def _field(name: str, value: str) -> bytes:
        return (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n"
        ).encode("utf-8")

    body_parts: list[bytes] = [
        _field("chat_id", str(chat_id)),
    ]
    if caption:
        body_parts.append(_field("caption", caption[:1024]))

    # Только ASCII в имени файла — кириллица может ломать multipart в некоторых клиентах
    safe_filename = filename.encode("ascii", errors="ignore").decode("ascii") or "report.pdf"
    body_parts.append((
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="document"; filename="{safe_filename}"\r\n'
        f"Content-Type: application/pdf\r\n\r\n"
    ).encode("ascii"))
    body_parts.append(data)
    body_parts.append(f"\r\n--{boundary}--\r\n".encode("ascii"))

    body = b"".join(body_parts)
    url = f"https://api.telegram.org/bot{bot_token}/sendDocument"
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body_err = e.read().decode(errors="replace")
        raise RuntimeError(f"Telegram sendDocument HTTP {e.code}: {body_err}") from e
    if not result.get("ok"):
        raise RuntimeError(f"Telegram sendDocument ошибка: {result}")
    return result


# ─── низкоуровневые утилиты ───────────────────────────────────────────────────

def _split(text: str, limit: int) -> list[str]:
    """Разбить текст на части ≤ limit по границам строк (не резать посреди строки).

    Одиночную строку длиннее limit режем жёстко — иначе её не отправить.
    """
    if len(text) <= limit:
        return [text]

    parts: list[str] = []
    buf = ""
    for line in text.split("\n"):
        # Строка сама по себе длиннее лимита — сбрасываем буфер и режем её жёстко.
        if len(line) > limit:
            if buf:
                parts.append(buf)
                buf = ""
            while len(line) > limit:
                parts.append(line[:limit])
                line = line[limit:]
            buf = line
            continue
        # Кандидат = буфер + перевод строки + текущая строка.
        candidate = f"{buf}\n{line}" if buf else line
        if len(candidate) <= limit:
            buf = candidate
        else:
            parts.append(buf)
            buf = line
    if buf:
        parts.append(buf)
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
