#!/usr/bin/env python3
"""
P-series: интеграционный тест всех кнопок Hermes-бота.

Прогоняет каждую кнопку через bot._handle() с реальной БД.
HTTP-вызовы в Telegram перехватываются — реальных сообщений пользователю НЕ отправляется.
Результат: tests/screenshots/REPORT.md

Использование:
    cd /Users/dmitrijdubravin/Desktop/АНАЛИТИК
    python3 tests/test_bot_e2e.py
"""
from __future__ import annotations

import sys
import time
import traceback
from pathlib import Path
from datetime import date, timedelta

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

# ── 1. Мокаем hermes.telegram ДО импорта bot ──────────────────────────────
# bot.py делает `from . import telegram as tg`, потом вызывает `tg.send_message(...)`.
# Мы мутируем атрибуты модуля — бот видит наши моки через тот же объект-модуль.
import hermes.telegram as _tg

_captured: list[dict] = []


def _mock_send_message(bot_token, chat_id, text, reply_markup=None):
    _captured.append({"type": "msg", "text": text})
    return {"ok": True}


def _mock_send_photo(bot_token, chat_id, data, caption=""):
    _captured.append({"type": "photo", "caption": caption, "kb": len(data) // 1024})
    return {"ok": True}


def _mock_send_document(bot_token, chat_id, data, filename, caption=""):
    _captured.append({"type": "doc", "filename": filename, "kb": len(data) // 1024})
    return {"ok": True}


_tg.send_message  = _mock_send_message
_tg.send_photo    = _mock_send_photo
_tg.send_document = _mock_send_document

# ── 2. Основные импорты ───────────────────────────────────────────────────
from hermes import config          # noqa: E402
from hermes import bot             # noqa: E402
from hermes.moysklad import MoyskladClient  # noqa: E402

try:
    import psycopg as _pg          # noqa: E402  # сервер: psycopg v3
except ImportError:
    import psycopg2 as _pg         # noqa: E402  # local macOS fallback

# ── 3. Фабрики ───────────────────────────────────────────────────────────
def conn_factory():
    return _pg.connect(config.DATABASE_URL())


def client_factory():
    try:
        token = config.MOYSKLAD_TOKEN()
        return MoyskladClient(token) if token else None
    except Exception:
        return None


# ── 4. Fake-апдейты ───────────────────────────────────────────────────────
_UID       = [0]
_BOT_TOKEN = "FAKE_TEST_TOKEN"    # не пойдёт в сеть — всё перехвачено
_CHAT_ID   = "88888888"           # должен совпадать в upd и в _handle


def _upd(text: str) -> dict:
    _UID[0] += 1
    return {
        "update_id": _UID[0],
        "message": {"chat": {"id": _CHAT_ID}, "text": text},
    }


def _h(text: str) -> None:
    bot._handle(_upd(text), conn_factory, client_factory, _BOT_TOKEN, _CHAT_ID)


# ── 5. Список кнопок с шагами диалога ────────────────────────────────────
# steps — порядок шагов; ответы ниже.
# Примечание: "🏷 Цены" — именно тег-эмодзи, как в telegram.py.
BUTTONS: list[tuple[str, str, list[str]]] = [
    ("📊 Продажи",     "sales",    ["period", "store"]),
    ("📦 Остатки",     "stock",    ["period", "store"]),
    ("🚨 Залежалые",   "stale",    ["period", "store", "group"]),
    ("🗑 Списания",    "loss",     ["period", "store"]),
    ("🎯 Резервы",     "reserves", ["period", "store"]),
    ("💸 Расходы",     "expenses", ["period", "store"]),
    ("🔄 Перемещения", "move",     ["period", "store"]),
    ("👥 Клиенты",     "clients",  ["period", "store"]),
    ("🔍 Изменения",   "audit",    ["period"]),
    ("🛒 Прогноз",     "forecast", []),
    ("📄 Отчёт PDF",   "pdf",      ["period", "store"]),
    ("🏷 Цены",        "prices",   []),
]

_STEP_ANSWER = {
    "period": "📅 Вчера",
    "store":  "📍 Все склады",
    "group":  "📦 Все группы",
}


# ── 6. Прогон одной кнопки ────────────────────────────────────────────────
def run_button(label: str, section: str, steps: list[str]) -> dict:
    """Симулирует полный диалог. Возвращает dict с результатом."""
    _captured.clear()
    bot._clear_state(_CHAT_ID)
    t0 = time.time()

    try:
        _h(label)
        for step in steps:
            _h(_STEP_ANSWER[step])

        elapsed = time.time() - t0

        msgs   = [m for m in _captured if m["type"] == "msg"]
        docs   = [m for m in _captured if m["type"] == "doc"]
        photos = [m for m in _captured if m["type"] == "photo"]
        texts  = [m["text"] for m in msgs]

        # Отчётные тексты — без служебных «⏳ …» и «⚠️ Данные могут быть…»
        report_texts = [
            t for t in texts
            if not t.startswith("⏳") and not t.startswith("⚠️ Данные")
        ]
        last = report_texts[-1] if report_texts else (texts[-1] if texts else "")

        is_error = "⚠️ Ошибка" in last

        if is_error:
            status = "❌ Ошибка"
        elif docs:
            status = f"✅ OK (PDF {docs[0]['kb']} КБ, {elapsed:.0f}с)"
        elif photos:
            status = f"✅ OK (фото, {elapsed:.0f}с)"
        elif last:
            status = f"✅ OK ({elapsed:.0f}с)"
        else:
            status = "⚠️ Пустой ответ"

        return {
            "label": label,
            "section": section,
            "status": status,
            "elapsed": elapsed,
            "preview": last[:300],
            "error": "",
        }

    except Exception as e:
        tb = traceback.format_exc()
        return {
            "label": label,
            "section": section,
            "status": "❌ Исключение",
            "elapsed": time.time() - t0,
            "preview": "",
            "error": f"{type(e).__name__}: {e}\n{tb[-800:]}",
        }


# ── 7. Главный запуск ─────────────────────────────────────────────────────
def main():
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    print(f"\nHermes — автопроверка кнопок")
    print(f"Дата: {date.today()} | Период: Вчера ({yesterday}) | Склад: Все склады")
    print("─" * 65)

    results: list[dict] = []
    for label, section, steps in BUTTONS:
        print(f"▶  {label:<20} ", end="", flush=True)
        r = run_button(label, section, steps)
        results.append(r)
        print(r["status"])
        if r["error"]:
            for line in r["error"].split("\n")[-5:]:
                if line.strip():
                    print(f"     {line}")

    # ── Отчёт REPORT.md ───────────────────────────────────────────────────
    out_dir = Path(__file__).parent / "screenshots"
    out_dir.mkdir(exist_ok=True)

    rows = [
        "# Hermes Bot — результаты автопроверки",
        f"Дата: {date.today()} · Период: Вчера ({yesterday}) · Склад: Все склады",
        "",
        "| # | Кнопка | Статус | Первые строки ответа |",
        "|---|--------|--------|----------------------|",
    ]

    errors: list[dict] = []
    for i, r in enumerate(results, 1):
        preview = r["preview"][:200].replace("|", "\\|").replace("\n", " / ")
        rows.append(f"| {i:02d} | {r['label']} | {r['status']} | {preview} |")
        if "❌" in r["status"]:
            errors.append(r)

    if errors:
        rows += ["", "## Проблемы"]
        for r in errors:
            detail = (r["error"] or r["preview"])[:600]
            rows.append(f"### {r['label']} (`{r['section']}`)")
            rows.append(f"```\n{detail}\n```")

    ok_count   = sum(1 for r in results if "✅" in r["status"])
    warn_count = sum(1 for r in results if "⚠️" in r["status"])
    err_count  = sum(1 for r in results if "❌" in r["status"])

    rows += [
        "",
        f"## Итог: {ok_count} ✅ · {warn_count} ⚠️ · {err_count} ❌ из {len(results)} кнопок",
        "",
        "## Методология",
        f"- Вызов через `bot._handle()` напрямую (не через HTTP long-polling)",
        f"- БД: `{config.DATABASE_URL()[:40]}…`",
        f"- `hermes.telegram.send_message/send_photo/send_document` перехвачены",
        f"- HTTP в Telegram **не отправлялось**; скриншоты не делались",
    ]

    report = out_dir / "REPORT.md"
    report.write_text("\n".join(rows), encoding="utf-8")

    print("\n" + "─" * 65)
    print(f"📄  REPORT.md → {report}")
    print(f"Итог: {ok_count} ✅   {warn_count} ⚠️   {err_count} ❌   ({len(results)} кнопок)")
    if err_count:
        print("\nПроблемные кнопки:")
        for r in errors:
            print(f"  ❌ {r['label']}: {(r['error'] or r['preview'])[:120]}")


if __name__ == "__main__":
    main()
