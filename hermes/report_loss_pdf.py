"""PDF-отчёт «Списания» — брендовый стиль ЦБД."""
from __future__ import annotations

from datetime import date

from . import calc, config, pdf_kit as pk


def _rub(kop: float) -> str:
    return f"{int(kop) // 100:,}".replace(",", " ")


def _qty(q: float) -> str:
    return (
        f"{int(q):,}".replace(",", " ")
        if q == int(q)
        else f"{q:,.1f}".replace(",", " ")
    )


def build_loss_pdf(conn, date_from: date, date_to: date) -> bytes:
    """PDF «Списания» — обычная порча без корректировок инвентаризации."""
    period = f"{date_from.strftime('%d.%m')}–{date_to.strftime('%d.%m.%Y')}"
    days = max((date_to - date_from).days + 1, 1)
    from .report_inventory import inventory_loss_doc_ids
    excluded_docs = sorted(
        inventory_loss_doc_ids(conn, date_from, date_to)
    ) or ["__none__"]

    # ── скидка ООО «Поставщик» ───────────────────────────────────────────────
    discount_pids = calc.discount_product_ids(conn)
    disc_list = sorted(discount_pids)
    has_disc = bool(disc_list)

    disc_case = (
        " * CASE WHEN i.product_id = ANY(%s::text[]) "
        "THEN 0.93::numeric ELSE 1.0 END"
        if has_disc else ""
    )

    def _dp():
        return [disc_list] if has_disc else []

    _TOTAL = f"""CASE
        WHEN i.product_id IS NOT NULL
             AND pp.price_kop IS NOT NULL AND pp.price_kop > 0
            THEN round(i.qty * pp.price_kop{disc_case})
        ELSE i.total_kop END"""

    _JOIN = """
        FROM loss_doc d
        JOIN loss_item i ON i.doc_id = d.doc_id
        LEFT JOIN LATERAL (
            SELECT price_kop FROM purchase_price_asof p
            WHERE i.product_id IS NOT NULL
              AND p.product_id = i.product_id AND p.priced_from <= d.day
            ORDER BY p.priced_from DESC LIMIT 1
        ) pp ON true
    """

    # ── 1. Итог по складам ───────────────────────────────────────────────────
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT d.store_name,
                   COUNT(DISTINCT d.doc_id),
                   COALESCE(SUM({_TOTAL}), 0) AS loss_kop
            {_JOIN}
            WHERE d.day BETWEEN %s AND %s
              AND NOT (d.doc_id = ANY(%s))
            GROUP BY d.store_name
            ORDER BY loss_kop DESC
        """, _dp() + [date_from, date_to, excluded_docs])
        store_rows = cur.fetchall()

    grand_kop = sum(int(r[2] or 0) for r in store_rows)
    n_stores = len(store_rows)

    # ── 2. Кол-во позиц��й списано ─────────────────��──────────────────────────
    with conn.cursor() as cur:
        cur.execute("""
            SELECT COUNT(*)
            FROM loss_item i
            JOIN loss_doc d ON d.doc_id = i.doc_id
            WHERE d.day BETWEEN %s AND %s
              AND NOT (d.doc_id = ANY(%s))
        """, [date_from, date_to, excluded_docs])
        grand_items = int(cur.fetchone()[0] or 0)

    # ── 3. Позиции документов (дата DESC) ────────────────────────────────────
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT d.day, d.store_name, i.product_name, i.qty,
                   COALESCE({_TOTAL}, 0) AS item_kop
            {_JOIN}
            WHERE d.day BETWEEN %s AND %s
              AND NOT (d.doc_id = ANY(%s))
            ORDER BY d.moment DESC, d.doc_id, i.product_name
        """, _dp() + [date_from, date_to, excluded_docs])
        doc_rows = cur.fetchall()

    # ── 4. Топ-10 позиций по сумме (Ассортимент) ───────────────────────���────
    asof = calc.assortment_filter("i.product_id")
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT i.product_name,
                   SUM(i.qty),
                   COALESCE(SUM({_TOTAL}), 0) AS pos_kop
            {_JOIN}
            WHERE d.day BETWEEN %s AND %s
              AND NOT (d.doc_id = ANY(%s))
              AND {asof}
            GROUP BY i.product_name
            ORDER BY pos_kop DESC
            LIMIT 15
        """, _dp() + [date_from, date_to, excluded_docs])
        top_rows = cur.fetchall()

    # ── рендеринг ───────────���────────────────────────────────────────────────
    avg_per_day = grand_kop / days

    pdf = pk.HermesPDF(section_title="Списания", period=period)
    pdf.add_page()
    pk.cover(pdf, f"Списания  ·  {period}")

    pk.kpi_row(pdf, [
        ("Позиций списано",  str(grand_items),      ""),
        ("Сумма списаний",   _rub(grand_kop),       "₽"),
        ("Складов с порчей", str(n_stores),         ""),
        ("Ср. потеря/день",  _rub(avg_per_day),     "₽"),
    ])

    # Итог по складам
    pk.section_header(pdf, "По складам")
    pk.table(
        pdf,
        headers=["Склад", "Сум��а"],
        rows=[[sn, _rub(int(kop or 0)) + " ₽"] for sn, _cnt, kop in store_rows],
        col_widths=[120, 54],
        aligns=["L", "R"],
    )
    pk.callout(pdf, f"Итого: {_rub(grand_kop)} ₽", kind="info")

    # Топ-15 (сразу после сводки)
    pk.section_header(pdf, "Топ-15 по сумме  ·  Ассортимент")
    if top_rows:
        pk.table(
            pdf,
            headers=["Название", "Кол-во", "Сумма"],
            rows=[
                [name, _qty(float(qty or 0)), _rub(float(kop or 0)) + " ₽"]
                for name, qty, kop in top_rows
            ],
            col_widths=[114, 24, 36],
            aligns=["L", "R", "R"],
        )
    else:
        pk.callout(pdf, "Нет данных по позициям за период.", kind="info")

    # Позиции документов
    pdf.add_page()
    pk.cover(pdf, "Позиции списаний")
    pk.section_header(pdf, "Позиции по документам")
    pk.table(
        pdf,
        headers=["Дата", "Склад", "Название", "Кол-во", "Сумма"],
        rows=[
            [
                doc_day.strftime("%d.%m") if hasattr(doc_day, "strftime") else str(doc_day),
                sn[:22],
                (pname or "")[:34],
                _qty(float(qty or 0)),
                _rub(float(kop or 0)) + " ₽",
            ]
            for doc_day, sn, pname, qty, kop in doc_rows
        ],
        col_widths=[18, 44, 70, 22, 20],
        aligns=["L", "L", "L", "R", "R"],
        font_size=8.5,
    )

    return bytes(pdf.output())
