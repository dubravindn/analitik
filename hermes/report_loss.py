"""Отчёт «Аналитик списаний»: все документы с полным раскрытием позиций."""
from __future__ import annotations

from datetime import date


def _rub(kop: float) -> str:
    return f"{kop / 100:,.0f}".replace(",", " ")


def _qty(q: float) -> str:
    if q == int(q):
        return f"{int(q):,}".replace(",", " ")
    return f"{q:,.1f}".replace(",", " ")


def build_loss_report(conn, d_from: date, d_to: date, store_name: str | None = None) -> str:
    """Полный отчёт по списаниям: каждый документ + все позиции."""
    period_str = (
        d_from.strftime("%d.%m.%Y") if d_from == d_to
        else f"{d_from.strftime('%d.%m.%Y')} – {d_to.strftime('%d.%m.%Y')}"
    )
    store_label = f" · {store_name}" if store_name else " · Все склады"
    lines: list[str] = []
    lines.append(f"🗑 Списания {period_str}{store_label}")
    lines.append("")

    sf = "AND d.store_name = %s" if store_name else ""
    p  = [d_from, d_to] + ([store_name] if store_name else [])

    # Сводка
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT COUNT(DISTINCT d.doc_id),
                   COALESCE(SUM(i.qty), 0),
                   COALESCE(SUM(i.total_kop), 0)
            FROM loss_doc d
            JOIN loss_item i ON i.doc_id = d.doc_id
            WHERE d.day BETWEEN %s AND %s {sf}
        """, p)
        cnt, total_qty, total_kop = cur.fetchone()

    if not cnt:
        lines.append("Данных о списаниях за этот период нет.")
        return "\n".join(lines)

    lines.append(f"📋 Документов: {cnt} · Позиций: {_qty(float(total_qty))} ед. · Сумма: {_rub(float(total_kop))} ₽")
    lines.append("")

    # Разбивка по складам
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT d.store_name,
                   COUNT(DISTINCT d.doc_id),
                   COALESCE(SUM(i.qty), 0),
                   COALESCE(SUM(i.total_kop), 0)
            FROM loss_doc d
            JOIN loss_item i ON i.doc_id = d.doc_id
            WHERE d.day BETWEEN %s AND %s {sf}
            GROUP BY d.store_name
            ORDER BY SUM(i.total_kop) DESC
        """, p)
        by_store = cur.fetchall()

    if not store_name and len(by_store) > 1:
        lines.append("── По складам ──")
        for sn, dcnt, sqty, skop in by_store:
            lines.append(f"  📍 {sn}: {dcnt} докум. · {_qty(float(sqty))} ед. · {_rub(float(skop))} ₽")
        lines.append("")

    # Топ-10 товаров по сумме
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT i.product_name, SUM(i.qty), SUM(i.total_kop)
            FROM loss_doc d
            JOIN loss_item i ON i.doc_id = d.doc_id
            WHERE d.day BETWEEN %s AND %s {sf}
            GROUP BY i.product_name
            ORDER BY SUM(i.total_kop) DESC
            LIMIT 10
        """, p)
        top = cur.fetchall()

    if top:
        lines.append("🏆 Топ-10 по сумме списания:")
        for i, (name, qty, kop) in enumerate(top, 1):
            lines.append(f"  {i:2}. {name}: {_qty(float(qty))} ед. · {_rub(float(kop))} ₽")
        lines.append("")

    # Все документы с позициями
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT d.doc_id, d.moment, d.store_name, d.description
            FROM loss_doc d
            WHERE d.day BETWEEN %s AND %s {sf}
            ORDER BY d.moment
        """, p)
        docs = cur.fetchall()

    lines.append(f"── Все документы ({len(docs)}) ──")
    lines.append("")
    for doc_id, moment, sn, description in docs:
        moment_str = moment.strftime("%d.%m.%Y %H:%M") if hasattr(moment, "strftime") else str(moment)[:16]
        header = f"📅 {moment_str}"
        if not store_name:
            header += f" · {sn}"
        if description:
            header += f" · {description}"
        lines.append(header)

        with conn.cursor() as cur:
            cur.execute("""
                SELECT product_name, qty, cost_kop, total_kop
                FROM loss_item
                WHERE doc_id = %s
                ORDER BY total_kop DESC
            """, [doc_id])
            positions = cur.fetchall()

        doc_total = 0.0
        for pname, qty, cost_kop, total_kop in positions:
            doc_total += float(total_kop)
            cost_str = f" × {_rub(cost_kop)} ₽/ед." if cost_kop else ""
            lines.append(
                f"  • {pname}: {_qty(float(qty))} ед.{cost_str} = {_rub(float(total_kop))} ₽"
            )
        lines.append(f"  Итого: {_qty(float(sum(p[1] for p in positions)))} ед. · {_rub(doc_total)} ₽")
        lines.append("")

    lines.append(f"═══ ИТОГО: {_qty(float(total_qty))} ед. · {_rub(float(total_kop))} ₽ ═══")
    return "\n".join(lines)
