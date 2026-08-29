"""Безопасная выгрузка агрегатов для закрытого управленческого дашборда.

Модуль не обращается к МойСклад и ничего не меняет в PostgreSQL. Он повторно
использует проверенную методику PDF, формирует небольшой JSON и отправляет его в
закрытое хранилище дашборда по секретному серверному каналу.
"""
from __future__ import annotations

import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from . import config

_MONTHS = (
    "", "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)


def period_label(d_from: date, d_to: date) -> str:
    if d_from == d_to:
        return f"{d_from.day} {_MONTHS[d_from.month]} {d_from.year}"
    if d_from.year == d_to.year and d_from.month == d_to.month:
        return f"{d_from.day}–{d_to.day} {_MONTHS[d_to.month]} {d_to.year}"
    return (
        f"{d_from.day} {_MONTHS[d_from.month]} {d_from.year} – "
        f"{d_to.day} {_MONTHS[d_to.month]} {d_to.year}"
    )


def _paginate_moysklad(client, path: str, params: dict | None = None) -> list[dict]:
    rows: list[dict] = []
    offset = 0
    while True:
        page_params = dict(params or {})
        page_params.update({"limit": 1000, "offset": offset})
        page = client._get(path, page_params)
        batch = page.get("rows", [])
        rows.extend(batch)
        offset += len(batch)
        size = int(page.get("meta", {}).get("size", 0) or 0)
        if not batch or offset >= size:
            return rows


def _counterparty_report(client) -> list[dict]:
    """Отчёт большой, поэтому читаем пять страниц параллельно в фоне."""
    first = client._get("/report/counterparty", {"limit": 1000, "offset": 0})
    rows = list(first.get("rows", []))
    size = int(first.get("meta", {}).get("size", 0) or 0)
    offsets = list(range(1000, size, 1000))
    if not offsets:
        return rows

    def fetch(offset: int) -> list[dict]:
        return client._get(
            "/report/counterparty", {"limit": 1000, "offset": offset},
        ).get("rows", [])

    with ThreadPoolExecutor(max_workers=3) as pool:
        for batch in pool.map(fetch, offsets):
            rows.extend(batch)
    return rows


def _live_b2b_state(conn) -> dict[str, Any]:
    """Текущая дебиторка и незакрытые заказы БАЗЫ из read-only API."""
    from .moysklad import MoyskladClient

    excluded_agents = list(config.RETAIL_PLACEHOLDER_AGENTS or [""])
    excluded_agents += list(config.INTERNAL_AGENTS or [""])
    base_store = config.BASE_CASHFLOW_WRITEOFF_STORE
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT agent_id, agent_name
            FROM sales_doc
            WHERE store_name = %s
              AND agent_id IS NOT NULL AND agent_id != ''
              AND NOT (agent_name = ANY(%s))
            """,
            (base_store, excluded_agents),
        )
        base_agents = {str(agent_id): str(name or "Контрагент не указан")
                       for agent_id, name in cur.fetchall()}

    client = MoyskladClient(config.MOYSKLAD_TOKEN())
    counterparty_rows = _counterparty_report(client)
    debtors: list[dict[str, Any]] = []
    for row in counterparty_rows:
        counterparty = row.get("counterparty") or {}
        meta = counterparty.get("meta") or {}
        agent_id = str(meta.get("id") or counterparty.get("id") or "")
        balance = int(round(float(row.get("balance", 0) or 0)))
        if agent_id not in base_agents or balance <= 0:
            continue
        debtors.append({
            "name": str(counterparty.get("name") or base_agents[agent_id]),
            "balanceKop": balance,
            "lastDemandDate": str(row.get("lastDemandDate") or "")[:10],
            "demandsCount": int(row.get("demandsCount", 0) or 0),
            "demandsSumKop": int(round(float(row.get("demandsSum", 0) or 0))),
        })
    debtors.sort(key=lambda row: row["balanceKop"], reverse=True)

    base_store_id = next(
        store["id"] for store in config.STORES if store["name"] == base_store
    )
    cutoff = config.msk_today() - timedelta(days=365)
    orders = _paginate_moysklad(
        client,
        "/entity/customerorder",
        {
            "filter": (
                f"store={client.store_href(base_store_id)};"
                f"moment>={cutoff.isoformat()} 00:00:00"
            ),
            "order": "moment,desc",
            "expand": "agent,state",
        },
    )
    open_orders: list[dict[str, Any]] = []
    for order in orders:
        state = order.get("state") or {}
        if state.get("stateType") == "Unsuccessful" or not order.get("applicable", True):
            continue
        total = int(round(float(order.get("sum", 0) or 0)))
        shipped = int(round(float(order.get("shippedSum", 0) or 0)))
        remaining = max(0, total - shipped)
        if remaining <= 1:
            continue
        payed = int(round(float(order.get("payedSum", 0) or 0)))
        agent = order.get("agent") or {}
        open_orders.append({
            "number": str(order.get("name") or "—"),
            "moment": str(order.get("moment") or "")[:10],
            "client": str(agent.get("name") or "Контрагент не указан"),
            "state": str(state.get("name") or "Статус не указан"),
            "sumKop": total,
            "remainingToShipKop": remaining,
            "unpaidKop": max(0, total - payed),
            "reservedKop": int(round(float(order.get("reservedSum", 0) or 0))),
        })
    open_orders.sort(key=lambda row: row["remainingToShipKop"], reverse=True)

    return {
        "asOf": config.msk_now().isoformat(),
        "receivables": {
            "totalKop": sum(row["balanceKop"] for row in debtors),
            "clients": len(debtors),
            "rows": debtors[:30],
            "rule": "положительный balance отчёта по контрагентам МойСклад",
        },
        "openOrders": {
            "orders": len(open_orders),
            "sumKop": sum(row["sumKop"] for row in open_orders),
            "remainingToShipKop": sum(
                row["remainingToShipKop"] for row in open_orders
            ),
            "unpaidKop": sum(row["unpaidKop"] for row in open_orders),
            "rows": open_orders[:30],
        },
    }


def _latest_ai_summary(conn) -> dict[str, Any]:
    """Последняя уже проверенная сводка действующего ИИ-аналитика."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, report_type, report_id, validated_json, completed_at
            FROM ai_analysis_run
            WHERE status = 'validated'
              AND report_type IN ('daily', 'period')
              AND validated_json IS NOT NULL
            ORDER BY completed_at DESC NULLS LAST, created_at DESC
            LIMIT 1
            """
        )
        row = cur.fetchone()
    if not row:
        return {"available": False}
    run_id, report_type, report_id, validated, completed_at = row
    if isinstance(validated, str):
        validated = json.loads(validated)
    findings = []
    for item in (validated or {}).get("findings", [])[:5]:
        findings.append({
            "severity": str(item.get("severity") or "info"),
            "title": str(item.get("title") or "Без названия"),
            "evidence": str(item.get("evidence") or ""),
            "whyItMatters": str(item.get("why_it_matters") or ""),
            "action": str(item.get("action") or ""),
        })
    warnings = [
        str(item.get("title") or "")
        for item in (validated or {}).get("data_warnings", [])[:3]
        if item.get("title")
    ]
    return {
        "available": True,
        "runId": str(run_id),
        "reportType": str(report_type),
        "reportId": str(report_id),
        "completedAt": completed_at.isoformat() if completed_at else "",
        "findings": findings,
        "warnings": warnings,
    }


def build_dashboard_snapshot(
    conn, d_from: date, d_to: date, *, generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Сформировать только агрегаты, которые нужны главному экрану."""
    from . import calc
    from .report_clients import get_churn_clients, get_top_clients
    from .report_sales_pdf import _period_metrics

    discount_pids = calc.discount_product_ids(conn)
    metrics = _period_metrics(conn, d_from, d_to, None, discount_pids)
    period_days = (d_to - d_from).days + 1
    previous_to = d_from - timedelta(days=1)
    previous_from = previous_to - timedelta(days=period_days - 1)
    previous = _period_metrics(
        conn, previous_from, previous_to, None, discount_pids,
    )

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT store_name, COALESCE(SUM(revenue_kop), 0)
            FROM sales_by_store_day
            WHERE day BETWEEN %s AND %s
              AND channel = ANY(%s)
              AND store_name = ANY(%s)
            GROUP BY store_name
            ORDER BY 2 DESC
            """,
            (d_from, d_to, list(config.PROFIT_CHANNELS), config.INVENTORY_STORES),
        )
        revenue_rows = cur.fetchall()

    revenue_by_store = {str(name): int(value or 0) for name, value in revenue_rows}
    stores = [
        {"name": name, "revenueKop": revenue_by_store.get(name, 0)}
        for name in config.INVENTORY_STORES
    ]
    stores.sort(key=lambda row: row["revenueKop"], reverse=True)

    excluded_agents = list(config.RETAIL_PLACEHOLDER_AGENTS or [""])
    excluded_agents += list(config.INTERNAL_AGENTS or [""])
    base_store = config.BASE_CASHFLOW_WRITEOFF_STORE
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(DISTINCT agent_id),
                   COUNT(*) FILTER (WHERE doc_type = 'demand'),
                   COALESCE(SUM(sum_kop), 0)
            FROM sales_doc
            WHERE day BETWEEN %s AND %s
              AND store_name = %s
              AND agent_id IS NOT NULL AND agent_id != ''
              AND NOT (agent_name = ANY(%s))
            """,
            (d_from, d_to, base_store, excluded_agents),
        )
        clients_count, client_orders, client_revenue = cur.fetchone()
    top_clients = get_top_clients(conn, d_from, d_to, base_store, limit=12)
    churn_clients = get_churn_clients(
        conn, limit=50, store_name=base_store, inactive_days=10,
        min_avg_check_kop=1_000_000,
    )

    moment = generated_at or datetime.now(config.MSK)
    try:
        live_b2b = {"available": True, **_live_b2b_state(conn)}
    except Exception as exc:  # Дашборд продаж не должен падать из-за API
        live_b2b = {
            "available": False,
            "error": f"{type(exc).__name__}: read-only API временно недоступен",
        }
    return {
        "schemaVersion": 1,
        "generatedAt": moment.isoformat(),
        "period": {
            "from": d_from.isoformat(),
            "to": d_to.isoformat(),
            "label": period_label(d_from, d_to),
        },
        "metrics": {
            "revenueKop": int(metrics["rev"]),
            "grossProfitKop": int(metrics["profit"]),
            "profitBeforeWriteoffsKop": int(metrics["before"]),
            "profitAfterWriteoffsKop": int(metrics["result"]),
            "writeoffsKop": int(metrics["loss"]),
            "operatingExpensesKop": int(metrics["op_expenses"]),
            "checks": int(metrics["checks"]),
        },
        "stores": stores,
        "comparison": {
            "label": "Предыдущий период такой же длины",
            "period": {
                "from": previous_from.isoformat(),
                "to": previous_to.isoformat(),
            },
            "metrics": {
                "revenueKop": int(previous["rev"]),
                "grossProfitKop": int(previous["profit"]),
                "profitBeforeWriteoffsKop": int(previous["before"]),
                "profitAfterWriteoffsKop": int(previous["result"]),
                "writeoffsKop": int(previous["loss"]),
                "operatingExpensesKop": int(previous["op_expenses"]),
                "checks": int(previous["checks"]),
            },
        },
        "clients": {
            "summary": {
                "clients": int(clients_count or 0),
                "orders": int(client_orders or 0),
                "revenueKop": int(client_revenue or 0),
            },
            "top": [
                {
                    "name": str(name or "Контрагент не указан"),
                    "orders": int(orders or 0),
                    "revenueKop": int(revenue or 0),
                }
                for name, orders, revenue in top_clients
            ],
            "churn": [
                {
                    "name": str(name or "Контрагент не указан"),
                    "lastOrder": last_day.isoformat(),
                    "daysSince": int(days_since or 0),
                    "orders": int(orders or 0),
                    "revenueKop": int(revenue or 0),
                    "averageCheckKop": int(average_check or 0),
                }
                for (
                    name, last_day, days_since, orders, revenue, average_check,
                ) in churn_clients
            ],
        },
        "liveB2b": live_b2b,
        "aiSummary": _latest_ai_summary(conn),
    }


def post_dashboard_snapshot(url: str, token: str, payload: dict[str, Any]) -> dict:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    request = Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
    )
    with urlopen(request, timeout=30) as response:  # noqa: S310 - URL is admin config
        return json.loads(response.read().decode("utf-8"))


def main() -> None:
    from . import db

    yesterday = date.today() - timedelta(days=1)
    parser = argparse.ArgumentParser(description="Обновить закрытый дашборд")
    parser.add_argument("--from", dest="d_from", type=date.fromisoformat,
                        default=yesterday - timedelta(days=6))
    parser.add_argument("--to", dest="d_to", type=date.fromisoformat,
                        default=yesterday)
    parser.add_argument("--url")
    parser.add_argument("--token")
    parser.add_argument("--output", type=Path,
                        help="сохранить JSON вместо отправки")
    args = parser.parse_args()

    if args.d_from > args.d_to:
        parser.error("дата --from должна быть не позже --to")
    target_url = args.url or config.get("DASHBOARD_URL")
    target_token = args.token or config.get("DASHBOARD_SYNC_TOKEN")
    if target_url and not target_url.rstrip("/").endswith("/api/sync"):
        target_url = target_url.rstrip("/") + "/api/sync"
    if not args.output and not (target_url and target_token):
        parser.error("нужны --url и --token либо --output")
    conn = db.connect(config.DATABASE_URL())
    try:
        snapshot = build_dashboard_snapshot(conn, args.d_from, args.d_to)
    finally:
        conn.close()
    if args.output:
        args.output.write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    else:
        result = post_dashboard_snapshot(target_url, target_token, snapshot)
        if not result.get("ok"):
            raise RuntimeError(f"дашборд отклонил обновление: {result}")
    print(f"Дашборд обновлён: {snapshot['period']['label']}")


if __name__ == "__main__":
    main()
