"""Отчёт «Аналитик списаний»: топ позиций, разбивка по складам, итоги за период."""
from __future__ import annotations

from datetime import date


def _rub(kop: float) -> str:
    return f"{kop / 100:,.0f}".replace(",", " ")


def _qty(q: float) -> str:
    if q == int(q):
        return f"{int(q):,}".replace(",", " ")
    return f"{q:,.1f}".replace(",", " ")


def build_loss_report(conn, d_from: date, d_to: date) -> str:
    period_str = (
        d_from.strftime("%d.%m.%Y")
        if d_from == d_to
        else f"{d_from.strftime('%d.%m.%Y')} – {d_to.strftime('%d.%m.%Y')}"
    )

    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM loss_doc WHERE day BETWEEN %s AND %s", (d_from, d_to)
        )
        doc_count = cur.fetchone()[0]

    lines: list[str] = []
    lines.append(f"🗑 Списания за {period_str}")
    lines.append("")

    if not doc_count:
        lines.append("Данных о списаниях за этот период нет.")
        return "\n".join(lines)

    # Итоги по складам
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT d.store_name,
                   COUNT(DISTINCT d.doc_id) AS docs,
                   SUM(i.qty)               AS total_qty,
                   SUM(i.total_kop)         AS total_kop
            FROM loss_doc d
            JOIN loss_item i ON i.doc_id = d.doc_id
            WHERE d.day BETWEEN %s AND %s
            GROUP BY d.store_name
            ORDER BY SUM(i.total_kop) DESC
            """,
            (d_from, d_to),
        )
        store_rows = cur.fetchall()

    grand_kop = sum(r[3] for r in store_rows)
    grand_docs = sum(r[1] for r in store_rows)

    lines.append(
        f"📋 Документов: {grand_docs} · Сумма: {_rub(grand_kop)} ₽"
    )
    lines.append("")
    lines.append("— ПО СКЛАДАМ —")
    for store_name, docs, qty, kop in store_rows:
        lines.append(
            f"  {store_name}: {docs} докум. · {_qty(qty)} ед. · {_rub(kop)} ₽"
        )
    lines.append("")

    # Топ-10 товаров по сумме списания
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT i.product_name,
                   SUM(i.qty)       AS total_qty,
                   SUM(i.total_kop) AS total_kop
            FROM loss_doc d
            JOIN loss_item i ON i.doc_id = d.doc_id
            WHERE d.day BETWEEN %s AND %s
            GROUP BY i.product_name
            ORDER BY SUM(i.total_kop) DESC
            LIMIT 10
            """,
            (d_from, d_to),
        )
        top_items = cur.fetchall()

    if top_items:
        lines.append("🏆 Топ-10 по сумме списания:")
        for i, (name, qty, kop) in enumerate(top_items, 1):
            lines.append(f"  {i}. {name}: {_qty(qty)} ед. · {_rub(kop)} ₽")

    return "\n".join(lines)
