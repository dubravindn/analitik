"""Точка входа Hermes. Примеры:

    python -m hermes init-db
    python -m hermes sync --from 2026-08-01 --to 2026-08-04
    python -m hermes report --date 2026-08-04
    python -m hermes whoami
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime

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

    p_rep = sub.add_parser("report", help="Отчёт по продажам за день")
    p_rep.add_argument("--date", dest="d", required=True, type=_parse_date)

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

    return 1


if __name__ == "__main__":
    sys.exit(main())
