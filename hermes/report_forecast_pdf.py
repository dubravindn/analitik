"""PDF-отчёт «Прогноз закупки» — брендовый стиль ЦБД."""
from __future__ import annotations

from datetime import date

from . import calc, pdf_kit as pk

_SENTINEL_QTY = 9999


def _rub(kop: float) -> str:
    return f"{int(kop) // 100:,}".replace(",", " ")


def _qty(q: float) -> str:
    return (
        f"{int(q):,}".replace(",", " ")
        if q == int(q)
        else f"{q:,.1f}".replace(",", " ")
    )


def build_forecast_pdf(conn, date_from: date, date_to: date) -> bytes:
    """PDF «Прогноз закупки» — рекомендации на основе продаж и остатков."""
    period = f"{date_from.strftime('%d.%m')}–{date_to.strftime('%d.%m.%Y')}"
    days = max((date_to - date_from).days + 1, 1)

    asf = calc.assortment_filter("spd.assortment_id")

    # ── 1. Продажи за период (Ассортимент) ───────────────────────────────────
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT spd.assortment_id, spd.product_name, SUM(spd.sell_qty) AS qty
            FROM sales_by_product_day spd
            WHERE spd.day BETWEEN %s AND %s
              AND spd.sell_qty > 0
              AND {asf}
            GROUP BY spd.assortment_id, spd.product_name
        """, [date_from, date_to])
        sales_rows = cur.fetchall()

    sales_by_pid = {r[0]: (r[1], float(r[2] or 0)) for r in sales_rows}

    # ── 2. Последний снимок остатков ─────────────────────────────────────────
    with conn.cursor() as cur:
        cur.execute("SELECT MAX(day) FROM stock_snapshot")
        snap_day = cur.fetchone()[0]

    stock_by_pid: dict[str, float] = {}
    cost_by_pid: dict[str, float] = {}
    if snap_day:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT ss.product_id,
                       SUM(ss.available_qty) AS qty,
                       MAX(ss.cost_price_kop) AS cost
                FROM stock_snapshot ss
                JOIN product_dim pd ON pd.product_id = ss.product_id
                WHERE ss.day = %s
                  AND ss.available_qty > 0 AND ss.available_qty < %s
                  AND pd.folder_path LIKE %s
                GROUP BY ss.product_id
            """, [snap_day, _SENTINEL_QTY, "Ассортимент/%"])
            for pid, qty, cost in cur.fetchall():
                stock_by_pid[pid] = float(qty or 0)
                cost_by_pid[pid] = float(cost or 0)

    # ── 3. Рекомендации: продано × 1.1 − остаток ─────────────────────────────
    recs: list[tuple] = []
    for pid, (pname, sold) in sales_by_pid.items():
        stock = stock_by_pid.get(pid, 0.0)
        rec = sold * 1.1 - stock
        if rec > 0:
            cost = cost_by_pid.get(pid, 0.0)
            recs.append((pname, sold, stock, rec, cost))

    recs.sort(key=lambda x: -x[3])

    # ── 4. Позиции в избытке (остаток > продаж) ───────────────────────────────
    overstock_count = sum(
        1 for pid, (_, sold) in sales_by_pid.items()
        if stock_by_pid.get(pid, 0) > sold
    )

    # ── KPI ──────────────────────────────────────────────────────────────────
    n_to_order = len(recs)
    total_kop = sum(r[3] * r[4] for r in recs if r[4] > 0)

    # ── рендеринг ─────────────────────────────────────────────────────────────
    pdf = pk.HermesPDF(section_title="Прогноз", period=period)
    pdf.add_page()
    pk.cover(pdf, f"Прогноз закупки  ·  {period}")

    pk.kpi_row(pdf, [
        ("Позиций к заказу",   str(n_to_order),       ""),
        ("На сумму",           _rub(total_kop),        "₽"),
        ("Позиций в избытке",  str(overstock_count),   ""),
        ("Период анализа",     str(days),              "дн."),
    ])

    pk.section_header(pdf, "Рекомендации к заказу  ·  продано × 1.1 − остаток")

    if recs:
        pk.table(
            pdf,
            headers=["Название", "Продано", "Остаток", "К заказу"],
            rows=[
                [
                    pname[:46],
                    _qty(sold),
                    _qty(stock),
                    _qty(rec),
                ]
                for pname, sold, stock, rec, _ in recs
            ],
            col_widths=[106, 22, 24, 22],
            aligns=["L", "R", "R", "R"],
            font_size=8.5,
        )
    else:
        pk.callout(pdf, "Всё покрыто остатком — докупать нечего.", kind="ok")

    # Позиции в избытке
    if overstock_count:
        pdf.add_page()
        pk.cover(pdf, "Позиции в избытке")
        pk.section_header(pdf, "Остаток > продаж за период")
        over_rows = [
            (sales_by_pid[pid][0], sales_by_pid[pid][1], stock_by_pid.get(pid, 0))
            for pid in sales_by_pid
            if stock_by_pid.get(pid, 0) > sales_by_pid[pid][1]
        ]
        over_rows.sort(key=lambda x: -(x[2] - x[1]))
        pk.table(
            pdf,
            headers=["Название", "Продано", "Остаток", "Избыток"],
            rows=[
                [pname[:46], _qty(sold), _qty(stock), _qty(stock - sold)]
                for pname, sold, stock in over_rows
            ],
            col_widths=[106, 22, 24, 22],
            aligns=["L", "R", "R", "R"],
            font_size=8.5,
        )

    return bytes(pdf.output())
