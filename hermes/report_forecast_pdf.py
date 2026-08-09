"""PDF-отчёт «Прогноз закупки» — брендовый стиль ЦБД."""
from __future__ import annotations

import math
from datetime import date, timedelta

from . import calc, pdf_kit as pk

_SENTINEL_QTY = 9999

# Праздники, важные для цветочного магазина (месяц, день, название)
_HOLIDAYS = [
    (1,  1,  "Новый год"),
    (1,  7,  "Рождество"),
    (2,  14, "День влюблённых"),
    (2,  23, "День защитника Отечества"),
    (3,  8,  "Женский день"),
    (5,  1,  "День труда"),
    (5,  9,  "День Победы"),
    (6,  1,  "День защиты детей"),
    (6,  12, "День России"),
    (9,  1,  "День знаний"),
    (10, 5,  "День учителя"),
    (11, 4,  "День народного единства"),
    (12, 31, "Новогодний вечер"),
]


def _holidays_near(d_from: date, d_to: date, margin: int = 3) -> list[tuple[date, str]]:
    """Праздники в окне [d_from - margin .. d_to + margin]."""
    lo = d_from - timedelta(days=margin)
    hi = d_to   + timedelta(days=margin)
    found: list[tuple[date, str]] = []
    for m, d, name in _HOLIDAYS:
        for year in {lo.year, hi.year}:
            try:
                hday = date(year, m, d)
            except ValueError:
                continue
            if lo <= hday <= hi:
                found.append((hday, name))
    found.sort()
    return found


def _rub(kop: float) -> str:
    return f"{int(kop) // 100:,}".replace(",", " ")


def _qty(q: float) -> str:
    if q == 0:
        return "—"
    return (
        f"{int(q):,}".replace(",", " ")
        if q == int(q)
        else f"{q:,.1f}".replace(",", " ")
    )


def _pct(a: float, b: float) -> str:
    if b == 0:
        return "—"
    sign = "+" if a >= b else ""
    return f"{sign}{(a - b) / b * 100:.0f}%"


def build_forecast_pdf(conn, date_from: date, date_to: date) -> bytes:
    """PDF «Прогноз закупки»: рекомендации + контекст год назад."""
    period = f"{date_from.strftime('%d.%m')}–{date_to.strftime('%d.%m.%Y')}"
    days   = max((date_to - date_from).days + 1, 1)

    asf_spd = calc.assortment_filter("spd.assortment_id")
    asf_si  = calc.assortment_filter("si.product_id")
    asf_ss  = calc.assortment_filter("ss.product_id")

    delta    = timedelta(days=days)
    prev_from = date_from - delta
    prev_to   = date_to   - delta
    yoy_from  = date_from - timedelta(days=365)
    yoy_to    = date_to   - timedelta(days=365)

    # ── продажи текущего периода ──────────────────────────────────────────────
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT spd.assortment_id, spd.product_name, SUM(spd.sell_qty)
            FROM sales_by_product_day spd
            WHERE spd.day BETWEEN %s AND %s
              AND spd.sell_qty > 0
              AND {asf_spd}
            GROUP BY spd.assortment_id, spd.product_name
        """, [date_from, date_to])
        sales_by_pid = {r[0]: (r[1], float(r[2] or 0)) for r in cur.fetchall()}

    # ── продажи прошлой недели ────────────────────────────────────────────────
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT spd.assortment_id, SUM(spd.sell_qty)
            FROM sales_by_product_day spd
            WHERE spd.day BETWEEN %s AND %s
              AND spd.sell_qty > 0
              AND {asf_spd}
            GROUP BY spd.assortment_id
        """, [prev_from, prev_to])
        prev_by_pid = {r[0]: float(r[1] or 0) for r in cur.fetchall()}

    # ── текущий остаток ───────────────────────────────────────────────────────
    with conn.cursor() as cur:
        cur.execute("SELECT MAX(day) FROM stock_snapshot")
        snap_day = cur.fetchone()[0]

    stock_by_pid: dict[str, float] = {}
    cost_by_pid:  dict[str, float] = {}
    srezka_pids:  set[str]         = set()

    if snap_day:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT ss.product_id,
                       SUM(ss.available_qty),
                       MAX(ss.cost_price_kop),
                       MAX(pd.folder_path)
                FROM stock_snapshot ss
                JOIN product_dim pd ON pd.product_id = ss.product_id
                WHERE ss.day = %s
                  AND ss.available_qty > 0 AND ss.available_qty < %s
                  AND pd.folder_path LIKE 'Ассортимент/%%'
                GROUP BY ss.product_id
            """, [snap_day, _SENTINEL_QTY])
            for pid, qty, cost, fpath in cur.fetchall():
                stock_by_pid[pid] = float(qty or 0)
                cost_by_pid[pid]  = float(cost or 0)
                if fpath and "СРЕЗКА" in fpath:
                    srezka_pids.add(pid)

    # ── приёмка год назад (supply_item) ──────────────────────────────────────
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT si.product_id, si.product_name, SUM(si.qty)
            FROM supply_item si
            JOIN supply_doc sd ON sd.doc_id = si.doc_id
            WHERE sd.day BETWEEN %s AND %s
              AND {asf_si}
            GROUP BY si.product_id, si.product_name
        """, [yoy_from, yoy_to])
        yoy_supply: dict[str, tuple[str, float]] = {
            r[0]: (r[1], float(r[2] or 0)) for r in cur.fetchall()
        }

    # ── остаток год назад (ближайший снимок) ─────────────────────────────────
    with conn.cursor() as cur:
        cur.execute("SELECT MAX(day) FROM stock_snapshot WHERE day <= %s", [yoy_to])
        yoy_snap_day = cur.fetchone()[0]

    yoy_stock_by_pid: dict[str, float] = {}
    if yoy_snap_day:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT ss.product_id, SUM(ss.available_qty)
                FROM stock_snapshot ss
                JOIN product_dim pd ON pd.product_id = ss.product_id
                WHERE ss.day = %s
                  AND ss.available_qty > 0 AND ss.available_qty < %s
                  AND pd.folder_path LIKE 'Ассортимент/%%'
                GROUP BY ss.product_id
            """, [yoy_snap_day, _SENTINEL_QTY])
            yoy_stock_by_pid = {r[0]: float(r[1] or 0) for r in cur.fetchall()}

    # ── рекомендации ──────────────────────────────────────────────────────────
    recs: list[tuple] = []
    for pid, (pname, sold) in sales_by_pid.items():
        stock = stock_by_pid.get(pid, 0.0)
        rec   = math.ceil(sold * 1.1 - stock)
        if rec > 0:
            prev = prev_by_pid.get(pid, 0.0)
            cost = cost_by_pid.get(pid, 0.0)
            recs.append((pname, sold, prev, stock, rec, cost))
    recs.sort(key=lambda x: -x[4])

    # ── избыток (только СРЕЗКА) ───────────────────────────────────────────────
    over_rows = sorted(
        [
            (sales_by_pid[pid][0], sales_by_pid[pid][1],
             stock_by_pid.get(pid, 0.0))
            for pid in sales_by_pid
            if pid in srezka_pids and stock_by_pid.get(pid, 0) > sales_by_pid[pid][1]
        ],
        key=lambda x: -(x[2] - x[1]),
    )

    # ── KPI ───────────────────────────────────────────────────────────────────
    n_to_order  = len(recs)
    total_kop   = sum(r[4] * r[5] for r in recs if r[5] > 0)
    total_sold  = sum(v[1] for v in sales_by_pid.values())
    total_prev  = sum(prev_by_pid.values())

    # ── праздники ─────────────────────────────────────────────────────────────
    curr_holidays = _holidays_near(date_from, date_to)
    yoy_holidays  = _holidays_near(yoy_from,  yoy_to)

    def _holiday_text(holidays):
        return "  ·  ".join(
            f"{hd.strftime('%d.%m')} — {name}" for hd, name in holidays
        )

    # ═════════════════════════════════════════════════════════════════════════
    # РЕНДЕРИНГ
    # ═════════════════════════════════════════════════════════════════════════
    pdf = pk.HermesPDF(section_title="Прогноз", period=period)

    # ─── Страница 1: рекомендации ─────────────────────────────────────────────
    pdf.add_page()
    pk.cover(pdf, f"Прогноз закупки  ·  {period}")

    pk.kpi_row(pdf, [
        ("Позиций к заказу",  str(n_to_order),                ""),
        ("На сумму",          _rub(total_kop),                "₽"),
        ("vs прошл. неделя",  _pct(total_sold, total_prev),   ""),
        ("Позиций в избытке", str(len(over_rows)),             ""),
    ])

    if curr_holidays:
        pk.callout(pdf, "Праздники в периоде: " + _holiday_text(curr_holidays), kind="info")

    pk.section_header(pdf, "Рекомендации к заказу  ·  продано × 1.1 − остаток")

    if recs:
        pk.table(
            pdf,
            headers=["Название", "Продано", "Прошл. нед.", "Остаток", "К заказу"],
            rows=[
                [pname[:42], _qty(sold), _qty(prev), _qty(stock), _qty(rec)]
                for pname, sold, prev, stock, rec, _ in recs
            ],
            col_widths=[90, 22, 24, 22, 16],
            aligns=["L", "R", "R", "R", "R"],
            font_size=8.5,
        )
    else:
        pk.callout(pdf, "Всё покрыто остатком — докупать нечего.", kind="ok")

    if over_rows:
        pk.section_header(pdf, "Избыток СРЕЗКА  ·  остаток > продаж")
        pk.table(
            pdf,
            headers=["Название", "Продано", "Остаток", "Избыток"],
            rows=[
                [pname[:70], _qty(sold), _qty(stock), _qty(stock - sold)]
                for pname, sold, stock in over_rows
            ],
            col_widths=[100, 24, 26, 24],
            aligns=["L", "R", "R", "R"],
            font_size=8.5,
        )

    # ─── Страница 2: контекст год назад ──────────────────────────────────────
    pdf.add_page()
    yoy_label = f"{yoy_from.strftime('%d.%m')}–{yoy_to.strftime('%d.%m.%Y')}"
    pk.cover(pdf, f"Год назад  ·  {yoy_label}")

    if yoy_holidays:
        pk.callout(pdf, "Праздники в периоде: " + _holiday_text(yoy_holidays), kind="info")
    else:
        pk.callout(pdf, f"Особых праздников в период {yoy_label} не выявлено.", kind="info")

    pk.section_header(pdf, "Приёмка товара год назад  ·  объём закупки как прокси продаж")

    # Объединяем supply и stock год назад по pid
    yoy_all_pids = set(yoy_supply) | set(yoy_stock_by_pid)
    yoy_rows = []
    for pid in yoy_all_pids:
        sup_name, sup_qty = yoy_supply.get(pid, ("", 0.0))
        yoy_stock = yoy_stock_by_pid.get(pid, 0.0)
        if sup_qty > 0 or yoy_stock > 0:
            name = sup_name or pid
            yoy_rows.append((name, sup_qty, yoy_stock))
    yoy_rows.sort(key=lambda x: -x[1])

    if yoy_rows:
        pk.table(
            pdf,
            headers=["Название", "Принято г.н.", "Остаток г.н."],
            rows=[
                [name[:70], _qty(sup_qty), _qty(yoy_stock)]
                for name, sup_qty, yoy_stock in yoy_rows
            ],
            col_widths=[110, 32, 32],
            aligns=["L", "R", "R"],
            font_size=8.5,
        )
    else:
        pk.callout(pdf, "Данных о приёмках за этот период год назад нет.", kind="info")

    return bytes(pdf.output())
