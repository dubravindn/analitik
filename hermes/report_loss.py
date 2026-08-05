"""Отчёт «Аналитик списаний»: все документы с полным раскрытием позиций."""
from __future__ import annotations

from datetime import date


def _rub(kop: float) -> str:
    return f"{kop / 100:,.0f}".replace(",", " ")


def _qty(q: float) -> str:
    if q == int(q):
        return f"{int(q):,}".replace(",", " ")
    return f"{q:,.1f}".replace(",", " ")


# Стоимость списаний — по закупочной цене из карточки товара (блок G), на дату
# документа. Фолбэк на cost_kop МойСклад для позиций без цены.
_LS_JOIN = """
    FROM loss_doc d
    JOIN loss_item i ON i.doc_id = d.doc_id
    LEFT JOIN LATERAL (
        SELECT price_kop FROM purchase_price_asof p
        WHERE p.product_id = i.product_id AND p.priced_from <= d.day
        ORDER BY p.priced_from DESC LIMIT 1
    ) pp ON true
"""
_LS_TOTAL = ("CASE WHEN pp.price_kop IS NOT NULL AND pp.price_kop > 0 "
             "THEN round(i.qty * pp.price_kop) ELSE i.total_kop END")


def build_loss_report(conn, d_from: date, d_to: date, store_name: str | None = None,
                       max_docs: int | None = 50) -> str:
    """Полный отчёт по списаниям: каждый документ + все позиции.

    max_docs ограничивает число выводимых документов (последние N) — защита от
    лимита Telegram 4096. В PDF передаём max_docs=None (показать все).
    """
    period_str = (
        d_from.strftime("%d.%m.%Y") if d_from == d_to
        else f"{d_from.strftime('%d.%m.%Y')} – {d_to.strftime('%d.%m.%Y')}"
    )
    store_label = f" · {store_name}" if store_name else " · Все склады"
    lines: list[str] = []
    lines.append(f"🗑 Списания {period_str}{store_label}")
    lines.append("Стоимость — по закупочным ценам из карточки товара.")

    sf = "AND d.store_name = %s" if store_name else ""
    p  = [d_from, d_to] + ([store_name] if store_name else [])

    # Сводка
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT COUNT(DISTINCT d.doc_id),
                   COALESCE(SUM(i.qty), 0),
                   COALESCE(SUM({_LS_TOTAL}), 0)
            {_LS_JOIN}
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
                   COALESCE(SUM({_LS_TOTAL}), 0)
            {_LS_JOIN}
            WHERE d.day BETWEEN %s AND %s {sf}
            GROUP BY d.store_name
            ORDER BY SUM({_LS_TOTAL}) DESC
        """, p)
        by_store = cur.fetchall()

    if not store_name and len(by_store) > 1:
        lines.append("── По складам ──")
        for sn, dcnt, sqty, skop in by_store:
            lines.append(f"  📍 {sn}: {dcnt} докум. · {_qty(float(sqty))} ед. · {_rub(float(skop))} ₽")
        lines.append("")

    # Разбивка по проектам (если есть)
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT COALESCE(NULLIF(d.project_name, ''), 'Без проекта'),
                   COUNT(DISTINCT d.doc_id),
                   COALESCE(SUM(i.qty), 0),
                   COALESCE(SUM({_LS_TOTAL}), 0)
            {_LS_JOIN}
            WHERE d.day BETWEEN %s AND %s {sf}
            GROUP BY 1
            ORDER BY 4 DESC
        """, p)
        by_project = cur.fetchall()

    if len(by_project) > 1:
        lines.append("── По проектам ──")
        for proj, dcnt, sqty, skop in by_project:
            lines.append(f"  📁 {proj}: {dcnt} докум. · {_qty(float(sqty))} ед. · {_rub(float(skop))} ₽")
        lines.append("")

    # Топ-10 товаров по сумме
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT i.product_name, SUM(i.qty), SUM({_LS_TOTAL})
            {_LS_JOIN}
            WHERE d.day BETWEEN %s AND %s {sf}
            GROUP BY i.product_name
            ORDER BY SUM({_LS_TOTAL}) DESC
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
            SELECT d.doc_id, d.moment, d.day, d.store_name, d.description, d.project_name
            FROM loss_doc d
            WHERE d.day BETWEEN %s AND %s {sf}
            ORDER BY d.moment
        """, p)
        docs = cur.fetchall()

    total_docs = len(docs)
    if max_docs is not None and total_docs > max_docs:
        docs = docs[-max_docs:]   # последние N (самые свежие)
        lines.append(f"── Документы: показаны {max_docs} из {total_docs} "
                     f"(полный список — в PDF) ──")
    else:
        lines.append(f"── Все документы ({total_docs}) ──")
    lines.append("")
    for doc_id, moment, doc_day, sn, description, project_name in docs:
        moment_str = moment.strftime("%d.%m.%Y %H:%M") if hasattr(moment, "strftime") else str(moment)[:16]
        header = f"📅 {moment_str}"
        if not store_name:
            header += f" · {sn}"
        if project_name:
            header += f" · [{project_name}]"
        if description:
            header += f" · {description}"
        lines.append(header)

        # Цена позиции — закупочная из карточки на дату документа (фолбэк cost_kop).
        with conn.cursor() as cur:
            cur.execute("""
                SELECT i.product_name, i.qty, i.cost_kop, i.total_kop, pp.price_kop
                FROM loss_item i
                LEFT JOIN LATERAL (
                    SELECT price_kop FROM purchase_price_asof p
                    WHERE p.product_id = i.product_id AND p.priced_from <= %s
                    ORDER BY p.priced_from DESC LIMIT 1
                ) pp ON true
                WHERE i.doc_id = %s
                ORDER BY i.total_kop DESC
            """, [doc_day, doc_id])
            positions = cur.fetchall()

        doc_total = 0.0
        for pname, qty, ms_cost, ms_total, purch in positions:
            covered = purch is not None and purch > 0
            unit = int(purch) if covered else int(ms_cost or 0)
            pos_total = round(float(qty) * unit) if covered else float(ms_total)
            doc_total += float(pos_total)
            cost_str = f" × {_rub(unit)} ₽/ед." if unit else ""
            lines.append(
                f"  • {pname}: {_qty(float(qty))} ед.{cost_str} = {_rub(float(pos_total))} ₽"
            )
        lines.append(f"  Итого: {_qty(float(sum(p[1] for p in positions)))} ед. · {_rub(doc_total)} ₽")
        lines.append("")

    # total_qty/total_kop — из сводного запроса (не затирать переменной цикла!)
    lines.append(f"═══ ИТОГО: {_qty(float(total_qty))} ед. · {_rub(float(total_kop))} ₽ ═══")
    return "\n".join(lines)
