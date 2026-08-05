"""Отчёт «Помощник по закупкам»: поставки за период, топ поставщиков и товаров."""
from __future__ import annotations

from datetime import date


def _rub(kop: float) -> str:
    return f"{kop / 100:,.0f}".replace(",", " ")


def _qty(q: float) -> str:
    if q == int(q):
        return f"{int(q):,}".replace(",", " ")
    return f"{q:,.1f}".replace(",", " ")


def build_supply_report(conn, d_from: date, d_to: date) -> str:
    period_str = (
        d_from.strftime("%d.%m.%Y")
        if d_from == d_to
        else f"{d_from.strftime('%d.%m.%Y')} – {d_to.strftime('%d.%m.%Y')}"
    )

    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*), COALESCE(SUM(total_kop), 0) FROM supply_doc WHERE day BETWEEN %s AND %s",
            (d_from, d_to),
        )
        row = cur.fetchone()
        doc_count, grand_total = row[0], row[1]

    lines: list[str] = []
    lines.append(f"📦 Закупки за {period_str}")
    lines.append("")

    if not doc_count:
        lines.append("Поставок за этот период нет.")
        return "\n".join(lines)

    lines.append(f"📋 Поставок: {doc_count} · Сумма: {_rub(grand_total)} ₽")
    lines.append("")

    # По складам
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT store_name, COUNT(*) AS docs, SUM(total_kop) AS kop
            FROM supply_doc
            WHERE day BETWEEN %s AND %s
            GROUP BY store_name
            ORDER BY SUM(total_kop) DESC
            """,
            (d_from, d_to),
        )
        store_rows = cur.fetchall()

    if store_rows:
        lines.append("— ПО СКЛАДАМ —")
        for store_name, docs, kop in store_rows:
            lines.append(f"  {store_name}: {docs} пост. · {_rub(kop)} ₽")
        lines.append("")

    # По поставщикам
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COALESCE(agent_name, 'Не указан'), COUNT(*) AS docs, SUM(total_kop) AS kop
            FROM supply_doc
            WHERE day BETWEEN %s AND %s
            GROUP BY agent_name
            ORDER BY SUM(total_kop) DESC
            LIMIT 10
            """,
            (d_from, d_to),
        )
        agent_rows = cur.fetchall()

    if agent_rows:
        lines.append("— ПОСТАВЩИКИ —")
        for agent_name, docs, kop in agent_rows:
            lines.append(f"  {agent_name}: {docs} пост. · {_rub(kop)} ₽")
        lines.append("")

    # Топ-10 товаров по сумме
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT i.product_name, SUM(i.qty) AS qty, SUM(i.total_kop) AS kop
            FROM supply_doc d
            JOIN supply_item i ON i.doc_id = d.doc_id
            WHERE d.day BETWEEN %s AND %s
            GROUP BY i.product_name
            ORDER BY SUM(i.total_kop) DESC
            LIMIT 10
            """,
            (d_from, d_to),
        )
        top_items = cur.fetchall()

    if top_items:
        lines.append("🏆 Топ-10 закупаемых товаров:")
        for idx, (name, qty, kop) in enumerate(top_items, 1):
            lines.append(f"  {idx}. {name}: {_qty(qty)} ед. · {_rub(kop)} ₽")

    return "\n".join(lines)
