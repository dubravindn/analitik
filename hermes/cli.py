"""Точка входа Hermes. Примеры:

    python -m hermes init-db
    python -m hermes sync --from 2026-08-01 --to 2026-08-04
    python -m hermes report --date 2026-08-04
    python -m hermes send --date 2026-08-04
    python -m hermes daily
    python -m hermes whoami
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta

from . import config, db
from .etl_sales import run as run_sync
from .logging_setup import setup
from .moysklad import MoyskladClient
from .report_sales import build_day_report


def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def main(argv: list[str] | None = None) -> int:
    log = setup()
    parser = argparse.ArgumentParser(prog="hermes", description="Аналитика МойСклад")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("whoami", help="Проверить доступ к МойСклад")
    sub.add_parser("init-db", help="Создать/обновить таблицы в БД")

    p_sync = sub.add_parser("sync", help="Выгрузить продажи за период")
    p_sync.add_argument("--from", dest="d_from", required=True, type=_parse_date)
    p_sync.add_argument("--to", dest="d_to", required=True, type=_parse_date)

    p_rep = sub.add_parser("report", help="Отчёт по продажам за день (в stdout)")
    p_rep.add_argument("--date", dest="d", required=True, type=_parse_date)

    p_send = sub.add_parser("send", help="Собрать отчёт за день и отправить в Telegram")
    p_send.add_argument("--date", dest="d", required=True, type=_parse_date)

    sub.add_parser(
        "daily",
        help="Синхронизировать вчера + отправить отчёт в Telegram (для cron/systemd)",
    )

    args = parser.parse_args(argv)

    if args.cmd == "whoami":
        client = MoyskladClient(config.MOYSKLAD_TOKEN())
        me = client.whoami()
        print(f"Доступ есть. Сотрудник: {me.get('name')} | accountId: {me.get('accountId')}")
        return 0

    if args.cmd == "init-db":
        conn = db.connect(config.DATABASE_URL())
        db.apply_schema(conn)
        print("Схема готова.")
        return 0

    if args.cmd == "sync":
        client = MoyskladClient(config.MOYSKLAD_TOKEN())
        conn = db.connect(config.DATABASE_URL())
        db.apply_schema(conn)
        log.info("Старт выгрузки продаж %s..%s", args.d_from, args.d_to)
        run_sync(client, conn, args.d_from, args.d_to)
        print(f"Выгрузка завершена: {args.d_from}..{args.d_to}")
        return 0

    if args.cmd == "report":
        conn = db.connect(config.DATABASE_URL())
        print(build_day_report(conn, args.d))
        return 0

    if args.cmd == "send":
        conn = db.connect(config.DATABASE_URL())
        text = build_day_report(conn, args.d)
        _send_telegram(text, log)
        return 0

    if args.cmd == "daily":
        yesterday = date.today() - timedelta(days=1)
        client = MoyskladClient(config.MOYSKLAD_TOKEN())
        conn = db.connect(config.DATABASE_URL())
        db.apply_schema(conn)
        log.info("Ежедневная выгрузка за %s", yesterday)
        run_sync(client, conn, yesterday, yesterday)
        text = build_day_report(conn, yesterday)
        _send_telegram(text, log)
        return 0

    return 1


def _send_telegram(text: str, log) -> None:
    bot_token = config.TELEGRAM_BOT_TOKEN()
    chat_id = config.TELEGRAM_CHAT_ID()
    if bot_token and chat_id:
        from .telegram import send_message
        send_message(bot_token, chat_id, text)
        log.info("Отчёт отправлен в Telegram")
    else:
        print(text)
        log.warning("TELEGRAM_BOT_TOKEN или TELEGRAM_CHAT_ID не заданы — вывод в stdout")


if __name__ == "__main__":
    sys.exit(main())
