"""Конфигурация: читает переменные окружения из .env (без внешних зависимостей).

Порядок поиска .env:
  1. переменные окружения процесса (наивысший приоритет);
  2. файл, указанный в HERMES_ENV_FILE;
  3. .env рядом с проектом;
  4. /opt/hermes/.env (боевой сервер).
"""
from __future__ import annotations

import os
from pathlib import Path


def _load_env_file(path: Path) -> None:
    """Простой парсер .env: KEY=VALUE, # — комментарий. Не перетирает уже заданные."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _bootstrap() -> None:
    candidates = []
    if os.environ.get("HERMES_ENV_FILE"):
        candidates.append(Path(os.environ["HERMES_ENV_FILE"]))
    candidates.append(Path(__file__).resolve().parent.parent / ".env")
    candidates.append(Path("/opt/hermes/.env"))
    for c in candidates:
        _load_env_file(c)


_bootstrap()


def get(name: str, default: str | None = None, required: bool = False) -> str | None:
    value = os.environ.get(name, default)
    if required and not value:
        raise RuntimeError(
            f"Не задана обязательная переменная окружения {name}. "
            f"Проверь .env (см. .env.example)."
        )
    return value


# --- Доступы ---
MOYSKLAD_TOKEN = lambda: get("MOYSKLAD_TOKEN", required=True)  # noqa: E731
DATABASE_URL = lambda: get("DATABASE_URL", required=True)      # noqa: E731
TELEGRAM_BOT_TOKEN = lambda: get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = lambda: get("TELEGRAM_CHAT_ID")


# --- Список складов для меню бота и фильтрации ---
# Ключ "short" — кнопка в боте, "name" — точное название в БД
STORES: list[dict] = [
    {"short": "Ленина 102А", "name": "Киров, Ленина 102А",         "id": "45168e06-344d-11f1-0a80-0c0400012fef"},
    {"short": "Слободской",  "name": "Слободской, Советская 64",   "id": "4a32d3c1-344d-11f1-0a80-13ba00011c8b"},
    {"short": "Воровского",  "name": "Розница Воровского 107/1",   "id": "acb431e3-3c6b-11f0-0a80-0b6600098edd"},
    {"short": "База",        "name": "База Воровского 107/1",      "id": "b4a45a8e-3d5e-11f0-0a80-0b690011c5d1"},
    {"short": "СОБРАНИЕ",    "name": "СОБРАНИЕ",                   "id": "e721ae80-9021-11f1-0a80-06c30015a0d1"},
]
# short → name (для поиска по кнопке)
STORE_NAME_BY_SHORT: dict[str, str] = {s["short"]: s["name"] for s in STORES}

# --- Справочник складов и каналов продаж ---
# id складов взяты из МойСклад на этапе разведки. Классификация — по решению владельца:
# розница = 3 магазина, опт = База, ресторан = СОБРАНИЕ (работает через перемещения).
STORE_CHANNELS: dict[str, str] = {
    "45168e06-344d-11f1-0a80-0c0400012fef": "розница",   # Киров, Ленина 102А
    "4a32d3c1-344d-11f1-0a80-13ba00011c8b": "розница",   # Слободской, Советская 64
    "acb431e3-3c6b-11f0-0a80-0b6600098edd": "розница",   # Розница Воровского 107/1
    "b4a45a8e-3d5e-11f0-0a80-0b690011c5d1": "опт",        # База Воровского 107/1
    "e721ae80-9021-11f1-0a80-06c30015a0d1": "ресторан",  # СОБРАНИЕ (ресторан)
}
