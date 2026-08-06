"""Слой доступа к PostgreSQL на psycopg 3."""
from __future__ import annotations

import logging
from pathlib import Path

import psycopg

log = logging.getLogger("hermes.db")

_SCHEMA = Path(__file__).resolve().parent / "schema.sql"


def connect(database_url: str) -> psycopg.Connection:
    conn = psycopg.connect(database_url, autocommit=False)
    # Отдавать timestamptz в московском времени. Храним в UTC (ETL помечают
    # moment как config.MSK), но по умолчанию сессия Postgres в UTC и отчёты
    # печатали время на 3 ч назад. Ставим зону сессии — чинит вывод во всех
    # отчётах разом (report_loss/move/cashflow/audit, alerts).
    with conn.cursor() as cur:
        cur.execute("SET TIME ZONE 'Europe/Moscow'")
    conn.commit()
    return conn


def apply_schema(conn: psycopg.Connection) -> None:
    sql = _SCHEMA.read_text(encoding="utf-8")
    with conn.cursor() as cur:
        # CREATE OR REPLACE VIEW берёт ACCESS EXCLUSIVE и может ждать вечно, если
        # бот в этот момент читает вью. Ограничиваем ожидание — лучше явная ошибка,
        # чем зависание (migrate тогда запускать при остановленном боте).
        cur.execute("SET lock_timeout = '15s'")
        cur.execute(sql)
    conn.commit()
    log.info("Схема применена (idempotent)")


def upsert_store_day(conn: psycopg.Connection, row: dict) -> None:
    """Идемпотентно: повторный запуск за тот же день перезаписывает строку, не дублирует."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO sales_by_store_day
                (day, store_id, store_name, channel, revenue_kop, cost_kop,
                 checks, positions_total, positions_nocost, synced_at)
            VALUES
                (%(day)s, %(store_id)s, %(store_name)s, %(channel)s, %(revenue_kop)s,
                 %(cost_kop)s, %(checks)s, %(positions_total)s, %(positions_nocost)s, now())
            ON CONFLICT (day, store_id) DO UPDATE SET
                store_name = EXCLUDED.store_name,
                channel = EXCLUDED.channel,
                revenue_kop = EXCLUDED.revenue_kop,
                cost_kop = EXCLUDED.cost_kop,
                checks = EXCLUDED.checks,
                positions_total = EXCLUDED.positions_total,
                positions_nocost = EXCLUDED.positions_nocost,
                synced_at = now()
            """,
            row,
        )


def replace_products_for_day_store(conn: psycopg.Connection, day: str, store_id: str, rows: list[dict]) -> None:
    """Идемпотентно: сносим товарные строки за (день, склад) и вставляем заново."""
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM sales_by_product_day WHERE day = %s AND store_id = %s",
            (day, store_id),
        )
        if rows:
            cur.executemany(
                """
                INSERT INTO sales_by_product_day
                    (day, store_id, assortment_id, product_name, sell_qty,
                     revenue_kop, cost_kop, profit_kop)
                VALUES
                    (%(day)s, %(store_id)s, %(assortment_id)s, %(product_name)s,
                     %(sell_qty)s, %(revenue_kop)s, %(cost_kop)s, %(profit_kop)s)
                """,
                rows,
            )


def log_sync(conn: psycopg.Connection, **kw) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO sync_log (task, period_from, period_to, rows_loaded, duration_ms, ok, error)
            VALUES (%(task)s, %(period_from)s, %(period_to)s, %(rows_loaded)s, %(duration_ms)s, %(ok)s, %(error)s)
            """,
            kw,
        )
    conn.commit()
