"""ETL клиентских документов: /entity/demand (отгрузки) и /entity/salesreturn
(возвраты) в таблицу sales_doc. Возвраты пишутся с отрицательным sum_kop —
суммы по клиенту становятся нетто, а детектор оттока считает только по demand.
"""
from __future__ import annotations

import logging
from datetime import date, datetime

from .moysklad import MoyskladClient
from . import config

log = logging.getLogger("hermes.etl_clients")
_PAGE = 100


def _moment_filter(d_from: date, d_to: date) -> str:
    return (
        f"moment>={d_from.isoformat()} 00:00:00;"
        f"moment<={d_to.isoformat()} 23:59:59"
    )


def _fetch(client: MoyskladClient, entity: str, d_from: date, d_to: date) -> list[dict]:
    docs: list[dict] = []
    offset = 0
    flt = _moment_filter(d_from, d_to)
    while True:
        page = client._get(f"/entity/{entity}", {
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
    return docs


def _upsert(conn, doc: dict, doc_type: str, sign: int) -> None:
    """Записать один документ. sign=+1 для demand, -1 для возврата (нетто-суммы)."""
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
    store_id   = store.get("id", "")   if isinstance(store, dict) else ""
    store_name = store.get("name", "") if isinstance(store, dict) else ""
    channel    = config.STORE_CHANNELS.get(store_id, "")

    agent = doc.get("agent", {})
    agent_id   = agent.get("id", "")   if isinstance(agent, dict) else ""
    agent_name = agent.get("name", "") if isinstance(agent, dict) else ""

    sum_kop = sign * round(doc.get("sum", 0) or 0)

    positions = doc.get("positions", {})
    pos_count = positions.get("meta", {}).get("size", 0) if isinstance(positions, dict) else 0

    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO sales_doc
                (doc_id, moment, day, store_id, channel,
                 agent_id, agent_name, sum_kop,
                 store_name, positions, doc_type)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (doc_id) DO UPDATE SET
                moment=EXCLUDED.moment, day=EXCLUDED.day,
                store_id=EXCLUDED.store_id, channel=EXCLUDED.channel,
                agent_id=EXCLUDED.agent_id, agent_name=EXCLUDED.agent_name,
                sum_kop=EXCLUDED.sum_kop,
                store_name=EXCLUDED.store_name, positions=EXCLUDED.positions,
                doc_type=EXCLUDED.doc_type,
                synced_at=now()
        """, (doc_id, moment, doc_day, store_id, channel,
              agent_id, agent_name, sum_kop, store_name, pos_count, doc_type))
    conn.commit()


def run(client: MoyskladClient, conn, d_from: date, d_to: date) -> int:
    """Синхронизировать отгрузки и возвраты за период. Возвращает число документов."""
    demands = _fetch(client, "demand", d_from, d_to)
    returns = _fetch(client, "salesreturn", d_from, d_to)
    log.info("Найдено за %s..%s: отгрузок %d, возвратов %d",
             d_from, d_to, len(demands), len(returns))

    total = 0
    for doc in demands:
        _upsert(conn, doc, "demand", +1)
        total += 1
    for doc in returns:
        _upsert(conn, doc, "salesreturn", -1)
        total += 1

    log.info("Клиентские документы %s..%s: загружено %d (отгрузки+возвраты)",
             d_from, d_to, total)
    return total
