"""Точка входа Hermes. Примеры:

    python -m hermes init-db
    python -m hermes sync --from 2026-08-01 --to 2026-08-04
    python -m hermes sync-stock
    python -m hermes report --date 2026-08-04
    python -m hermes report-stock --date 2026-08-05
    python -m hermes send --date 2026-08-04
    python -m hermes daily
    python -m hermes whoami
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import date, datetime, timedelta

from . import config, db
from .etl_sales import run as run_sync
from .etl_stock import run as run_sync_stock
from .etl_loss import run as run_sync_loss
from .etl_supply import run as run_sync_supply
from .etl_cashflow import run as run_sync_cashflow
from .etl_clients import run as run_sync_clients
from .etl_move import run as run_sync_move
from .etl_prices import run as run_sync_prices
from .logging_setup import setup
from .moysklad import MoyskladClient
from .report_sales import build_day_report
from .report_stock import build_stock_report
from .report_loss import build_loss_report
from .report_supply import build_supply_report
from .report_cashflow import build_cashflow_report
from .report_employees import build_employee_report
from .report_move import build_move_report


def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def main(argv: list[str] | None = None) -> int:
    log = setup()
    parser = argparse.ArgumentParser(prog="hermes", description="Аналитика МойСклад")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("whoami", help="Проверить доступ к МойСклад")
    sub.add_parser("init-db", help="Создать/обновить таблицы в БД")
    sub.add_parser("migrate", help="Применить схему (индексы) — отдельно от синков")

    p_sync = sub.add_parser("sync", help="Выгрузить продажи за период")
    p_sync.add_argument("--from", dest="d_from", required=True, type=_parse_date)
    p_sync.add_argument("--to", dest="d_to", required=True, type=_parse_date)

    p_sync_stock = sub.add_parser("sync-stock", help="Снимок остатков на дату (по умолчанию сегодня)")
    p_sync_stock.add_argument("--date", dest="d", default=None, type=_parse_date)

    p_rep = sub.add_parser("report", help="Отчёт по продажам за день (в stdout)")
    p_rep.add_argument("--date", dest="d", required=True, type=_parse_date)

    p_rep_stock = sub.add_parser("report-stock", help="Отчёт по остаткам за день (в stdout)")
    p_rep_stock.add_argument("--date", dest="d", required=True, type=_parse_date)

    p_send = sub.add_parser("send", help="Отчёт по продажам за день → Telegram")
    p_send.add_argument("--date", dest="d", required=True, type=_parse_date)

    p_send_stock = sub.add_parser("send-stock", help="Отчёт по остаткам за день → Telegram")
    p_send_stock.add_argument("--date", dest="d", required=True, type=_parse_date)

    p_sync_loss = sub.add_parser("sync-loss", help="Выгрузить списания за период")
    p_sync_loss.add_argument("--from", dest="d_from", required=True, type=_parse_date)
    p_sync_loss.add_argument("--to", dest="d_to", required=True, type=_parse_date)

    p_sync_supply = sub.add_parser("sync-supply", help="Выгрузить поставки за период")
    p_sync_supply.add_argument("--from", dest="d_from", required=True, type=_parse_date)
    p_sync_supply.add_argument("--to", dest="d_to", required=True, type=_parse_date)

    p_sync_cf = sub.add_parser("sync-cashflow", help="Выгрузить ДДС за период")
    p_sync_cf.add_argument("--from", dest="d_from", required=True, type=_parse_date)
    p_sync_cf.add_argument("--to", dest="d_to", required=True, type=_parse_date)

    p_sync_clients = sub.add_parser("sync-clients", help="Выгрузить отгрузки (demand) за период")
    p_sync_clients.add_argument("--from", dest="d_from", required=True, type=_parse_date)
    p_sync_clients.add_argument("--to", dest="d_to", required=True, type=_parse_date)

    p_sync_move = sub.add_parser("sync-move", help="Выгрузить перемещения за период")
    p_sync_move.add_argument("--from", dest="d_from", required=True, type=_parse_date)
    p_sync_move.add_argument("--to", dest="d_to", required=True, type=_parse_date)

    p_sync_prices = sub.add_parser("sync-prices", help="Снимок цен номенклатуры (закупочная из карточки)")
    p_sync_prices.add_argument("--date", dest="d", default=None, type=_parse_date)

    p_rep_loss = sub.add_parser("report-loss", help="Отчёт по списаниям (в stdout)")
    p_rep_loss.add_argument("--from", dest="d_from", required=True, type=_parse_date)
    p_rep_loss.add_argument("--to", dest="d_to", required=True, type=_parse_date)

    p_rep_supply = sub.add_parser("report-supply", help="Отчёт по закупкам (в stdout)")
    p_rep_supply.add_argument("--from", dest="d_from", required=True, type=_parse_date)
    p_rep_supply.add_argument("--to", dest="d_to", required=True, type=_parse_date)

    p_rep_cf = sub.add_parser("report-cashflow", help="Отчёт ДДС (в stdout)")
    p_rep_cf.add_argument("--from", dest="d_from", required=True, type=_parse_date)
    p_rep_cf.add_argument("--to", dest="d_to", required=True, type=_parse_date)

    p_rep_move = sub.add_parser("report-move", help="Отчёт по перемещениям (в stdout)")
    p_rep_move.add_argument("--from", dest="d_from", required=True, type=_parse_date)
    p_rep_move.add_argument("--to", dest="d_to", required=True, type=_parse_date)
    p_rep_move.add_argument("--store", dest="store", default=None)

    p_rep_emp = sub.add_parser("report-employees", help="Аналитика сотрудников (в stdout)")
    p_rep_emp.add_argument("--from", dest="d_from", required=True, type=_parse_date)
    p_rep_emp.add_argument("--to", dest="d_to", required=True, type=_parse_date)

    sub.add_parser("report-forecast", help="Прогноз закупки (заказы + история фургонов)")

    sub.add_parser(
        "daily",
        help="Sync вчера (продажи) + сегодня (остатки) → отправить оба отчёта в Telegram",
    )

    p_backfill = sub.add_parser(
        "backfill",
        help="Разовая прогрузка истории помесячно (запускать вручную, ночью)",
    )
    p_backfill.add_argument("--months", dest="months", type=int, default=12,
                            help="За сколько месяцев назад грузить (по умолчанию 12)")

    sub.add_parser("bot", help="Запустить Telegram-бот (long-polling, блокирующий)")

    args = parser.parse_args(argv)

    # Долгие выгрузки помечают «идёт синхронизация» — бот покажет предупреждение
    # вместо молчаливого зависания на контенции. Маркер снимается при выходе.
    _SYNC_CMDS = {"sync", "sync-stock", "sync-loss", "sync-supply", "sync-cashflow",
                  "sync-clients", "sync-move", "sync-prices", "backfill", "daily"}
    if args.cmd in _SYNC_CMDS:
        import atexit
        from . import synclock
        synclock.set_running(args.cmd)
        atexit.register(synclock.clear)

    if args.cmd == "whoami":
        client = MoyskladClient(config.MOYSKLAD_TOKEN())
        me = client.whoami()
        print(f"Доступ есть. Сотрудник: {me.get('name')} | accountId: {me.get('accountId')}")
        return 0

    if args.cmd in ("init-db", "migrate"):
        conn = db.connect(config.DATABASE_URL())
        db.apply_schema(conn)
        print("Схема применена.")
        return 0

    if args.cmd == "sync":
        client = MoyskladClient(config.MOYSKLAD_TOKEN())
        conn = db.connect(config.DATABASE_URL())
        log.info("Старт выгрузки продаж %s..%s", args.d_from, args.d_to)
        run_sync(client, conn, args.d_from, args.d_to)
        print(f"Выгрузка завершена: {args.d_from}..{args.d_to}")
        return 0

    if args.cmd == "sync-stock":
        client = MoyskladClient(config.MOYSKLAD_TOKEN())
        conn = db.connect(config.DATABASE_URL())
        snap_date = args.d or config.msk_today()
        n = run_sync_stock(client, conn, snap_date)
        print(f"Снимок остатков на {snap_date}: {n} позиций")
        return 0

    if args.cmd == "report":
        conn = db.connect(config.DATABASE_URL())
        print(build_day_report(conn, args.d))
        return 0

    if args.cmd == "report-stock":
        conn = db.connect(config.DATABASE_URL())
        print(build_stock_report(conn, args.d))
        return 0

    if args.cmd == "send":
        conn = db.connect(config.DATABASE_URL())
        _send_telegram(build_day_report(conn, args.d), log)
        return 0

    if args.cmd == "send-stock":
        conn = db.connect(config.DATABASE_URL())
        _send_telegram(build_stock_report(conn, args.d), log)
        return 0

    if args.cmd == "sync-loss":
        client = MoyskladClient(config.MOYSKLAD_TOKEN())
        conn = db.connect(config.DATABASE_URL())
        n = run_sync_loss(client, conn, args.d_from, args.d_to)
        print(f"Выгружено списаний: {n} документов за {args.d_from}..{args.d_to}")
        return 0

    if args.cmd == "sync-supply":
        client = MoyskladClient(config.MOYSKLAD_TOKEN())
        conn = db.connect(config.DATABASE_URL())
        n = run_sync_supply(client, conn, args.d_from, args.d_to)
        print(f"Выгружено поставок: {n} документов за {args.d_from}..{args.d_to}")
        return 0

    if args.cmd == "sync-cashflow":
        client = MoyskladClient(config.MOYSKLAD_TOKEN())
        conn = db.connect(config.DATABASE_URL())
        n = run_sync_cashflow(client, conn, args.d_from, args.d_to)
        print(f"Выгружено платежей: {n} событий за {args.d_from}..{args.d_to}")
        return 0

    if args.cmd == "sync-clients":
        client = MoyskladClient(config.MOYSKLAD_TOKEN())
        conn = db.connect(config.DATABASE_URL())
        n = run_sync_clients(client, conn, args.d_from, args.d_to)
        print(f"Выгружено отгрузок: {n} документов за {args.d_from}..{args.d_to}")
        return 0

    if args.cmd == "sync-prices":
        client = MoyskladClient(config.MOYSKLAD_TOKEN())
        conn = db.connect(config.DATABASE_URL())
        n = run_sync_prices(client, conn, args.d or config.msk_today())
        print(f"Снимок цен: {n} товаров")
        return 0

    if args.cmd == "sync-move":
        client = MoyskladClient(config.MOYSKLAD_TOKEN())
        conn = db.connect(config.DATABASE_URL())
        n = run_sync_move(client, conn, args.d_from, args.d_to)
        print(f"Выгружено перемещений: {n} документов за {args.d_from}..{args.d_to}")
        return 0

    if args.cmd == "report-loss":
        conn = db.connect(config.DATABASE_URL())
        print(build_loss_report(conn, args.d_from, args.d_to))
        return 0

    if args.cmd == "report-supply":
        conn = db.connect(config.DATABASE_URL())
        print(build_supply_report(conn, args.d_from, args.d_to))
        return 0

    if args.cmd == "report-cashflow":
        conn = db.connect(config.DATABASE_URL())
        print(build_cashflow_report(conn, args.d_from, args.d_to))
        return 0

    if args.cmd == "report-move":
        conn = db.connect(config.DATABASE_URL())
        print(build_move_report(conn, args.d_from, args.d_to, args.store))
        return 0

    if args.cmd == "report-employees":
        client = MoyskladClient(config.MOYSKLAD_TOKEN())
        print(build_employee_report(client, args.d_from, args.d_to))
        return 0

    if args.cmd == "report-forecast":
        from .report_forecast import build_forecast_report
        client = MoyskladClient(config.MOYSKLAD_TOKEN())
        conn   = db.connect(config.DATABASE_URL())
        print(build_forecast_report(client, conn))
        return 0

    if args.cmd == "bot":
        from .bot import run as run_bot
        conn = db.connect(config.DATABASE_URL())
        try:
            db.apply_schema(conn)   # схема применяется один раз при старте бота
        except Exception as e:
            log.warning("apply_schema при старте не удался (%s) — продолжаю, "
                        "схема, вероятно, уже применена; при изменениях запусти migrate", e)
        run_bot(
            conn_factory=lambda: db.connect(config.DATABASE_URL()),
            client_factory=lambda: MoyskladClient(config.MOYSKLAD_TOKEN()),
            bot_token=config.TELEGRAM_BOT_TOKEN(),
            chat_id=config.TELEGRAM_CHAT_ID(),
        )
        return 0

    if args.cmd == "daily":
        today = config.msk_today()
        yesterday = today - timedelta(days=1)
        client = MoyskladClient(config.MOYSKLAD_TOKEN())
        conn = db.connect(config.DATABASE_URL())
        # Схему НЕ применяем: бот делает это при старте, а CREATE OR REPLACE VIEW
        # конфликтует с работающим ботом. Миграции — командой migrate (бот стоп).

        # 1. Сначала синкаем ВСЕ сущности за вчера (+ остатки на сегодня).
        # Каждый синк изолирован: падение одного не останавливает остальные —
        # иначе в истории появляются дыры (ложный отток клиентов и т.п.).
        def _safe(name: str, fn, *a) -> None:
            try:
                log.info("daily-sync: %s", name)
                fn(*a)
            except Exception as e:
                log.exception("daily-sync %s упал: %s", name, e)

        _safe("продажи",     run_sync,          client, conn, yesterday, yesterday)
        _safe("остатки",     run_sync_stock,    client, conn, today)
        _safe("цены",        run_sync_prices,   client, conn, today)
        _safe("списания",    run_sync_loss,     client, conn, yesterday, yesterday)
        _safe("ДДС",         run_sync_cashflow, client, conn, yesterday, yesterday)
        _safe("клиенты",     run_sync_clients,  client, conn, yesterday, yesterday)
        _safe("поставки",    run_sync_supply,   client, conn, yesterday, yesterday)
        _safe("перемещения", run_sync_move,     client, conn, yesterday, yesterday)

        # 2. Потом отчёты и алерты (каждый тоже изолирован).
        def _safe_send(name: str, build_fn) -> None:
            try:
                _send_telegram(build_fn(), log)
            except Exception as e:
                log.exception("daily-отчёт %s упал: %s", name, e)

        _safe_send("продажи вчера", lambda: build_day_report(conn, yesterday))
        _safe_send("остатки сегодня", lambda: build_stock_report(conn, today))

        from .alerts import build_alerts
        try:
            alert_text = build_alerts(conn, yesterday)
            if alert_text:
                log.info("Отправляем алерт выручки")
                _send_telegram(alert_text, log)
        except Exception as e:
            log.exception("daily-алерты упали: %s", e)

        return 0

    if args.cmd == "backfill":
        return _run_backfill(args.months, log)

    return 1


def _month_ranges(months: int, today: date) -> list[tuple[date, date]]:
    """Список (начало_месяца, конец_месяца) от старого к новому.

    Последний диапазон заканчивается сегодняшним днём. months=1 → текущий месяц.
    """
    # Начинаем с первого дня месяца (months-1) назад.
    y, m = today.year, today.month
    back = months - 1
    start_month = m - back
    start_year = y
    while start_month <= 0:
        start_month += 12
        start_year -= 1
    cur = date(start_year, start_month, 1)
    ranges: list[tuple[date, date]] = []
    while cur <= today:
        # Последний день месяца.
        if cur.month == 12:
            nxt = date(cur.year + 1, 1, 1)
        else:
            nxt = date(cur.year, cur.month + 1, 1)
        m_end = min(nxt - timedelta(days=1), today)
        ranges.append((cur, m_end))
        cur = nxt
    return ranges


def _month_done(conn, tag: str) -> bool:
    """Был ли месяц уже успешно прогружен (есть запись sync_log ok=true)?"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM sync_log WHERE task = %s AND ok = true LIMIT 1", (tag,)
        )
        return cur.fetchone() is not None


def _run_backfill(months: int, log) -> int:
    client = MoyskladClient(config.MOYSKLAD_TOKEN())
    conn = db.connect(config.DATABASE_URL())
    db.apply_schema(conn)

    today = config.msk_today()
    ranges = _month_ranges(months, today)
    log.info("Backfill: %d мес., %d диапазонов (%s .. %s)",
             months, len(ranges), ranges[0][0], ranges[-1][1])

    # Сущности: (имя, функция синка). Продажи, клиенты(+возвраты), списания,
    # ДДС, поставки, перемещения. Порядок — от старого к новому по месяцам.
    entities = [
        ("продажи",     run_sync),
        ("клиенты",     run_sync_clients),
        ("списания",    run_sync_loss),
        ("ДДС",         run_sync_cashflow),
        ("поставки",    run_sync_supply),
        ("перемещения", run_sync_move),
    ]

    for i, (d_from, d_to) in enumerate(ranges):
        tag = f"backfill-{d_from.strftime('%Y-%m')}"
        if _month_done(conn, tag):
            log.info("[%d/%d] %s — уже прогружен, пропуск", i + 1, len(ranges), tag)
            continue

        log.info("[%d/%d] %s: %s .. %s", i + 1, len(ranges), tag, d_from, d_to)
        t0 = time.monotonic()
        month_ok = True
        errors: list[str] = []
        for name, fn in entities:
            try:
                fn(client, conn, d_from, d_to)
            except Exception as e:
                month_ok = False
                errors.append(f"{name}: {e}")
                log.exception("backfill %s / %s упал: %s", tag, name, e)

        dur_ms = int((time.monotonic() - t0) * 1000)
        db.log_sync(conn, task=tag, period_from=d_from, period_to=d_to,
                    rows_loaded=None, duration_ms=dur_ms, ok=month_ok,
                    error="; ".join(errors) if errors else None)
        log.info("[%d/%d] %s — %s за %d c", i + 1, len(ranges), tag,
                 "OK" if month_ok else "С ОШИБКАМИ", dur_ms // 1000)

        # Пауза между месяцами (щадим лимиты МойСклад), кроме последнего.
        if i < len(ranges) - 1:
            time.sleep(7)

    # Сводка по таблицам и покрытию.
    log.info("── Backfill завершён. Сводка ──")
    tables = ["sales_by_store_day", "sales_doc", "loss_doc",
              "cashflow_event", "supply_doc", "move_doc"]
    for t in tables:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*), MIN(day), MAX(day) FROM {t}")
            cnt, dmin, dmax = cur.fetchone()
        log.info("  %-20s строк: %-7s покрытие: %s .. %s", t, cnt, dmin, dmax)
    print("Backfill завершён. Подробности в логе.")
    return 0


def _send_telegram(text: str, log) -> None:
    bot_token = config.TELEGRAM_BOT_TOKEN()
    chat_id = config.TELEGRAM_CHAT_ID()
    if bot_token and chat_id:
        from .telegram import send_message
        send_message(bot_token, chat_id, text)
        log.info("Отправлено в Telegram (%d символов)", len(text))
    else:
        print(text)
        log.warning("TELEGRAM_BOT_TOKEN или TELEGRAM_CHAT_ID не заданы — вывод в stdout")


if __name__ == "__main__":
    sys.exit(main())
