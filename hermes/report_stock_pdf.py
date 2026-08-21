"""PDF-отчёт «Остатки» и «Залежалые» — брендовый стиль ЦБД."""
from __future__ import annotations

from datetime import date

from hermes import calc, config, pdf_kit as pk
from hermes.report_stock import (
    _ST_JOIN, _ST_UNIT, _ST_VALUE, _STORE_ORDER,
)

# Порог залежалости для PDF-отчёта (в тексте report_stock.py — 5 дней).
_STALE_MIN_DAYS = 5

# Залежалые показываем только по настоящим розничным точкам. Ресторанные
# склады входят в остатки и перемещения, но не должны ошибочно считаться
# розницей только потому, что в названии нет слова «База».
_RETAIL_STORES = frozenset(
    store["name"]
    for store in config.STORES
    if config.STORE_CHANNELS.get(store["id"]) == "розница"
)


def _rub(kop: float) -> str:
    return f"{int(kop) // 100:,}".replace(",", " ")


def _qty(q: float) -> str:
    return (
        f"{int(q):,}".replace(",", " ")
        if q == int(q)
        else f"{q:,.1f}".replace(",", " ")
    )


def build_stock_pdf(conn, date_from: date, date_to: date) -> bytes:
    """PDF «Остатки и Залежалые» — снимок на date_to."""
    day    = date_to
    period = f"{date_from.strftime('%d.%m')}–{date_to.strftime('%d.%m.%Y')}"

    # ── скидка ООО «Поставщик» ────────────────────────────────────────────────
    discount_pids = calc.discount_product_ids(conn)
    disc_list = sorted(discount_pids)
    has_disc  = bool(disc_list)

    if has_disc:
        _stv = (
            "stock_qty"
            " * COALESCE(NULLIF(pp.price_kop, 0), stock_snapshot.cost_price_kop)"
            " * CASE WHEN stock_snapshot.product_id = ANY(%s::text[])"
            "   THEN 0.93::numeric ELSE 1.0 END"
        )
        _stu = (
            "COALESCE(NULLIF(pp.price_kop, 0), stock_snapshot.cost_price_kop)"
            " * CASE WHEN stock_snapshot.product_id = ANY(%s::text[])"
            "   THEN 0.93::numeric ELSE 1.0 END"
        )
    else:
        _stv = _ST_VALUE
        _stu = _ST_UNIT

    def _disc(n: int = 1) -> list:
        """Параметры скидки (disc_list × n) — вставляются перед WHERE-параметрами."""
        return [disc_list] * n if has_disc else []

    # ── сбор данных (до рендеринга) ───────────────────────────────────────────

    # 1. Остатки по складам
    store_rows: list[list[str]] = []
    grand_pos = 0
    grand_kop = 0.0

    with conn.cursor() as cur:
        for sn in _STORE_ORDER:
            cur.execute(f"""
                SELECT COUNT(*),
                       COALESCE(SUM(stock_qty), 0),
                       COALESCE(SUM({_stv}), 0)
                FROM stock_snapshot {_ST_JOIN}
                WHERE day = %s AND stock_qty > 0 AND reserve_qty = 0
                  AND store_name = %s AND folder_path LIKE %s
            """, _disc() + [day, sn, "Ассортимент/СРЕЗКА/%"])
            r = cur.fetchone()
            if not r or not r[0]:
                continue
            pos = int(r[0])
            qty = float(r[1])
            kop = float(r[2])
            grand_pos += pos
            grand_kop += kop
            store_rows.append([sn, str(pos), _qty(qty), _rub(kop) + " ₽"])

    # 2. Топ-15 продаж СРЕЗКА — свой по каждому складу за период
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT store_name, store_id FROM sales_by_store_day"
            " WHERE store_name = ANY(%s::text[])",
            ([list(_STORE_ORDER)],)
        )
        _sid_map = {r[0]: r[1] for r in cur.fetchall()}

    top_by_store: dict[str, list] = {}
    for _sn in _STORE_ORDER:
        _sid = _sid_map.get(_sn)
        if not _sid:
            continue
        with conn.cursor() as cur:
            cur.execute("""
                SELECT spd.product_name, SUM(spd.sell_qty) AS qty
                FROM sales_by_product_day spd
                JOIN product_dim pd ON pd.product_id = spd.assortment_id
                WHERE spd.day BETWEEN %s AND %s
                  AND spd.store_id = %s
                  AND pd.folder_path LIKE %s
                  AND spd.sell_qty > 0
                GROUP BY spd.product_name
                ORDER BY qty DESC
                LIMIT 15
            """, [date_from, date_to, _sid, "Ассортимент/СРЕЗКА/%"])
            _rows = cur.fetchall()
        if _rows:
            top_by_store[_sn] = [(r[0], float(r[1])) for r in _rows]

    # 3. Залежалые СРЕЗКА > 14 дней — сортировка по сумме DESC внутри склада
    with conn.cursor() as cur:
        cur.execute(
            "SELECT product_name, MAX(day) FROM sales_by_product_day GROUP BY product_name"
        )
        last_sales: dict[str, date] = {row[0]: row[1] for row in cur.fetchall()}

    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT store_name, product_name, stock_qty,
                   {_stu} AS cost_unit,
                   {_stv} AS cost_total
            FROM stock_snapshot {_ST_JOIN}
            WHERE day = %s AND stock_qty > 0 AND reserve_qty = 0
              AND folder_path LIKE %s
            ORDER BY store_name, cost_total DESC
        """, _disc(2) + [day, "Ассортимент/СРЕЗКА/%"])
        snap_rows = cur.fetchall()

    stale_by_store: dict[str, list] = {}
    has_no_history = False
    for sname, pname, qty, _cu, cost_total in snap_rows:
        if sname not in _RETAIL_STORES:
            continue
        last      = last_sales.get(pname)
        days_idle = (day - last).days if last else 9999
        if days_idle <= _STALE_MIN_DAYS:
            continue
        if days_idle > 900:
            has_no_history = True
        stale_by_store.setdefault(sname, []).append(
            (pname, float(qty), float(cost_total or 0), days_idle)
        )

    stale_total_pos = sum(len(v) for v in stale_by_store.values())
    stale_total_kop = sum(item[2] for items in stale_by_store.values()
                          for item in items)

    # ── рендеринг ─────────────────────────────────────────────────────────────

    pdf = pk.HermesPDF(section_title="Остатки", period=period)
    pdf.add_page()
    pk.cover(pdf, f"Остатки  ·  {day.strftime('%d.%m.%Y')}")

    # Таблица по складам
    pk.section_header(pdf, "Остатки по складам  ·  СРЕЗКА, без резерва")
    pk.table(
        pdf,
        headers=["Склад", "Позиций", "Кол-во", "Сумма закуп."],
        rows=store_rows,
        col_widths=[84, 24, 28, 38],
        aligns=["L", "R", "R", "R"],
    )

    # 4 KPI-карточки (вкл. залежалые — данные уже посчитаны)
    pdf.ln(2)
    pk.kpi_row(pdf, [
        ("Позиций всего",     str(grand_pos),           ""),
        ("Закуп. стоимость",  _rub(grand_kop),          "₽"),
        ("Залежалых позиций", str(stale_total_pos),     ""),
        ("Заморожено",        _rub(stale_total_kop),    "₽"),
    ])

    # Топ-15 продаж СРЕЗКА — свой по каждому складу
    for _sn in _STORE_ORDER:
        _items = top_by_store.get(_sn)
        if not _items:
            continue
        pk.section_header(pdf, f"Топ-15 продаж  ·  {_sn}")
        pk.table(
            pdf,
            headers=["Название", "Продано, шт"],
            rows=[[name, _qty(qty)] for name, qty in _items],
            col_widths=[140, 34],
            aligns=["L", "R"],
            font_size=8.5,
        )

    # ── Залежалые (новая страница) ─────────────────────────────────────────────
    pdf.add_page()
    pk.cover(pdf, f"Залежалые  ·  розничные точки  ·  > {_STALE_MIN_DAYS} дней")
    pk.section_header(
        pdf, f"СРЕЗКА без продаж более {_STALE_MIN_DAYS} дн.  ·  розница, без резерва"
    )

    if not stale_by_store:
        pk.callout(pdf, "Залежалой СРЕЗКИ не обнаружено.", kind="ok")
    else:
        pk.kpi_row(pdf, [
            ("Залежалых позиций", str(stale_total_pos),  ""),
            ("Заморожено",        _rub(stale_total_kop), "₽"),
        ])

        # Таблица с группировкой по складам (без колонки «Склад»)
        for sname in sorted(stale_by_store):
            items = stale_by_store[sname][:15]
            pk.section_header(pdf, sname)
            pk.table(
                pdf,
                headers=["Название", "Кол-во", "Дней", "Сумма"],
                rows=[
                    [
                        pname,
                        _qty(qty),
                        "нет ист.*" if days > 900 else f"{days} дн.",
                        _rub(cost_total) + " ₽",
                    ]
                    for pname, qty, cost_total, days in items
                ],
                col_widths=[100, 22, 28, 24],
                aligns=["L", "R", "R", "R"],
                font_size=8.5,
            )

        pdf.ln(2)
        pk.callout(
            pdf,
            f"Залежалая СРЕЗКА: {stale_total_pos} поз. · {_rub(stale_total_kop)} ₽ заморожено.",
            kind="warn",
        )

        if has_no_history:
            pdf.ln(1)
            pdf.set_x(pk._MARGIN)
            pdf.set_font("DejaVu", size=7.5)
            pdf.set_text_color(*pk.SAGE)
            pdf.cell(
                pk._INNER_W, 4,
                "* «нет истории» — в МойСклад отсутствуют данные о последней продаже.",
                align="L", new_x="LMARGIN", new_y="NEXT",
            )
            pdf.set_text_color(*pk.INK)

    return bytes(pdf.output())
