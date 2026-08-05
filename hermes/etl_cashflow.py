"""ETL ДДС: выгружает кассовые и банковские платежи из МойСклад.

Типы документов:
  cashin      — приходный кассовый ордер  (in)
  cashout     — расходный кассовый ордер  (out)
  paymentin   — входящий платёж (банк)    (in)
  paymentout  — исходящий платёж (банк)   (out)
"""
from __future__ import annotations

import logging
from datetime import date, datetime

from .moysklad import MoyskladClient
from . import config

log = logging.getLogger("hermes.etl_cashflow")

_PAGE = 100

_DOC_TYPES = [
    ("cashin",     "in"),
    ("cashout",    "out"),
    ("paymentin",  "in"),
    ("paymentout", "out"),
]


def _moment_filter(d_from: date, d_to: date) -> str:
    return (
        f"moment>={d_from.isoformat()} 00:00:00;"
        f"moment<={d_to.isoformat()} 23:59:59"
    )


def _fetch_type(client: MoyskladClient, doc_type: str, d_from: date, d_to: date) -> list[dict]:
    docs: list[dict] = []
    offset = 0
    flt = _moment_filter(d_from, d_to)
    while True:
        page = client._get(f"/entity/{doc_type}", {
            "limit": _PAGE,
            "offset": offset,
            "filter": flt,
            "expand": "agent,expenseItem,project",
            "order": "moment,asc",
        })
        batch = page.get("rows", [])
        docs.extend(batch)
        size = page.get("meta", {}).get("size", 0)
        offset += len(batch)
        if offset >= size or not batch:
            break
    return docs


def run(client: MoyskladClient, conn, d_from: date, d_to: date) -> int:
    """Синхронизировать движение денег за период. Возвращает число событий."""
    total = 0
    for doc_type, direction in _DOC_TYPES:
        docs = _fetch_type(client, doc_type, d_from, d_to)
        log.info("  %s: %d документов", doc_type, len(docs))

        records = []
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

            agent = doc.get("agent", {})
            agent_name = agent.get("name", "") if isinstance(agent, dict) else ""
            description = doc.get("description") or ""
            amount = round(doc.get("sum", 0) or 0)
            expense_item = doc.get("expenseItem", {})
            expense_item_name = expense_item.get("name", "") if isinstance(expense_item, dict) else ""
            project = doc.get("project", {})
            project_name = project.get("name", "") if isinstance(project, dict) else ""
            records.append((doc_id, moment, doc_day, direction, doc_type,
                            agent_name, description, amount, expense_item_name, project_name))

        if records:
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO cashflow_event
                        (event_id, moment, day, direction, doc_type,
                         agent_name, description, amount_kop,
                         expense_item_name, project_name)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (event_id) DO UPDATE SET
                        moment=EXCLUDED.moment, day=EXCLUDED.day,
                        direction=EXCLUDED.direction, doc_type=EXCLUDED.doc_type,
                        agent_name=EXCLUDED.agent_name, description=EXCLUDED.description,
                        amount_kop=EXCLUDED.amount_kop,
                        expense_item_name=EXCLUDED.expense_item_name,
                        project_name=EXCLUDED.project_name,
                        synced_at=now()
                    """,
                    records,
                )
            conn.commit()
        total += len(records)

    log.info("ДДС %s..%s: загружено %d событий", d_from, d_to, total)
    return total
