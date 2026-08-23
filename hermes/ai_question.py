"""Read-only factual context for free-form Telegram business questions.

The model never receives database credentials and never writes SQL.  This
module executes a fixed allow-list of queries, then sends only structured facts
through the same isolated Codex worker used by scheduled report analysis.
"""
from __future__ import annotations

import calendar
import hashlib
import json
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

from . import config
from .analysis_payload import (
    _fact,
    build_current_state_analysis_payload,
    build_period_analysis_payload,
)


_BASE_STORE = "База Воровского 107/1"
_MONTHS = {
    "январ": 1, "феврал": 2, "март": 3, "апрел": 4,
    "май": 5, "мая": 5, "июн": 6, "июл": 7, "август": 8,
    "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12,
}
_STOP_WORDS = {
    "какой", "какая", "какие", "сколько", "почему", "когда", "который",
    "покажи", "скажи", "нужно", "можно", "этот", "этой", "этого", "нашей",
    "сейчас", "сегодня", "вчера", "месяц", "неделя", "период", "товар",
    "клиент", "склад", "продажи", "выручка", "прибыль", "расходы",
}


def _previous_month(day: date) -> tuple[date, date]:
    end = day.replace(day=1) - timedelta(days=1)
    return end.replace(day=1), end


def _has_period_hint(text: str) -> bool:
    norm = text.casefold()
    return bool(
        re.search(r"\d{1,4}[./-]\d{1,2}(?:[./-]\d{2,4})?", norm)
        or any(word in norm for word in (
            "сегодня", "вчера", "недел", "месяц", "квартал", "год",
            "дней", "январ", "феврал", "март", "апрел", "мае", "май",
            "июн", "июл", "август", "сентябр", "октябр", "ноябр", "декабр",
        ))
    )


def parse_question_period(
    text: str, today: date, previous: tuple[date, date] | None = None,
) -> tuple[date, date]:
    """Parse common Russian business periods; default to month-to-date."""
    norm = text.casefold().replace("ё", "е")
    iso = re.findall(r"\b(\d{4})-(\d{2})-(\d{2})\b", norm)
    ru = re.findall(r"\b(\d{1,2})[.]([01]?\d)[.](\d{4})\b", norm)
    parsed: list[date] = []
    for y, m, d in iso:
        try:
            parsed.append(date(int(y), int(m), int(d)))
        except ValueError:
            pass
    for d, m, y in ru:
        try:
            parsed.append(date(int(y), int(m), int(d)))
        except ValueError:
            pass
    if len(parsed) >= 2:
        return min(parsed), max(parsed)
    if len(parsed) == 1:
        return parsed[0], parsed[0]

    if "позавчера" in norm:
        day = today - timedelta(days=2)
        return day, day
    if "вчера" in norm:
        day = today - timedelta(days=1)
        return day, day
    if "сегодня" in norm:
        return today, today
    if "прошл" in norm and "недел" in norm:
        this_monday = today - timedelta(days=today.weekday())
        return this_monday - timedelta(days=7), this_monday - timedelta(days=1)
    if "недел" in norm:
        return today - timedelta(days=today.weekday()), today
    if "прошл" in norm and "месяц" in norm:
        return _previous_month(today)
    if "прошл" in norm and "год" in norm:
        return date(today.year - 1, 1, 1), date(today.year - 1, 12, 31)
    if "год" in norm:
        return date(today.year, 1, 1), today
    match = re.search(r"(?:за|последн\w*)\s+(\d{1,3})\s+д", norm)
    if match:
        days = min(max(int(match.group(1)), 1), 366)
        return today - timedelta(days=days - 1), today

    for stem, month in _MONTHS.items():
        if stem in norm:
            year_match = re.search(r"\b(20\d{2})\b", norm)
            year = int(year_match.group(1)) if year_match else today.year
            start = date(year, month, 1)
            end = date(year, month, calendar.monthrange(year, month)[1])
            if year == today.year and month == today.month:
                end = today
            return start, end

    if previous and not _has_period_hint(text):
        return previous
    return today.replace(day=1), today


def _conversation(conn, chat_id: str, limit: int = 4) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT payload_json->>'question', validated_json->>'answer',
                   payload_json->'period'
            FROM ai_analysis_run
            WHERE report_type = 'question' AND chat_id = %s
              AND mode = 'live' AND status = 'validated' AND validated_json IS NOT NULL
            ORDER BY created_at DESC LIMIT %s
            """,
            (str(chat_id), limit),
        )
        rows = cur.fetchall()
    return [
        {"question": row[0], "answer": row[1], "period": row[2] or {}}
        for row in reversed(rows)
    ]


def _append_source_freshness(conn, facts: list[dict], d_to: date) -> None:
    sources = (
        ("sales_by_store_day", "Продажи", True),
        ("stock_snapshot", "Остатки", True),
        ("loss_doc", "Списания", False),
        ("cashflow_event", "Расходы и платежи", False),
        ("sales_doc", "Клиенты", False),
        ("supply_doc", "Приёмки", False),
        ("move_doc", "Перемещения", False),
        ("product_price", "Цены", True),
    )
    for idx, (table, label, daily_snapshot) in enumerate(sources, 1):
        with conn.cursor() as cur:
            cur.execute(f"SELECT MAX(day) FROM {table}")
            latest = (cur.fetchone() or (None,))[0]
        stale = daily_snapshot and (
            latest is None or latest < min(d_to, config.msk_today() - timedelta(days=1))
        )
        facts.append(_fact(
            f"source.{idx}.latest", "data_quality" if stale else "source_status",
            f"Актуальность: {label}" if daily_snapshot else f"Последний документ: {label}",
            latest, "date",
            details={
                "source_table": table, "stale_for_period": stale,
                "daily_snapshot": daily_snapshot,
            },
        ))


def _append_definitions(facts: list[dict]) -> None:
    definitions = (
        ("gross", "Валовая прибыль", "выручка минус себестоимость"),
        ("before", "Прибыль до списаний", "валовая прибыль минус операционные расходы"),
        ("result", "Прибыль после списаний", "прибыль до списаний минус списания"),
        ("available", "Доступный остаток", "физический остаток минус резерв"),
        ("discount", "Скидка поставщика", "фактическая цена = базовая цена × 0,93; базовая цена = фактическая цена ÷ 0,93"),
        ("move", "Цена перемещения", "количество × цена Наличка на дату документа"),
    )
    for key, label, formula in definitions:
        facts.append(_fact(
            f"definition.{key}", "definition", label, formula, "text",
        ))
    facts.append(_fact(
        "definition.discount.rate", "definition", "Скидка ООО Поставщик",
        round(config.SUPPLIER_DISCOUNT_RATE * 100, 2), "pct",
    ))


def _append_business_breakdowns(conn, facts: list[dict], d_from: date, d_to: date) -> None:
    period = f"{d_from.isoformat()}–{d_to.isoformat()}"
    # Top products by revenue for every business store.
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH ranked AS (
                SELECT s.store_name, p.product_name,
                       SUM(p.sell_qty) qty, SUM(p.revenue_kop) revenue,
                       ROW_NUMBER() OVER (
                           PARTITION BY s.store_name ORDER BY SUM(p.revenue_kop) DESC
                       ) rn
                FROM sales_by_product_day p
                JOIN sales_by_store_day s ON s.day=p.day AND s.store_id=p.store_id
                WHERE p.day BETWEEN %s AND %s
                  AND s.channel = ANY(%s)
                GROUP BY s.store_name, p.product_name
            )
            SELECT store_name, product_name, qty, revenue
            FROM ranked WHERE rn <= 8 ORDER BY store_name, rn
            """,
            (d_from, d_to, list(config.PROFIT_CHANNELS)),
        )
        for idx, (store, product, qty, revenue) in enumerate(cur.fetchall(), 1):
            facts.append(_fact(
                f"product.top.{idx}", "product_sales", f"Выручка товара: {product}",
                revenue, "kop", store=store, period=period,
                details={"product": product, "qty": qty},
            ))

    from . import calc
    from .report_sales_pdf import _get_losses_by_store
    losses = _get_losses_by_store(
        conn, d_from, d_to, discount_pids=calc.discount_product_ids(conn),
    )
    loss_rows = sorted(
        ((store, amount) for store, amount in losses.items() if store != "__total__"),
        key=lambda row: row[1], reverse=True,
    )
    for idx, (store, amount) in enumerate(loss_rows, 1):
        facts.append(_fact(
            f"loss.store.{idx}", "loss_breakdown", "Списания склада",
            amount, "kop", store=store, period=period,
            details={"formula": "методика PDF, включая скидку поставщика"},
        ))

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COALESCE(NULLIF(expense_item_name, ''), 'Без статьи'),
                   SUM(amount_kop), COUNT(*)
            FROM cashflow_event
            WHERE day BETWEEN %s AND %s AND direction='out'
              AND NOT (COALESCE(expense_item_name, '') = ANY(%s))
            GROUP BY 1 ORDER BY 2 DESC LIMIT 12
            """,
            (d_from, d_to, list(config.OWNER_EXPENSE_ITEMS or ["__none__"])),
        )
        for idx, (item, amount, count) in enumerate(cur.fetchall(), 1):
            facts.append(_fact(
                f"expense.item.{idx}", "expense_breakdown", f"Расход: {item}",
                amount, "kop", period=period, details={"documents": count},
            ))

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COALESCE(NULLIF(agent_name, ''), 'Поставщик не указан'),
                   SUM(total_kop), COUNT(*)
            FROM supply_doc WHERE day BETWEEN %s AND %s
            GROUP BY 1 ORDER BY 2 DESC LIMIT 10
            """,
            (d_from, d_to),
        )
        for idx, (supplier, amount, count) in enumerate(cur.fetchall(), 1):
            facts.append(_fact(
                f"supply.agent.{idx}", "supply", f"Приёмки: {supplier}",
                amount, "kop", period=period, details={"documents": count},
            ))

    from .report_move import _MV_ASSORT, _MV_JOIN, _MV_TOTAL
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT d.store_from_name, d.store_to_name, SUM({_MV_TOTAL}), SUM(i.qty)
            {_MV_JOIN}
            WHERE d.day BETWEEN %s AND %s AND {_MV_ASSORT}
            GROUP BY d.store_from_name, d.store_to_name ORDER BY 3 DESC LIMIT 12
        """, (d_from, d_to))
        for idx, (source, target, amount, qty) in enumerate(cur.fetchall(), 1):
            facts.append(_fact(
                f"move.route.{idx}", "movement", f"Перемещение {source} → {target}",
                amount, "kop", period=period, details={"qty": qty, "price_type": "Наличка"},
            ))


def _question_tokens(question: str) -> list[str]:
    words = re.findall(r"[а-яa-z0-9-]{4,}", question.casefold().replace("ё", "е"))
    return [word for word in dict.fromkeys(words) if word not in _STOP_WORDS][:8]


def _append_entity_matches(conn, facts: list[dict], question: str, d_from: date, d_to: date) -> None:
    tokens = _question_tokens(question)
    if not tokens:
        return
    patterns = [f"%{token}%" for token in tokens]
    clauses = " OR ".join("product_name ILIKE %s" for _ in patterns)
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT product_id, product_name FROM product_dim WHERE {clauses} LIMIT 12",
            patterns,
        )
        products = cur.fetchall()
    for idx, (product_id, product) in enumerate(products, 1):
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COALESCE(SUM(sell_qty),0), COALESCE(SUM(revenue_kop),0)
                FROM sales_by_product_day
                WHERE assortment_id=%s AND day BETWEEN %s AND %s
                """,
                (product_id, d_from, d_to),
            )
            qty, revenue = cur.fetchone()
            cur.execute(
                """
                SELECT day, store_name, stock_qty, reserve_qty, available_qty
                FROM stock_snapshot WHERE product_id=%s
                  AND day=(SELECT MAX(day) FROM stock_snapshot)
                ORDER BY store_name
                """,
                (product_id,),
            )
            stock = cur.fetchall()
        product_fact = _fact(
            f"entity.product.{idx}", "entity_product", f"Товар: {product}",
            revenue, "kop", period=f"{d_from.isoformat()}–{d_to.isoformat()}",
            details={"product_id": product_id, "qty": qty, "latest_stock": stock},
        )
        if stock:
            stock_text = "; ".join(
                f"{store}: физ. {physical}, резерв {reserve}, доступно {available}"
                for _day, store, physical, reserve, available in stock
            )
            product_fact["evidence"] += f" · Остаток: {stock_text}"
        facts.append(product_fact)

    client_clauses = " OR ".join("agent_name ILIKE %s" for _ in patterns)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT agent_name, MAX(day), COUNT(*) FILTER (WHERE doc_type='demand'),
                   COALESCE(SUM(sum_kop),0)
            FROM sales_doc WHERE store_name=%s AND ({client_clauses})
            GROUP BY agent_id, agent_name ORDER BY 4 DESC LIMIT 10
            """,
            [_BASE_STORE, *patterns],
        )
        for idx, (client, last_day, orders, amount) in enumerate(cur.fetchall(), 1):
            client_fact = _fact(
                f"entity.client.{idx}", "entity_client", f"Клиент: {client}",
                amount, "kop", store=_BASE_STORE,
                details={"last_order": last_day, "orders": orders},
            )
            client_fact["evidence"] += f" · Последний заказ: {last_day} · Заказов: {orders}"
            facts.append(client_fact)


def _append_recent_report_facts(conn, facts: list[dict]) -> None:
    wanted = (
        (("forecast", "current_state"), "forecast", {"forecast", "forecast_product"}),
        (("current_state",), "state", {"zero_stock"}),
        (("period",), "period", {"document_audit"}),
    )
    for report_types, source_key, categories in wanted:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT payload_json, created_at FROM ai_analysis_run
                WHERE report_type = ANY(%s) AND status='validated'
                ORDER BY created_at DESC LIMIT 1
                """,
                (list(report_types),),
            )
            row = cur.fetchone()
        if not row:
            continue
        payload, created_at = row
        selected = [f for f in (payload.get("facts") or []) if f.get("category") in categories]
        if source_key == "forecast":
            selected.sort(
                key=lambda f: float((f.get("details") or {}).get("recommended_order") or 0),
                reverse=True,
            )
            selected = selected[:80]
        elif source_key == "state":
            selected = selected[:80]
        for idx, fact in enumerate(selected, 1):
            cloned = dict(fact)
            cloned["id"] = f"recent.{source_key}.{idx}"
            if source_key == "state" and cloned.get("category") == "zero_stock":
                cloned["category"] = "catalog_zero_stock"
            cloned["details"] = dict(cloned.get("details") or {})
            cloned["details"]["source_created_at"] = created_at.isoformat()
            entity = cloned["details"].get("product") or cloned["details"].get("document")
            if entity:
                cloned["label"] = f"{cloned.get('label', source_key)}: {entity}"
                cloned["evidence"] = f"{cloned['label']}: {cloned.get('value')}"
            facts.append(cloned)


def build_question_analysis_payload(conn, question: str, chat_id: str) -> dict[str, Any]:
    conversation = _conversation(conn, chat_id)
    previous_period = None
    if conversation and not _has_period_hint(question):
        period = conversation[-1].get("period") or {}
        try:
            previous_period = (date.fromisoformat(period["from"]), date.fromisoformat(period["to"]))
        except (KeyError, TypeError, ValueError):
            previous_period = None

    today = config.msk_today()
    d_from, d_to = parse_question_period(question, today, previous_period)
    with conn.cursor() as cur:
        cur.execute("SELECT MAX(day) FROM sales_by_store_day")
        latest_sales_day = (cur.fetchone() or (None,))[0]
    if (
        latest_sales_day and d_to == today and latest_sales_day < today
        and "сегодня" not in question.casefold()
    ):
        d_to = latest_sales_day
        d_from = min(d_from, d_to)
    base = build_period_analysis_payload(conn, d_from, d_to, client=None)
    facts = list(base["facts"])

    # Current-state facts are DB-only here; no API request and no forecast recalculation.
    state = build_current_state_analysis_payload(conn, token=None)
    state_categories = {"stock", "stock_issue", "zero_stock", "stale_stock"}
    category_counts: dict[str, int] = {}
    for fact in state["facts"]:
        category = fact.get("category")
        if category not in state_categories:
            continue
        category_counts[category] = category_counts.get(category, 0) + 1
        limits = {"stock": 20, "stock_issue": 30, "zero_stock": 60, "stale_stock": 40}
        if category_counts[category] <= limits[category]:
            cloned = dict(fact)
            cloned["id"] = f"question.state.{cloned['id']}"
            facts.append(cloned)

    _append_source_freshness(conn, facts, d_to)
    _append_definitions(facts)
    _append_business_breakdowns(conn, facts, d_from, d_to)
    _append_entity_matches(conn, facts, question, d_from, d_to)
    _append_recent_report_facts(conn, facts)

    period_label = f"{d_from:%d.%m.%Y}–{d_to:%d.%m.%Y}"
    body = {
        "schema_version": 1,
        "report_type": "question",
        "report_id": f"question-{datetime.now(timezone.utc):%Y%m%d%H%M%S%f}",
        "period": {"from": d_from.isoformat(), "to": d_to.isoformat(), "label": period_label},
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "question": question[:1500],
        "conversation": conversation,
        "facts": facts,
    }
    canonical = json.dumps(body, ensure_ascii=False, sort_keys=True, default=str)
    body["payload_hash"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return body
