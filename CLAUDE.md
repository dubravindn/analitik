# Hermes — правила для Claude Code

## Обязательно в начале каждой сессии
1. `graphify update .` — обновить граф зависимостей (уже установлен глобально).
2. Прочитать `CHANGELOG.md` — понять, что сделано в последних коммитах.

## Обязательно после каждого изменения кода
- `python3 -m py_compile hermes/*.py` — проверить синтаксис.
- `python3 -m pytest tests/ -q` — прогнать тесты. Красный тест = стоп,
  не коммитить пока не зелёный.

## Запрещено без явного разрешения владельца
- Трогать production БД (DSN из переменных окружения на сервере).
- Мёрджить в `main`.
- Запускать `sync-*` или `backfill` команды.
- Устанавливать новые pip-пакеты без проверки с владельцем.

## Архитектура (кратко)
- `etl_*.py` — загрузка из МойСклад → PostgreSQL (идемпотентно).
- `report_*.py` — расчёт и текст отчётов (только чтение БД).
- `calc.py` — общие формулы. Отдельного `fmt.py` нет: форматирование
  живёт в самих отчётах (локальные хелперы `_rub()` и подобные).
- `bot.py` — Telegram-интерфейс, `cli.py` — командная строка.
- Деньги везде в копейках (bigint), в рубли только при выводе.
- Фильтр «Ассортимент»: `calc.assortment_filter()` — использовать везде,
  где читаются товарные данные.

## Конфигурация
Значения в `config.py` — это **лямбды, их надо вызывать**:
- `config.DATABASE_URL()` — строка подключения к PostgreSQL.
- `config.MOYSKLAD_TOKEN()` — токен МойСклад.
- `config.TELEGRAM_BOT_TOKEN()`, `config.TELEGRAM_CHAT_ID()`.

Имён `config.DB_DSN` и `config.MS_TOKEN` не существует — обращение к ним
даёт `AttributeError`.

## Ключевые бизнес-правила (не менять без явного решения владельца)
- Прибыль = выручка − закупочная цена из карточки товара (buyPrice).
- Списания Базы = корректировки инвентаризации, НЕ потери.
  Список: `config.ADJUSTMENT_STORES`.
- Залежалые: только СРЕЗКА, порог > 5 дней, сортировка по количеству.
- Заказ в прогнозе считается по продажам, списания в формулу не входят.
- Округление до упаковки — только для группы СРЕЗКА.

## Инструменты
- `graphify query "название"` — найти где используется функция/таблица.
- `graphify path "A" "B"` — цепочка вызовов от A до B.
- `graphify affected "A" --depth 2` — что сломается при правке A.
- `/unlock` в Telegram-боте — снять залипший флаг выгрузки.

## graphify

This project has a knowledge graph at graphify-out/ with god nodes, community structure, and cross-file relationships.

Rules:
- For codebase questions, first run `graphify query "<question>"` when graphify-out/graph.json exists. Use `graphify path "<A>" "<B>"` for relationships and `graphify explain "<concept>"` for focused concepts. These return a scoped subgraph, usually much smaller than GRAPH_REPORT.md or raw grep output.
- If graphify-out/wiki/index.md exists, use it for broad navigation instead of raw source browsing.
- Read graphify-out/GRAPH_REPORT.md only for broad architecture review or when query/path/explain do not surface enough context.
- After modifying code, run `graphify update .` to keep the graph current (AST-only, no API cost).
