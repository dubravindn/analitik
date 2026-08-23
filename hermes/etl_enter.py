"""ETL оприходований: выгружает документы /entity/enter из МойСклад в PostgreSQL.

Оприходование — это «+»-сторона учёта: товар ставится на склад (в т.ч. при
инвентаризации, когда фактический остаток оказался больше учётного). Вместе со
списаниями (loss) даёт полную картину инвентаризационной корректировки Базы (J2).
Зеркало etl_loss: тот же формат заголовков и позиций.
"""
from __future__ import annotations

import logging
from datetime import date, datetime

from .moysklad import MoyskladClient
from . import config

log = logging.getLogger("hermes.etl_enter")

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
        page = client._get("/entity/enter", {
            "limit": _PAGE,
            "offset": offset,
            "filter": flt,
            "expand": "store,project",
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
        page = client._get(f"/entity/enter/{doc_id}/positions", {
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
    """Синхронизировать оприходования за период. Возвращает число документов."""
    docs = _fetch_docs(client, d_from, d_to)
    log.info("Найдено оприходований за %s..%s: %d", d_from, d_to, len(docs))

    total_docs = 0
    for doc in docs:
        doc_id = doc["id"]
        moment_str = doc.get("moment", "")
        try:
            moment = datetime.strptime(moment_str[:19], "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=config.MSK
            )
        except ValueError:
            moment = config.msk_now()
        doc_day = moment.date()

        store = doc.get("store", {})
        store_id = store.get("id", "")
        store_name = store.get("name", "")
        description = doc.get("description") or ""
        project = doc.get("project", {})
        project_name = project.get("name", "") if isinstance(project, dict) else ""

        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO enter_doc (doc_id, moment, day, store_id, store_name, description, project_name)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (doc_id) DO UPDATE SET
                    moment=EXCLUDED.moment, day=EXCLUDED.day,
                    store_id=EXCLUDED.store_id, store_name=EXCLUDED.store_name,
                    description=EXCLUDED.description,
                    project_name=EXCLUDED.project_name,
                    synced_at=now()
                """,
                (doc_id, moment, doc_day, store_id, store_name, description, project_name),
            )

        positions = _fetch_positions(client, doc_id)
        pos_records = []
        for pos in positions:
            pos_id = pos["id"]
            assort = pos.get("assortment", {})
            product_id = (assort.get("id", "") if isinstance(assort, dict) else "").split("?")[0]
            product_name = assort.get("name", "")
            folder_meta = assort.get("productFolder", {}).get("meta", {}).get("href", "")
            folder_path = ""
            if folder_meta:
                folder_path = folder_meta.split("/entity/productfolder/")[-1]

            qty = float(pos.get("quantity", 0) or 0)
            price = round(pos.get("price", 0) or 0)
            total = round(qty * price)
            pos_records.append((doc_id, pos_id, product_id, product_name, folder_path, qty, price, total))

        with conn.cursor() as cur:
            cur.execute("DELETE FROM enter_item WHERE doc_id = %s", (doc_id,))
            if pos_records:
                cur.executemany(
                    """
                    INSERT INTO enter_item
                        (doc_id, position_id, product_id, product_name, folder_path, qty, cost_kop, total_kop)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (doc_id, position_id) DO UPDATE SET
                        product_id=EXCLUDED.product_id,
                        product_name=EXCLUDED.product_name,
                        folder_path=EXCLUDED.folder_path,
                        qty=EXCLUDED.qty, cost_kop=EXCLUDED.cost_kop,
                        total_kop=EXCLUDED.total_kop, synced_at=now()
                    """,
                    pos_records,
                )
        conn.commit()
        total_docs += 1

    seen_ids = [doc["id"] for doc in docs]
    with conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM enter_doc
            WHERE day BETWEEN %s AND %s
              AND NOT (doc_id = ANY(%s::text[]))
            """,
            (d_from, d_to, seen_ids),
        )
    conn.commit()

    log.info("Оприходования %s..%s: загружено %d документов", d_from, d_to, total_docs)
    return total_docs
