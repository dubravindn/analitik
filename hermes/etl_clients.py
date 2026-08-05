"""ETL клиентских отгрузок: выгружает /entity/demand в таблицу sales_doc."""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone

from .moysklad import MoyskladClient

log = logging.getLogger("hermes.etl_clients")
_PAGE = 100


def _moment_filter(d_from: date, d_to: date) -> str:
    return (
        f"moment>={d_from.isoformat()} 00:00:00;"
        f"moment<={d_to.isoformat()} 23:59:59"
    )


def run(client: MoyskladClient, conn, d_from: date, d_to: date) -> int:
    """Синхронизировать отгрузки за период. Возвращает число документов."""
    docs: list[dict] = []
    offset = 0
    flt = _moment_filter(d_from, d_to)
    while True:
        page = client._get("/entity/demand", {
            "limit": _PAGE,
            "offset": offset,
            "filter": flt,
            "expand": "agent,store",
            "order": "moment,asc",
        })
        batch = page.get("rows", [])
        docs.extend(batch)
        size = page.get("meta", {}).get("size", 0)
        offset += len(batch)
        if offset >= size or not batch:
            break

    log.info("Найдено отгрузок за %s..%s: %d", d_from, d_to, len(docs))

    total = 0
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
        store_id   = store.get("id", "")   if isinstance(store, dict) else ""
        store_name = store.get("name", "") if isinstance(store, dict) else ""

        agent = doc.get("agent", {})
        agent_id   = agent.get("id", "")   if isinstance(agent, dict) else ""
        agent_name = agent.get("name", "") if isinstance(agent, dict) else ""

        positions = doc.get("positions", {})
        pos_count = positions.get("meta", {}).get("size", 0) if isinstance(positions, dict) else 0
        amount_kop = round(doc.get("sum", 0) or 0)

        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO sales_doc
                    (doc_id, moment, day, store_id, store_name,
                     agent_id, agent_name, positions, amount_kop)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (doc_id) DO UPDATE SET
                    moment=EXCLUDED.moment, day=EXCLUDED.day,
                    store_id=EXCLUDED.store_id, store_name=EXCLUDED.store_name,
                    agent_id=EXCLUDED.agent_id, agent_name=EXCLUDED.agent_name,
                    positions=EXCLUDED.positions, amount_kop=EXCLUDED.amount_kop,
                    synced_at=now()
            """, (doc_id, moment, doc_day, store_id, store_name,
                  agent_id, agent_name, pos_count, amount_kop))

        conn.commit()
        total += 1

    log.info("Клиентские отгрузки %s..%s: загружено %d", d_from, d_to, total)
    return total
