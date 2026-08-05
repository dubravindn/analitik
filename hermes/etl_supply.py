"""ETL поставок: выгружает входящие поставки /entity/supply из МойСклад."""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone

from .moysklad import MoyskladClient

log = logging.getLogger("hermes.etl_supply")

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
        page = client._get("/entity/supply", {
            "limit": _PAGE,
            "offset": offset,
            "filter": flt,
            "expand": "store,agent",
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
        page = client._get(f"/entity/supply/{doc_id}/positions", {
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
    """Синхронизировать поставки за период. Возвращает число документов."""
    docs = _fetch_docs(client, d_from, d_to)
    log.info("Найдено поставок за %s..%s: %d", d_from, d_to, len(docs))

    total_docs = 0
    for doc in docs:
        doc_id = doc["id"]
        moment_str = doc.get("moment", "")
        try:
            moment = datetime.strptime(moment_str[:19], "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            moment = datetime.now(timezone.utc)
        doc_day = moment.date()

        store = doc.get("store", {})
        store_id = store.get("id", "")
        store_name = store.get("name", "")
        agent = doc.get("agent", {})
        agent_name = agent.get("name", "")
        description = doc.get("description") or ""
        total_kop = round(doc.get("sum", 0) or 0)

        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO supply_doc
                    (doc_id, moment, day, store_id, store_name, agent_name, description, total_kop)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (doc_id) DO UPDATE SET
                    moment=EXCLUDED.moment, day=EXCLUDED.day,
                    store_id=EXCLUDED.store_id, store_name=EXCLUDED.store_name,
                    agent_name=EXCLUDED.agent_name, description=EXCLUDED.description,
                    total_kop=EXCLUDED.total_kop, synced_at=now()
                """,
                (doc_id, moment, doc_day, store_id, store_name, agent_name, description, total_kop),
            )

        positions = _fetch_positions(client, doc_id)
        pos_records = []
        for pos in positions:
            pos_id = pos["id"]
            assort = pos.get("assortment", {})
            product_name = assort.get("name", "")
            qty = float(pos.get("quantity", 0) or 0)
            price = round(pos.get("price", 0) or 0)
            total = round(qty * price)
            pos_records.append((doc_id, pos_id, product_name, qty, price, total))

        if pos_records:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM supply_item WHERE doc_id = %s", (doc_id,))
                cur.executemany(
                    """
                    INSERT INTO supply_item
                        (doc_id, position_id, product_name, qty, price_kop, total_kop)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (doc_id, position_id) DO UPDATE SET
                        product_name=EXCLUDED.product_name, qty=EXCLUDED.qty,
                        price_kop=EXCLUDED.price_kop, total_kop=EXCLUDED.total_kop,
                        synced_at=now()
                    """,
                    pos_records,
                )
        conn.commit()
        total_docs += 1

    log.info("Поставки %s..%s: загружено %d документов", d_from, d_to, total_docs)
    return total_docs
