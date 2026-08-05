"""Выгрузка продаж из МойСклад в PostgreSQL.

Идея: не дёргать API при каждом отчёте, а один раз выгрузить агрегаты по дням
и складам, и дальше считать по локальной БД. Повторный запуск за тот же день
перезаписывает данные (идемпотентно), а не плодит дубли.
"""
from __future__ import annotations

import logging
import time
from datetime import date, timedelta

from . import db
from .config import STORE_CHANNELS
from .moysklad import MoyskladClient

log = logging.getLogger("hermes.etl")


def _href_id(href: str) -> str:
    """Из ссылки .../entity/product/<id> достаём <id> (без параметров)."""
    return href.split("/")[-1].split("?")[0]


def _daterange(d_from: date, d_to: date):
    d = d_from
    while d <= d_to:
        yield d
        d += timedelta(days=1)


def sync_sales(client: MoyskladClient, conn, d_from: date, d_to: date) -> int:
    """Выгружает продажи за период [d_from, d_to] по всем известным складам.

    Возвращает число загруженных строк «склад-день».
    """
    # Реальные склады из API (имя может измениться — берём свежее).
    store_names = {_href_id(s["meta"]["href"]): s["name"] for s in client.stores()}
    total_rows = 0

    for day in _daterange(d_from, d_to):
        m_from = f"{day.isoformat()} 00:00:00"
        m_to = f"{day.isoformat()} 23:59:59"

        for store_id, channel in STORE_CHANNELS.items():
            store_name = store_names.get(store_id, store_id)
            href = client.store_href(store_id)

            rows = client.profit_by_product(href, m_from, m_to)

            revenue_kop = 0
            cost_kop = 0
            positions_total = 0
            positions_nocost = 0
            product_rows = []
            for r in rows:
                sell = int(r.get("sellSum", 0)) - int(r.get("returnSum", 0))
                cost = int(r.get("sellCostSum", 0)) - int(r.get("returnCostSum", 0))
                qty = float(r.get("sellQuantity", 0)) - float(r.get("returnQuantity", 0))
                revenue_kop += sell
                cost_kop += cost
                if r.get("sellQuantity", 0):
                    positions_total += 1
                    if int(r.get("sellCostSum", 0)) == 0:
                        positions_nocost += 1
                assortment = r.get("assortment", {})
                a_href = assortment.get("meta", {}).get("href", "")
                product_rows.append({
                    "day": day,
                    "store_id": store_id,
                    "assortment_id": _href_id(a_href) if a_href else "unknown",
                    "product_name": assortment.get("name", "?"),
                    "sell_qty": qty,
                    "revenue_kop": sell,
                    "cost_kop": cost,
                    "profit_kop": sell - cost,
                })

            checks = client.count(
                "/entity/demand",
                filter_str=f"store={href};moment>={m_from};moment<={m_to}",
            )

            db.upsert_store_day(conn, {
                "day": day,
                "store_id": store_id,
                "store_name": store_name,
                "channel": channel,
                "revenue_kop": revenue_kop,
                "cost_kop": cost_kop,
                "checks": checks,
                "positions_total": positions_total,
                "positions_nocost": positions_nocost,
            })
            db.replace_products_for_day_store(conn, day, store_id, product_rows)
            total_rows += 1
            log.info(
                "%s | %-22s | выручка=%.0f себест=%.0f чеков=%s поз=%s (без себест=%s)",
                day, store_name, revenue_kop / 100, cost_kop / 100, checks,
                positions_total, positions_nocost,
            )
        conn.commit()

    return total_rows


def run(client: MoyskladClient, conn, d_from: date, d_to: date) -> None:
    started = time.monotonic()
    error = None
    rows = 0
    try:
        rows = sync_sales(client, conn, d_from, d_to)
    except Exception as e:  # noqa: BLE001
        error = str(e)
        log.exception("Ошибка выгрузки продаж")
        raise
    finally:
        db.log_sync(
            conn,
            task="sync_sales",
            period_from=d_from,
            period_to=d_to,
            rows_loaded=rows,
            duration_ms=int((time.monotonic() - started) * 1000),
            ok=error is None,
            error=error,
        )
