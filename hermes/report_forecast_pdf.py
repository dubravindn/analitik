"""PDF-отчёт «Прогноз закупки» — брендовый стиль ЦБД."""
from __future__ import annotations

import math
from datetime import date, timedelta

from . import calc, pdf_kit as pk
from .pdf_kit import (
    _MARGIN, INK, SAGE, SAGE_L, CREAM, TERRA, GRID,
)

_WHITE        = (255, 255, 255)
_SENTINEL_QTY = 9999
_YOY_STORE    = "Воровского"   # часть названия склада для фильтра год назад

# Праздники, важные для цветочного магазина
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
    lo, hi = d_from - timedelta(days=margin), d_to + timedelta(days=margin)
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


def _holiday_text(holidays: list) -> str:
    return "  ·  ".join(f"{hd.strftime('%d.%m')} — {name}" for hd, name in holidays)


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


def _over(sold: float, stock: float) -> str:
    """Избыток: остаток − продажи/приёмка, если > 0."""
    v = stock - sold
    return _qty(v) if v > 0 else "—"


def _stock_on(conn, snap_to: date, store_filter: str | None = None) -> dict[str, float]:
    """Остатки из stock_snapshot на ближайшую дату ≤ snap_to."""
    store_cond = "AND ss.store_name ILIKE %s" if store_filter else ""
    params_day: list = [snap_to]
    if store_filter:
        params_day.append(f"%{store_filter}%")
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT MAX(day) FROM stock_snapshot WHERE day <= %s {store_cond}",
            params_day,
        )
        snap_day = cur.fetchone()[0]
    if not snap_day:
        return {}
    params: list = [snap_day, _SENTINEL_QTY]
    if store_filter:
        params.append(f"%{store_filter}%")
    store_cond2 = "AND ss.store_name ILIKE %s" if store_filter else ""
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT ss.product_id, SUM(ss.available_qty)
            FROM stock_snapshot ss
            JOIN product_dim pd ON pd.product_id = ss.product_id
            WHERE ss.day = %s
              AND ss.available_qty > 0 AND ss.available_qty < %s
              AND pd.folder_path LIKE 'Ассортимент/%%'
              {store_cond2}
            GROUP BY ss.product_id
        """, params)
        return {r[0]: float(r[1] or 0) for r in cur.fetchall()}


def _group_header_row(
    pdf: pk.HermesPDF,
    groups: list[tuple[str, float, tuple, tuple]],
) -> None:
    """Строка групповых заголовков: [(label, width, fill_rgb, text_rgb), ...]."""
    pdf.set_x(_MARGIN)
    pdf.set_font("DejaVu_B", size=7.5)
    for label, w, fill, text_color in groups:
        pdf.set_fill_color(*fill)
        pdf.set_text_color(*text_color)
        pdf.cell(w, 5.0, label, border=1, align="C", fill=True)
    pdf.ln()
    pdf.set_text_color(*INK)


def build_forecast_pdf(conn, date_from: date, date_to: date) -> bytes:
    """PDF «Прогноз закупки»: блок заказа (стр. 1) + аналитика (стр. 2)."""
    period = f"{date_from.strftime('%d.%m')}–{date_to.strftime('%d.%m.%Y')}"
    days   = max((date_to - date_from).days + 1, 1)

    asf_spd = calc.assortment_filter("spd.assortment_id")
    asf_si  = calc.assortment_filter("si.product_id")

    delta     = timedelta(days=days)
    prev_from = date_from - delta
    prev_to   = date_to   - delta
    yoy_from  = date_from - timedelta(days=365)
    yoy_to    = date_to   - timedelta(days=365)

    # ── эта неделя: продажи ───────────────────────────────────────────────────
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT spd.assortment_id, spd.product_name, SUM(spd.sell_qty)
            FROM sales_by_product_day spd
            WHERE spd.day BETWEEN %s AND %s
              AND spd.sell_qty > 0
              AND {asf_spd}
            GROUP BY spd.assortment_id, spd.product_name
        """, [date_from, date_to])
        curr_sales: dict[str, tuple[str, float]] = {
            r[0]: (r[1], float(r[2] or 0)) for r in cur.fetchall()
        }

    # ── прошлая неделя: продажи ───────────────────────────────────────────────
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT spd.assortment_id, SUM(spd.sell_qty)
            FROM sales_by_product_day spd
            WHERE spd.day BETWEEN %s AND %s
              AND spd.sell_qty > 0
              AND {asf_spd}
            GROUP BY spd.assortment_id
        """, [prev_from, prev_to])
        prev_sales: dict[str, float] = {r[0]: float(r[1] or 0) for r in cur.fetchall()}

    # ── год назад: приёмка (склад Воровского) ────────────────────────────────
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT si.product_id, si.product_name, SUM(si.qty)
            FROM supply_item si
            JOIN supply_doc sd ON sd.doc_id = si.doc_id
            WHERE sd.day BETWEEN %s AND %s
              AND sd.store_name ILIKE %s
              AND {asf_si}
            GROUP BY si.product_id, si.product_name
        """, [yoy_from, yoy_to, f"%{_YOY_STORE}%"])
        yoy_supply: dict[str, tuple[str, float]] = {
            r[0]: (r[1], float(r[2] or 0)) for r in cur.fetchall()
        }

    # ── остатки трёх периодов ─────────────────────────────────────────────────
    curr_stock = _stock_on(conn, date_to,  store_filter=None)
    prev_stock = _stock_on(conn, prev_to,  store_filter=None)
    yoy_stock  = _stock_on(conn, yoy_to,   store_filter=_YOY_STORE)

    # ── себестоимость для KPI «на сумму» ──────────────────────────────────────
    cost_by_pid: dict[str, float] = {}
    with conn.cursor() as cur:
        cur.execute("""
            SELECT product_id, MAX(cost_price_kop)
            FROM stock_snapshot
            WHERE day = (SELECT MAX(day) FROM stock_snapshot)
              AND available_qty < %s
            GROUP BY product_id
        """, [_SENTINEL_QTY])
        for pid, cost in cur.fetchall():
            cost_by_pid[pid] = float(cost or 0)

    # ── рекомендации к заказу ─────────────────────────────────────────────────
    # Формула: max(эта_нед_продажи, прошлая_нед_продажи) × 1.1 − текущий_остаток
    all_pids = set(curr_sales) | set(prev_sales)
    recs: list[tuple] = []
    for pid in all_pids:
        pname = curr_sales[pid][0] if pid in curr_sales else ""
        c_sold = curr_sales[pid][1] if pid in curr_sales else 0.0
        p_sold = prev_sales.get(pid, 0.0)
        base   = max(c_sold, p_sold)
        stock  = curr_stock.get(pid, 0.0)
        rec    = math.ceil(base * 1.1 - stock)
        if rec > 0:
            cost = cost_by_pid.get(pid, 0.0)
            recs.append((pname, rec, cost))
    recs.sort(key=lambda x: -x[1])

    # ── KPI ───────────────────────────────────────────────────────────────────
    n_order   = len(recs)
    total_kop = sum(r[1] * r[2] for r in recs if r[2] > 0)

    # ── праздники ─────────────────────────────────────────────────────────────
    curr_hols = _holidays_near(date_from, date_to)
    yoy_hols  = _holidays_near(yoy_from,  yoy_to)

    # ═════════════════════════════════════════════════════════════════════════
    # РЕНДЕРИНГ
    # ═════════════════════════════════════════════════════════════════════════
    pdf = pk.HermesPDF(section_title="Прогноз", period=period)

    # ─── Страница 1: список к заказу ─────────────────────────────────────────
    pdf.add_page()
    pk.cover(pdf, f"К заказу на следующую неделю  ·  {period}")

    pk.kpi_row(pdf, [
        ("Позиций к заказу", str(n_order),     ""),
        ("На сумму",         _rub(total_kop),  "₽"),
        ("Период анализа",   str(days),         "дн."),
        ("Склад год назад",  "Розница/База Воровского",  ""),
    ])

    if curr_hols:
        pk.callout(pdf, "Праздники: " + _holiday_text(curr_hols), kind="info")

    pk.section_header(pdf, "К заказу  ·  max(эта нед., прошл. нед.) × 1.1 − остаток")

    if recs:
        pk.table(
            pdf,
            headers=["Название", "К заказу", "На сумму"],
            rows=[
                [
                    pname[:72],
                    _qty(float(rec)),
                    _rub(rec * cost) + " ₽" if cost else "—",
                ]
                for pname, rec, cost in recs
            ],
            col_widths=[110, 32, 32],
            aligns=["L", "R", "R"],
            font_size=8.5,
        )
    else:
        pk.callout(pdf, "Заказывать нечего — остатки покрывают спрос.", kind="ok")

    # ─── Страница 2: аналитика трёх периодов ─────────────────────────────────
    pdf.add_page()
    pk.cover(pdf, "Аналитика трёх периодов")

    hol_parts = []
    if curr_hols:
        hol_parts.append(f"Эта неделя: {_holiday_text(curr_hols)}")
    if yoy_hols:
        hol_parts.append(f"Год назад: {_holiday_text(yoy_hols)}")
    if hol_parts:
        pk.callout(pdf, "  ·  ".join(hol_parts), kind="info")

    prev_label = f"{prev_from.strftime('%d.%m')}–{prev_to.strftime('%d.%m')}"
    yoy_label  = f"{yoy_from.strftime('%d.%m')}–{yoy_to.strftime('%d.%m.%Y')}"
    curr_label = f"{date_from.strftime('%d.%m')}–{date_to.strftime('%d.%m')}"

    pk.section_header(
        pdf,
        f"Прошл. нед. {prev_label}  ·  Год назад {yoy_label} (Воровского)  ·  Эта нед. {curr_label}",
    )

    # Групповые заголовки
    _group_header_row(pdf, [
        ("Название",         48, CREAM,  INK),
        (f"Прошл. нед. {prev_label}", 42, SAGE_L, INK),
        (f"Год назад {yoy_label[:5]}", 42, TERRA,  _WHITE),
        (f"Эта нед. {curr_label}",    42, INK,    _WHITE),
    ])

    # Строки аналитики
    analytics_pids = set(curr_sales) | set(prev_sales) | set(yoy_supply)
    a_rows = []
    for pid in analytics_pids:
        pname = (
            curr_sales[pid][0] if pid in curr_sales
            else yoy_supply[pid][0] if pid in yoy_supply
            else pid
        )
        c_sold = curr_sales.get(pid, ("", 0.0))[1] if pid in curr_sales else 0.0
        p_sold = prev_sales.get(pid, 0.0)
        y_sup  = yoy_supply[pid][1] if pid in yoy_supply else 0.0
        p_ost  = prev_stock.get(pid, 0.0)
        y_ost  = yoy_stock.get(pid, 0.0)
        c_ost  = curr_stock.get(pid, 0.0)
        if c_sold == 0 and p_sold == 0 and y_sup == 0:
            continue
        a_rows.append((
            pname,
            p_sold, p_ost, _over(p_sold, p_ost),
            y_sup,  y_ost, _over(y_sup,  y_ost),
            c_sold, c_ost, _over(c_sold, c_ost),
        ))
    a_rows.sort(key=lambda x: -(x[7] + x[1]))  # сумма продаж обеих недель

    pk.table(
        pdf,
        headers=[
            "Название",
            "Прод", "Ост", "Изб",
            "Прин", "Ост", "Изб",
            "Прод", "Ост", "Изб",
        ],
        rows=[
            [
                pname[:30],
                _qty(p_sold), _qty(p_ost), p_izb,
                _qty(y_sup),  _qty(y_ost), y_izb,
                _qty(c_sold), _qty(c_ost), c_izb,
            ]
            for pname, p_sold, p_ost, p_izb,
                       y_sup,  y_ost, y_izb,
                       c_sold, c_ost, c_izb in a_rows
        ],
        col_widths=[48, 14, 14, 14, 14, 14, 14, 14, 14, 14],
        aligns=["L", "R", "R", "R", "R", "R", "R", "R", "R", "R"],
        font_size=7.5,
    )

    return bytes(pdf.output())
