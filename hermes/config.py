"""Конфигурация: читает переменные окружения из .env (без внешних зависимостей).

Порядок поиска .env:
  1. переменные окружения процесса (наивысший приоритет);
  2. файл, указанный в HERMES_ENV_FILE;
  3. .env рядом с проектом;
  4. /opt/hermes/.env (боевой сервер).
"""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# Московская зона (UTC+3, без переходов на летнее время).
# МойСклад отдаёт moment в московском времени без указания зоны, поэтому
# помечаем именно MSK, а не UTC. Дата «сегодня» тоже считается по Москве —
# иначе с 00:00 до 03:00 МСК кнопка «сегодня» показывала бы вчерашний день.
MSK = timezone(timedelta(hours=3))


def msk_now() -> datetime:
    """Текущий момент в московской зоне."""
    return datetime.now(MSK)


def msk_today() -> date:
    """Сегодняшняя дата по Москве."""
    return msk_now().date()


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

# Московские поставщики (точные имена контрагентов из МойСклад).
# По этим именам определяются даты фургонов для прогноза закупки.
# Заполнить после того, как посмотришь в supply_doc: SELECT DISTINCT agent_name FROM supply_doc
MOSCOW_SUPPLIERS: list[str] = ['ООО "Поставщик"']

# Служебные контрагенты-заглушки розницы: розничные продажи проводятся на них
# документами demand (не на реального покупателя). В «Топ клиентов» они не
# показываются как клиенты — их сумма выводится одной строкой «Розничные продажи
# без идентификации». Пополнять при появлении новых розничных точек:
#   SELECT agent_name, COUNT(*), SUM(sum_kop)/100 FROM sales_doc
#   WHERE agent_name ILIKE '%покупатель%' OR agent_name ILIKE '%розница%'
#   GROUP BY agent_name ORDER BY 3 DESC;
RETAIL_PLACEHOLDER_AGENTS: list[str] = [
    "Покупатель Филармония",
    "Покупатель Воровского 107",
    "Слободской РОЗНИЦА",
    "О покупатель",
]

# Внутренние контрагенты (свои ИП, оптовые счета своих точек) — это НЕ клиенты,
# а внутренние операции. Исключаются из топа клиентов, показываются отдельной
# строкой. Пополнять по разметке владельца (см. G6, эвристика ⚙ в отчёте).
INTERNAL_AGENTS: list[str] = [
    "ОПТ Слободской",
    "ИП Дубравин Николай Николаевич",
    "ИП Дубравин Дмитрий Николаевич",
]

# Подстроки-подсказки: контрагенты с этими фрагментами помечаются ⚙ «похоже на
# внутренний» (не исключаются автоматически — только подсказка для разметки).
INTERNAL_HINTS: list[str] = ["ИП Дубравин", "ОПТ ", "РОЗНИЦА", "Покупатель"]

# Склады, где списания — это ИНВЕНТАРИЗАЦИОННЫЕ КОРРЕКТИРОВКИ учёта, а не порча
# (решение владельца, H2). Вся реальная порча списывается на рознице; испорченное
# с Базы перемещают на точку и списывают там. Списания этих складов НЕ считаются
# потерями и исключаются из расхода в прогнозе.
ADJUSTMENT_STORES: list[str] = ["База Воровского 107/1"]

# Точки, где через ОДНУ кассу идут и опт, и розница (решение владельца, I-ответы).
# Их маржу нельзя сравнивать с чисто розничными точками — низкая маржа тут норма
# (смесь каналов), а не ошибка. Сравниваем такую точку с её собственной историей.
MIXED_CHANNEL_STORES: list[str] = ["Слободской, Советская 64"]
