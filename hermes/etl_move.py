"""ETL перемещений: выгружает документы /entity/move из МойСклад в PostgreSQL.

Перемещение — движение товара со склада-источника на склад-получатель.
Канал «ресторан» (СОБРАНИЕ) работает именно через перемещения, поэтому без
этих данных цифры по СОБРАНИЮ недостоверны.
"""
from __future__ import annotations

import logging
from datetime import date, datetime

from .moysklad import MoyskladClient
from . import config

log = logging.getLogger("hermes.etl_move")

_PAGE = 100


def _moment_filter(d_from: date, d_to: date) -> str:
    return (
        f"moment>={d_from.isoformat()} 00:00:00;"
        f"moment<={d_to.isoformat()} 23:59:59"
    )


def _fetch_docs(client: MoyskladClient, d_from: date, d_to: date) -> list[dict]:
    docs: list[dict] = []
    offset = 0
    flt = _moment_filter(d_from, d_to)
    while True:
        page = client._get("/entity/move", {
            "limit": _PAGE,
            "offset": offset,
            "filter": flt,
            "expand": "sourceStore,targetStore",
            "order": "moment,asc",
        })
        batch = page.get("rows", [])
        docs.extend(batch)
        size = page.get("meta", {}).get("size", 0)
        offset += len(batch)
        if offset >= size or not batch:
            break
    return docs


def _fetch_positions(client: MoyskladClient, doc_id: str) -> list[dict]:
    rows: list[dict] = []
    offset = 0
    while True:
        page = client._get(f"/entity/move/{doc_id}/positions", {
            "limit": 100,
            "offset": offset,
            "expand": "assortment",
        })
        batch = page.get("rows", [])
        rows.extend(batch)
        size = page.get("meta", {}).get("size", 0)
        offset += len(batch)
        if offset >= size or not batch:
            break
    return rows


def run(client: MoyskladClient, conn, d_from: date, d_to: date) -> int:
    """Синхронизировать перемещения за период. Возвращает число документов."""
    docs = _fetch_docs(client, d_from, d_to)
    log.info("Найдено перемещений за %s..%s: %d", d_from, d_to, len(docs))

    total_docs = 0
    for doc in docs:
        doc_id = doc["id"]
        moment_str = doc.get("moment", "")
        # moment приходит как "2026-07-01 12:34:00.000" без timezone
        try:
            moment = datetime.strptime(moment_str[:19], "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=config.MSK
            )
        except ValueError:
            moment = config.msk_now()
        doc_day = moment.date()

        src = doc.get("sourceStore", {}) or {}
        dst = doc.get("targetStore", {}) or {}
        store_from_id = src.get("id", "") if isinstance(src, dict) else ""
        store_from_name = src.get("name", "") if isinstance(src, dict) else ""
        store_to_id = dst.get("id", "") if isinstance(dst, dict) else ""
        store_to_name = dst.get("name", "") if isinstance(dst, dict) else ""
        description = doc.get("description") or ""
        total_kop = round(doc.get("sum", 0) or 0)

        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO move_doc (doc_id, moment, day,
                    store_from_id, store_from_name, store_to_id, store_to_name,
                    description, total_kop)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (doc_id) DO UPDATE SET
                    moment=EXCLUDED.moment, day=EXCLUDED.day,
                    store_from_id=EXCLUDED.store_from_id,
                    store_from_name=EXCLUDED.store_from_name,
                    store_to_id=EXCLUDED.store_to_id,
                    store_to_name=EXCLUDED.store_to_name,
                    description=EXCLUDED.description,
                    total_kop=EXCLUDED.total_kop,
                    synced_at=now()
                """,
                (doc_id, moment, doc_day, store_from_id, store_from_name,
                 store_to_id, store_to_name, description, total_kop),
            )

        positions = _fetch_positions(client, doc_id)
        pos_records = []
        for pos in positions:
            pos_id = pos["id"]
            assort = pos.get("assortment", {}) or {}
            # id берём чистым (без хвоста ?expand=…), иначе join сломается (урок D)
            product_id = (assort.get("id", "") if isinstance(assort, dict) else "").split("?")[0]
            product_name = assort.get("name", "") if isinstance(assort, dict) else ""
            qty = float(pos.get("quantity", 0) or 0)
            cost = round(pos.get("price", 0) or 0)   # price = себест. в копейках
            total = round(qty * cost)
            pos_records.append((doc_id, pos_id, product_id, product_name, qty, cost, total))

        if pos_records:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM move_item WHERE doc_id = %s", (doc_id,))
                cur.executemany(
                    """
                    INSERT INTO move_item
                        (doc_id, position_id, product_id, product_name, qty, cost_kop, total_kop)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (doc_id, position_id) DO UPDATE SET
                        product_id=EXCLUDED.product_id,
                        product_name=EXCLUDED.product_name,
                        qty=EXCLUDED.qty, cost_kop=EXCLUDED.cost_kop,
                        total_kop=EXCLUDED.total_kop, synced_at=now()
                    """,
                    pos_records,
                )
        conn.commit()
        total_docs += 1

    log.info("Перемещения %s..%s: загружено %d документов", d_from, d_to, total_docs)
    return total_docs
