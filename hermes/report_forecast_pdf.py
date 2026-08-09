"""PDF-отчёт «Прогноз закупки СРЕЗКА» — forecast_v8, брендовый стиль ЦБД."""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import date, timedelta

from . import calc, pdf_kit as pk
from .pdf_kit import _MARGIN, INK, SAGE, SAGE_L, CREAM, TERRA, GRID

_WHITE        = (255, 255, 255)
_GRAY         = (180, 180, 180)
_SENTINEL_QTY = 9999

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


def _subgroup(folder_path: str) -> str:
    """'Ассортимент/СРЕЗКА/Розы/Роза 40' → 'Розы'"""
    parts = (folder_path or "").split("/")
    return parts[2] if len(parts) >= 3 else (parts[-1] if parts else "Другое")


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
    v = stock - sold
    return _qty(v) if v > 0 else "—"


def _stock_on(conn, snap_to: date, store_filter: str | None = None) -> dict[str, float]:
    """Остатки СРЕЗКА из stock_snapshot на ближайшую дату ≤ snap_to."""
    sc_day = "AND store_name ILIKE %s" if store_filter else ""
    p_day: list = [snap_to] + ([f"%{store_filter}%"] if store_filter else [])
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT MAX(day) FROM stock_snapshot WHERE day <= %s AND is_srezka = TRUE {sc_day}",
            p_day,
        )
        snap_day = cur.fetchone()[0]
    if not snap_day:
        return {}
    sc_ss = "AND store_name ILIKE %s" if store_filter else ""
    p_ss: list = [snap_day, _SENTINEL_QTY] + ([f"%{store_filter}%"] if store_filter else [])
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT product_id, SUM(available_qty)
            FROM stock_snapshot
            WHERE day = %s
              AND is_srezka = TRUE
              AND available_qty > 0 AND available_qty < %s
              {sc_ss}
            GROUP BY product_id
        """, p_ss)
        return {r[0]: float(r[1] or 0) for r in cur.fetchall()}


def _group_hdr(
    pdf: pk.HermesPDF,
    groups: list[tuple[str, float, tuple, tuple]],
) -> None:
    """Строка групповых заголовков: [(label, width, fill_rgb, text_rgb), ...]."""
    pdf.set_x(_MARGIN)
    pdf.set_font("DejaVu_B", size=7.5)
    for label, w, fill, txt in groups:
        pdf.set_fill_color(*fill)
        pdf.set_text_color(*txt)
        pdf.cell(w, 5.0, label, border=1, align="C", fill=True)
    pdf.ln()
    pdf.set_text_color(*INK)


def build_forecast_pdf(conn, date_from: date, date_to: date) -> bytes:
    """PDF «Прогноз закупки СРЕЗКА» — forecast_v8."""
    period = f"{date_from.strftime('%d.%m')}–{date_to.strftime('%d.%m.%Y')}"
    days   = max((date_to - date_from).days + 1, 1)

    delta     = timedelta(days=days)
    prev_from = date_from - delta
    prev_to   = date_to   - delta

    # ── Справочник СРЕЗКА: pid → (name, folder_path) ─────────────────────────
    with conn.cursor() as cur:
        cur.execute("""
            SELECT product_id, product_name, folder_path
            FROM product_dim
            WHERE is_srezka = TRUE
        """)
        srezka_info: dict[str, tuple[str, str]] = {
            r[0]: (r[1], r[2] or "") for r in cur.fetchall()
        }
    srezka_pids = set(srezka_info)

    if not srezka_pids:
        pdf = pk.HermesPDF(section_title="Прогноз", period=period)
        pdf.add_page()
        pk.cover(pdf, f"Прогноз закупки  ·  {period}")
        pk.callout(pdf, "Нет товаров с is_srezka = TRUE в product_dim.", kind="info")
        return bytes(pdf.output())

    srezka_list = list(srezka_pids)

    asf_spd = calc.assortment_filter("spd.assortment_id")

    # ── эта неделя: продажи СРЕЗКА ────────────────────────────────────────────
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT spd.assortment_id, SUM(spd.sell_qty)
            FROM sales_by_product_day spd
            WHERE spd.day BETWEEN %s AND %s
              AND spd.sell_qty > 0
              AND {asf_spd}
              AND spd.assortment_id = ANY(%s)
            GROUP BY spd.assortment_id
        """, [date_from, date_to, srezka_list])
        curr_sales: dict[str, float] = {r[0]: float(r[1] or 0) for r in cur.fetchall()}

    # ── прошлая неделя: продажи СРЕЗКА ───────────────────────────────────────
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT spd.assortment_id, SUM(spd.sell_qty)
            FROM sales_by_product_day spd
            WHERE spd.day BETWEEN %s AND %s
              AND spd.sell_qty > 0
              AND {asf_spd}
              AND spd.assortment_id = ANY(%s)
            GROUP BY spd.assortment_id
        """, [prev_from, prev_to, srezka_list])
        prev_sales: dict[str, float] = {r[0]: float(r[1] or 0) for r in cur.fetchall()}

    # ── среднее в приёмке: ООО Поставщик, все склады, вся история ────────────
    with conn.cursor() as cur:
        cur.execute("""
            SELECT si.product_id,
                   ROUND(SUM(si.qty)::numeric / NULLIF(COUNT(DISTINCT sd.doc_id), 0))
            FROM supply_item si
            JOIN supply_doc sd ON sd.doc_id = si.doc_id
            WHERE sd.agent_name ILIKE '%%поставщик%%'
              AND si.product_id = ANY(%s)
            GROUP BY si.product_id
        """, [srezka_list])
        avg_supply: dict[str, float] = {r[0]: float(r[1] or 0) for r in cur.fetchall()}

    # ── остатки двух периодов ─────────────────────────────────────────────────
    curr_stock = _stock_on(conn, date_to,  store_filter=None)
    prev_stock = _stock_on(conn, prev_to,  store_filter=None)

    # ── себестоимость для «На сумму» ──────────────────────────────────────────
    cost_by_pid: dict[str, float] = {}
    with conn.cursor() as cur:
        cur.execute("""
            SELECT product_id, MAX(cost_price_kop)
            FROM stock_snapshot
            WHERE day = (SELECT MAX(day) FROM stock_snapshot)
              AND is_srezka = TRUE
              AND available_qty < %s
            GROUP BY product_id
        """, [_SENTINEL_QTY])
        for pid, cost in cur.fetchall():
            cost_by_pid[pid] = float(cost or 0)

    # ── рекомендации к заказу ─────────────────────────────────────────────────
    recs: list[tuple] = []   # (pname, folder_path, rec, cost)
    for pid in srezka_pids:
        c_sold = curr_sales.get(pid, 0.0)
        p_sold = prev_sales.get(pid, 0.0)
        base   = max(c_sold, p_sold)
        if base == 0:
            continue
        stock = curr_stock.get(pid, 0.0)
        rec   = math.ceil(base * 1.1 - stock)
        if rec > 0:
            pname, fpath = srezka_info[pid]
            cost = cost_by_pid.get(pid, 0.0)
            recs.append((pname, fpath, rec, cost))

    # Сортировка: folder_path ASC, внутри группы — rec DESC
    recs.sort(key=lambda x: (x[1], -x[2]))

    # ── аналитика: строки (СРЕЗКА, есть продажи хотя бы в одну неделю) ───────
    a_rows: list[tuple] = []  # (pname, fpath, p_sold, p_ost, avg, c_sold, c_ost)
    for pid in srezka_pids:
        c_sold = curr_sales.get(pid, 0.0)
        p_sold = prev_sales.get(pid, 0.0)
        if c_sold == 0 and p_sold == 0:
            continue
        pname, fpath = srezka_info[pid]
        p_ost = prev_stock.get(pid, 0.0)
        avg   = avg_supply.get(pid, 0.0)
        c_ost = curr_stock.get(pid, 0.0)
        a_rows.append((pname, fpath, p_sold, p_ost, avg, c_sold, c_ost))

    # Сортировка: folder_path ASC, внутри — (c_sold+p_sold) DESC
    a_rows.sort(key=lambda x: (x[1], -(x[5] + x[2])))

    # ── KPI (только для cover) ────────────────────────────────────────────────
    n_order   = len(recs)
    total_kop = sum(r[2] * r[3] for r in recs if r[3] > 0)

    curr_hols = _holidays_near(date_from, date_to)

    prev_lbl = f"{prev_from.strftime('%d.%m')}–{prev_to.strftime('%d.%m')}"
    curr_lbl = f"{date_from.strftime('%d.%m')}–{date_to.strftime('%d.%m')}"

    # ═══════════════════════════════════════════════════════════════════════════
    # РЕНДЕРИНГ
    # ═══════════════════════════════════════════════════════════════════════════
    pdf = pk.HermesPDF(section_title="Прогноз СРЕЗКА", period=period)

    # ─── Страница 1+: К заказу по группам ────────────────────────────────────
    pdf.add_page()
    pk.cover(pdf, f"К заказу СРЕЗКА  ·  {period}")

    if curr_hols:
        pk.callout(pdf, "Праздники: " + _holiday_text(curr_hols), kind="info")

    pk.section_header(pdf, f"К заказу  ·  {n_order} позиций  ·  на сумму {_rub(total_kop)} ₽")
    pk.section_header(pdf, "Формула: max(эта нед., прошл. нед.) × 1.1 − остаток")

    if recs:
        groups_order: dict[str, list] = defaultdict(list)
        for pname, fpath, rec, cost in recs:
            groups_order[_subgroup(fpath)].append((pname, fpath, rec, cost))

        for sg in sorted(groups_order):
            pk.section_header(pdf, sg)
            pk.table(
                pdf,
                headers=["Название", "К заказу", "На сумму"],
                rows=[
                    [
                        pname,
                        _qty(float(rec)),
                        _rub(rec * cost) + " ₽" if cost else "—",
                    ]
                    for pname, _, rec, cost in groups_order[sg]
                ],
                col_widths=[110, 32, 32],
                aligns=["L", "R", "R"],
                font_size=8.5,
            )
    else:
        pk.callout(pdf, "Заказывать нечего — остатки покрывают спрос.", kind="ok")

    # ─── Следующие страницы: аналитика по группам ────────────────────────────
    pdf.add_page()
    pk.cover(pdf, "Аналитика СРЕЗКА — три колонки")
    pk.section_header(
        pdf,
        f"Прошл. нед. {prev_lbl}  ·  Ср.в приёмке (ООО Поставщик)  ·  Эта нед. {curr_lbl}",
    )

    # Ширины: Название=62, 3×прошл.нед=16, Ср.приёмка=16, 3×эта нед=16 → 62+48+16+48=174
    _group_hdr(pdf, [
        ("Название",    62, CREAM,  INK),
        (prev_lbl,      48, SAGE_L, INK),
        ("Ср.приёмка",  16, TERRA,  _WHITE),
        (curr_lbl,      48, INK,    _WHITE),
    ])

    if a_rows:
        groups_anal: dict[str, list] = defaultdict(list)
        for row in a_rows:
            groups_anal[_subgroup(row[1])].append(row)

        first_group = True
        for sg in sorted(groups_anal):
            if not first_group:
                # Заголовок группы — рисуем как разделитель (GRID-фон, жирный)
                pdf.set_fill_color(*GRID)
                pdf.set_font("DejaVu_B", size=8)
                pdf.set_x(_MARGIN)
                pdf.cell(174, 5.5, f"  {sg}", border=0, fill=True,
                         new_x="LMARGIN", new_y="NEXT")
                pdf.set_font("DejaVu", size=8)
            else:
                # Первая группа — просто заголовок через section_header
                pk.section_header(pdf, sg)
                first_group = False

            pk.table(
                pdf,
                headers=[
                    "Название",
                    "Прод", "Ост", "Изб",
                    "Ср.пр.",
                    "Прод", "Ост", "Изб",
                ],
                rows=[
                    [
                        pname[:45],
                        _qty(p_sold), _qty(p_ost), _over(p_sold, p_ost),
                        _qty(avg),
                        _qty(c_sold), _qty(c_ost), _over(c_sold, c_ost),
                    ]
                    for _, _, p_sold, p_ost, avg, c_sold, c_ost in groups_anal[sg]
                ],
                col_widths=[62, 16, 16, 16, 16, 16, 16, 16],
                aligns=["L", "R", "R", "R", "R", "R", "R", "R"],
                font_size=7.5,
            )
    else:
        pk.callout(pdf, "Нет продаж по СРЕЗКА за оба периода.", kind="info")

    pk.callout(
        pdf,
        "* Ср.приёмка — среднее кол-во в поставке за всю историю (ООО Поставщик, все склады)",
        kind="info",
    )

    return bytes(pdf.output())
