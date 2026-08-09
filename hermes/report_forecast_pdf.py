"""PDF-отчёт «Прогноз закупки СРЕЗКА» — forecast_v9, брендовый стиль ЦБД."""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import date, timedelta

from . import calc, pdf_kit as pk
from .pdf_kit import _MARGIN, _INNER_W, INK, SAGE, SAGE_L, CREAM, TERRA, GRID

_WHITE        = (255, 255, 255)
_RED          = (204, 0, 0)
_SENTINEL_QTY = 9999

# Колонки таблицы "К заказу" — сумма 174 мм (= _INNER_W)
_OC = [90, 21, 21, 21, 21]
# x-координаты левых краёв колонок
_OX = [_MARGIN + sum(_OC[:i]) for i in range(len(_OC))]

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
    return f"{int(q)}" if q == int(q) else f"{q:.1f}"


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


def _group_hdr(pdf: pk.HermesPDF, groups: list[tuple]) -> None:
    """Строка групповых заголовков аналитики: [(label, width, fill_rgb, text_rgb), ...]."""
    pdf.set_x(_MARGIN)
    pdf.set_font("DejaVu_B", size=7.5)
    for label, w, fill, txt in groups:
        pdf.set_fill_color(*fill)
        pdf.set_text_color(*txt)
        pdf.cell(w, 5.0, label, border=1, align="C", fill=True)
    pdf.ln()
    pdf.set_text_color(*INK)


# ── Рендеринг таблицы «К заказу» ─────────────────────────────────────────────

def _order_header_row(pdf: pk.HermesPDF) -> None:
    """Двухстрочный заголовок колонок с фоном SAGE."""
    y0 = pdf.get_y()
    H  = 10.0  # общая высота заголовка
    h  = H / 2  # высота одной строки

    labels = [
        ("Название",   ""),
        ("Пр.нед",  "(шт)"),
        ("Эт.нед",  "(шт)"),
        ("Остаток", "(шт)"),
        ("К заказу","(шт)"),
    ]
    aligns = ["L", "C", "C", "C", "R"]

    pdf.set_font("DejaVu_B", size=8)
    pdf.set_text_color(*_WHITE)

    for (l1, l2), w, al, x in zip(labels, _OC, aligns, _OX):
        pdf.set_fill_color(*SAGE)
        pdf.set_xy(x, y0)
        pdf.cell(w, H, "", border=1, fill=True)
        pdf.set_xy(x, y0 + 1)
        pdf.cell(w, h, l1, align=al)
        if l2:
            pdf.set_xy(x, y0 + h)
            pdf.cell(w, h, l2, align=al)

    pdf.set_xy(float(_MARGIN), y0 + H)
    pdf.set_text_color(*INK)


def _order_group_divider(pdf: pk.HermesPDF, label: str) -> None:
    """Строка-разделитель группы (Розы / Хризантемы / ...) в таблице К заказу."""
    if pdf.get_y() + 6 > pdf.page_break_trigger:
        pdf.add_page()
        _order_header_row(pdf)
    pdf.set_fill_color(*SAGE)
    pdf.set_text_color(*INK)
    pdf.set_font("DejaVu_B", size=8)
    pdf.set_x(_MARGIN)
    pdf.cell(_INNER_W, 6, f"  {label}", border=0, fill=True,
             new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(*INK)


def _order_data_row(
    pdf: pk.HermesPDF,
    pname: str,
    p_sold: float,
    c_sold: float,
    stock: float,
    rec: int,
    zidx: int,
) -> None:
    """Одна строка данных таблицы К заказу."""
    rh = 14.0 if len(pname) > 40 else 7.0

    if pdf.get_y() + rh > pdf.page_break_trigger:
        pdf.add_page()
        _order_header_row(pdf)

    y0  = pdf.get_y()
    fill_color = SAGE_L if zidx % 2 == 1 else _WHITE

    pdf.set_draw_color(*GRID)
    pdf.set_fill_color(*fill_color)
    pdf.set_font("DejaVu", size=8.5)
    pdf.set_text_color(*INK)

    # Название
    x = float(_OX[0])
    if len(pname) > 40:
        # Фон + рамка
        pdf.rect(x, y0, _OC[0], rh, style="FD")
        # Текст через multi_cell
        pdf.set_xy(x + 1, y0 + 1)
        pdf.multi_cell(_OC[0] - 2, 6, pname, border=0, align="L")
    else:
        pdf.set_xy(x, y0)
        pdf.cell(_OC[0], rh, pname, border=1, fill=True, align="L")

    # Пр.нед
    pdf.set_xy(float(_OX[1]), y0)
    pdf.cell(_OC[1], rh, str(int(p_sold)) if p_sold > 0 else "—",
             border=1, fill=True, align="C")

    # Эт.нед
    pdf.set_xy(float(_OX[2]), y0)
    pdf.cell(_OC[2], rh, str(int(c_sold)) if c_sold > 0 else "—",
             border=1, fill=True, align="C")

    # Остаток
    stock_int = int(stock)
    pdf.set_xy(float(_OX[3]), y0)
    if stock_int == 0:
        pdf.set_text_color(*_RED)
        pdf.cell(_OC[3], rh, "0 ⚠", border=1, fill=True, align="C")
        pdf.set_text_color(*INK)
    else:
        pdf.cell(_OC[3], rh, str(stock_int), border=1, fill=True, align="C")

    # К заказу (жирный)
    pdf.set_font("DejaVu_B", size=8.5)
    pdf.set_xy(float(_OX[4]), y0)
    pdf.cell(_OC[4], rh, str(rec), border=1, fill=True, align="R",
             new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("DejaVu", size=8.5)
    pdf.set_xy(float(_MARGIN), y0 + rh)


def _order_total_row(pdf: pk.HermesPDF, n_items: int, rec_total: int) -> None:
    """Итоговая строка в конце таблицы К заказу."""
    if pdf.get_y() + 7 > pdf.page_break_trigger:
        pdf.add_page()
    y0 = pdf.get_y()
    pdf.set_fill_color(*INK)
    pdf.set_text_color(*_WHITE)
    pdf.set_font("DejaVu_B", size=8.5)
    pdf.set_xy(float(_MARGIN), y0)
    pdf.cell(_OC[0], 7, f"  Итого к заказу: {n_items} позиций",
             border=1, fill=True, align="L")
    pdf.set_xy(float(_OX[1]), y0)
    pdf.cell(_OC[1] + _OC[2] + _OC[3], 7, "", border=1, fill=True)
    pdf.set_xy(float(_OX[4]), y0)
    pdf.cell(_OC[4], 7, str(rec_total), border=1, fill=True, align="R",
             new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(*INK)


# ═══════════════════════════════════════════════════════════════════════════════

def build_forecast_pdf(conn, date_from: date, date_to: date) -> bytes:
    """PDF «Прогноз закупки СРЕЗКА» — forecast_v9."""
    assert sum(_OC) <= _INNER_W, f"Таблица шире страницы: {sum(_OC)} > {_INNER_W}"

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

    # ── Продажи текущей недели ────────────────────────────────────────────────
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

    # ── Продажи прошлой недели ────────────────────────────────────────────────
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

    # ── Среднее в приёмке: ООО Поставщик, все склады, вся история ────────────
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

    # ── Остатки: Воровского (для заказа) и все склады (для аналитики) ────────
    curr_stock_voro = _stock_on(conn, date_to, store_filter="Воровского")
    curr_stock      = _stock_on(conn, date_to, store_filter=None)
    prev_stock      = _stock_on(conn, prev_to,  store_filter=None)

    # ── Себестоимость для «На сумму» ──────────────────────────────────────────
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

    # ── Рекомендации к заказу (остаток Воровского) ───────────────────────────
    # (pname, folder_path, p_sold, c_sold, stock_voro, rec, cost)
    recs: list[tuple] = []
    for pid in srezka_pids:
        c_sold = curr_sales.get(pid, 0.0)
        p_sold = prev_sales.get(pid, 0.0)
        base   = max(c_sold, p_sold)
        if base == 0:
            continue
        stock = curr_stock_voro.get(pid, 0.0)
        rec   = max(0, round(base * 1.1 - stock))
        if rec > 0:
            pname, fpath = srezka_info[pid]
            cost = cost_by_pid.get(pid, 0.0)
            recs.append((pname, fpath, p_sold, c_sold, stock, rec, cost))

    # Сортировка: folder_path ASC, внутри — rec DESC
    recs.sort(key=lambda x: (x[1], -x[5]))

    # ── Аналитика ─────────────────────────────────────────────────────────────
    # (pname, fpath, p_sold, p_ost, avg, c_sold, c_ost)
    a_rows: list[tuple] = []
    for pid in srezka_pids:
        c_sold = curr_sales.get(pid, 0.0)
        p_sold = prev_sales.get(pid, 0.0)
        if c_sold == 0 and p_sold == 0:
            continue
        pname, fpath = srezka_info[pid]
        a_rows.append((
            pname, fpath,
            p_sold, prev_stock.get(pid, 0.0),
            avg_supply.get(pid, 0.0),
            c_sold, curr_stock.get(pid, 0.0),
        ))
    a_rows.sort(key=lambda x: (x[1], -(x[5] + x[2])))

    curr_hols = _holidays_near(date_from, date_to)
    prev_lbl  = f"{prev_from.strftime('%d.%m')}–{prev_to.strftime('%d.%m')}"
    curr_lbl  = f"{date_from.strftime('%d.%m')}–{date_to.strftime('%d.%m')}"

    # ═══════════════════════════════════════════════════════════════════════════
    # РЕНДЕРИНГ
    # ═══════════════════════════════════════════════════════════════════════════
    pdf = pk.HermesPDF(section_title="Прогноз СРЕЗКА", period=period)

    # ─── Страница 1+: К заказу ───────────────────────────────────────────────
    pdf.add_page()
    pk.cover(pdf, f"СРЕЗКА — К ЗАКАЗУ  ·  {period}")

    if curr_hols:
        pk.callout(pdf, "Праздники: " + _holiday_text(curr_hols), kind="info")

    # Подзаголовок с формулой
    pdf.set_x(_MARGIN)
    pdf.set_font("DejaVu", size=8)
    pdf.set_text_color(100, 100, 100)
    pdf.cell(
        _INNER_W, 5,
        "Формула: К заказу = max(прод. прошл. нед., прод. эта нед.) × 1.1 − остаток",
        align="L", new_x="LMARGIN", new_y="NEXT",
    )
    pdf.set_text_color(*INK)
    pdf.ln(2)

    if recs:
        groups_order: dict[str, list] = defaultdict(list)
        for row in recs:
            groups_order[_subgroup(row[1])].append(row)

        _order_header_row(pdf)

        n_total   = 0
        rec_total = 0
        zidx      = 0

        for sg in sorted(groups_order):
            _order_group_divider(pdf, sg)
            for pname, fpath, p_sold, c_sold, stock, rec, cost in groups_order[sg]:
                _order_data_row(pdf, pname, p_sold, c_sold, stock, rec, zidx)
                zidx      += 1
                n_total   += 1
                rec_total += rec

        _order_total_row(pdf, n_total, rec_total)
    else:
        pk.callout(pdf, "Заказывать нечего — остатки покрывают спрос.", kind="ok")

    # ─── Следующие страницы: аналитика по группам ────────────────────────────
    pdf.add_page()
    pk.cover(pdf, "Аналитика СРЕЗКА — три колонки")
    pk.section_header(
        pdf,
        f"Прошл. нед. {prev_lbl}  ·  Ср.в приёмке (ООО Поставщик)  ·  Эта нед. {curr_lbl}",
    )

    # Заголовок-шапка аналитики
    _group_hdr(pdf, [
        ("Название",   62, CREAM,  INK),
        (prev_lbl,     48, SAGE_L, INK),
        ("Ср.приёмка", 16, TERRA,  _WHITE),
        (curr_lbl,     48, INK,    _WHITE),
    ])

    if a_rows:
        groups_anal: dict[str, list] = defaultdict(list)
        for row in a_rows:
            groups_anal[_subgroup(row[1])].append(row)

        first_group = True
        for sg in sorted(groups_anal):
            if not first_group:
                pdf.set_fill_color(*GRID)
                pdf.set_font("DejaVu_B", size=8)
                pdf.set_x(_MARGIN)
                pdf.cell(_INNER_W, 5.5, f"  {sg}", border=0, fill=True,
                         new_x="LMARGIN", new_y="NEXT")
                pdf.set_font("DejaVu", size=8)
            else:
                pk.section_header(pdf, sg)
                first_group = False

            pk.table(
                pdf,
                headers=["Название", "Прод", "Ост", "Изб", "Ср.пр.", "Прод", "Ост", "Изб"],
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
