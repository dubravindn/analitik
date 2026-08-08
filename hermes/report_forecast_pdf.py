"""PDF-отчёт «Прогноз закупки» — брендовый стиль ЦБД."""
from __future__ import annotations

import math
from datetime import date, timedelta

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
    delta = timedelta(days=days)

    prev_from = date_from - delta
    prev_to   = date_to   - delta
    yoy_from  = date_from - timedelta(days=365)
    yoy_to    = date_to   - timedelta(days=365)

    def _sales_query(d_from, d_to):
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT spd.assortment_id, SUM(spd.sell_qty) AS qty
                FROM sales_by_product_day spd
                WHERE spd.day BETWEEN %s AND %s
                  AND spd.sell_qty > 0
                  AND {asf}
                GROUP BY spd.assortment_id
            """, [d_from, d_to])
            return {r[0]: float(r[1] or 0) for r in cur.fetchall()}

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

    # ── 1b. Предыдущий период и год назад ────────────────────────────────────
    prev_by_pid = _sales_query(prev_from, prev_to)
    yoy_by_pid  = _sales_query(yoy_from, yoy_to)

    # ── 2. Последний снимок остатков ─────────────────────────────────────────
    with conn.cursor() as cur:
        cur.execute("SELECT MAX(day) FROM stock_snapshot")
        snap_day = cur.fetchone()[0]

    stock_by_pid: dict[str, float] = {}
    cost_by_pid: dict[str, float] = {}
    srezka_pids: set[str] = set()
    if snap_day:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT ss.product_id,
                       SUM(ss.available_qty) AS qty,
                       MAX(ss.cost_price_kop) AS cost,
                       MAX(pd.folder_path) AS fpath
                FROM stock_snapshot ss
                JOIN product_dim pd ON pd.product_id = ss.product_id
                WHERE ss.day = %s
                  AND ss.available_qty > 0 AND ss.available_qty < %s
                  AND pd.folder_path LIKE %s
                GROUP BY ss.product_id
            """, [snap_day, _SENTINEL_QTY, "Ассортимент/%"])
            for pid, qty, cost, fpath in cur.fetchall():
                stock_by_pid[pid] = float(qty or 0)
                cost_by_pid[pid] = float(cost or 0)
                if fpath and "СРЕЗКА" in fpath:
                    srezka_pids.add(pid)

    # ── 3. Рекомендации: продано × 1.1 − остаток ─────────────────────────────
    recs: list[tuple] = []
    for pid, (pname, sold) in sales_by_pid.items():
        stock = stock_by_pid.get(pid, 0.0)
        rec = math.ceil(sold * 1.1 - stock)
        prev = prev_by_pid.get(pid, 0.0)
        yoy  = yoy_by_pid.get(pid, 0.0)
        if rec > 0:
            cost = cost_by_pid.get(pid, 0.0)
            recs.append((pname, sold, prev, yoy, stock, rec, cost))

    recs.sort(key=lambda x: -x[5])

    # ── 4. Позиции в избытке (остаток > продаж) — только СРЕЗКА ─────────────
    overstock_count = sum(
        1 for pid, (_, sold) in sales_by_pid.items()
        if pid in srezka_pids and stock_by_pid.get(pid, 0) > sold
    )

    # ── KPI ──────────────────────────────────────────────────────────────────
    n_to_order = len(recs)
    total_kop = sum(r[5] * r[6] for r in recs if r[6] > 0)

    total_sold = sum(v[1] for v in sales_by_pid.values())
    total_prev = sum(prev_by_pid.values())
    total_yoy  = sum(yoy_by_pid.values())

    def _pct(a, b):
        if b == 0:
            return "—"
        sign = "+" if a >= b else ""
        return f"{sign}{(a - b) / b * 100:.0f}%"

    # ── рендеринг ─────────────────────────────────────────────────────────────
    pdf = pk.HermesPDF(section_title="Прогноз", period=period)
    pdf.add_page()
    pk.cover(pdf, f"Прогноз закупки  ·  {period}")

    pk.kpi_row(pdf, [
        ("Позиций к заказу",   str(n_to_order),                  ""),
        ("На сумму",           _rub(total_kop),                   "₽"),
        ("vs прошл. неделя",   _pct(total_sold, total_prev),      ""),
        ("vs год назад",       _pct(total_sold, total_yoy),       ""),
    ])
    pk.kpi_row(pdf, [
        ("Позиций в избытке",  str(overstock_count),              ""),
        ("Период анализа",     str(days),                         "дн."),
        ("Прошл. период",      f"{prev_from.strftime('%d.%m')}–{prev_to.strftime('%d.%m')}",  ""),
        ("Год назад",          f"{yoy_from.strftime('%d.%m')}–{yoy_to.strftime('%d.%m.%Y')}", ""),
    ])

    pk.section_header(pdf, "Рекомендации к заказу  ·  продано × 1.1 − остаток")

    if recs:
        pk.table(
            pdf,
            headers=["Название", "Продано", "Нед.назад", "Год назад", "Остаток", "К заказу"],
            rows=[
                [
                    pname[:40],
                    _qty(sold),
                    _qty(prev),
                    _qty(yoy),
                    _qty(stock),
                    _qty(rec),
                ]
                for pname, sold, prev, yoy, stock, rec, _ in recs
            ],
            col_widths=[80, 18, 20, 20, 18, 18],
            aligns=["L", "R", "R", "R", "R", "R"],
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
            (
                sales_by_pid[pid][0],
                sales_by_pid[pid][1],
                prev_by_pid.get(pid, 0.0),
                yoy_by_pid.get(pid, 0.0),
                stock_by_pid.get(pid, 0),
            )
            for pid in sales_by_pid
            if pid in srezka_pids and stock_by_pid.get(pid, 0) > sales_by_pid[pid][1]
        ]
        over_rows.sort(key=lambda x: -(x[4] - x[1]))
        pk.table(
            pdf,
            headers=["Название", "Продано", "Нед.назад", "Год назад", "Остаток", "Избыток"],
            rows=[
                [
                    pname[:40],
                    _qty(sold),
                    _qty(prev),
                    _qty(yoy),
                    _qty(stock),
                    _qty(stock - sold),
                ]
                for pname, sold, prev, yoy, stock in over_rows
            ],
            col_widths=[80, 18, 20, 20, 18, 18],
            aligns=["L", "R", "R", "R", "R", "R"],
            font_size=8.5,
        )

    return bytes(pdf.output())
