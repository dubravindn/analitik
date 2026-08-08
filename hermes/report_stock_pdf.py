"""PDF-отчёт «Остатки» и «Залежалые» — брендовый стиль ЦБД."""
from __future__ import annotations

from datetime import date

from hermes import calc, pdf_kit as pk
from hermes.report_stock import (
    _ST_JOIN, _ST_UNIT, _ST_VALUE, _STORE_ORDER,
)

# Порог залежалости для PDF-отчёта (в тексте report_stock.py — 5 дней).
_STALE_MIN_DAYS = 14


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
            """, _disc() + [day, sn, "Ассортимент/%"])
            r = cur.fetchone()
            if not r or not r[0]:
                continue
            pos = int(r[0])
            qty = float(r[1])
            kop = float(r[2])
            grand_pos += pos
            grand_kop += kop
            store_rows.append([sn, str(pos), _qty(qty), _rub(kop) + " ₽"])

    # 2. Топ-10 — только ассортимент, pids через ANY(%s) чтобы WHERE точно применился
    a_pids = calc.assortment_product_ids(conn)
    with conn.cursor() as cur:
        if a_pids:
            cur.execute(f"""
                SELECT product_name,
                       SUM(stock_qty)   AS total_qty,
                       MAX({_stu})      AS cost_unit,
                       SUM({_stv})      AS cost_total
                FROM stock_snapshot {_ST_JOIN}
                WHERE day = %s AND stock_qty > 0 AND reserve_qty = 0
                  AND stock_snapshot.product_id = ANY(%s::text[])
                GROUP BY product_name
                ORDER BY cost_total DESC
                LIMIT 10
            """, _disc(2) + [day, a_pids])
            top_rows = cur.fetchall()
        else:
            top_rows = []

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
              AND SPLIT_PART(folder_path, '/', 2) = 'СРЕЗКА'
            ORDER BY store_name, cost_total DESC
        """, _disc(2) + [day, "Ассортимент/%"])
        snap_rows = cur.fetchall()

    stale_by_store: dict[str, list] = {}
    has_no_history = False
    for sname, pname, qty, _cu, cost_total in snap_rows:
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
    pk.section_header(pdf, "Остатки по складам  ·  Ассортимент, без резерва")
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

    # Топ-10 — только ассортимент
    pk.section_header(
        pdf, "Топ-10 позиций по закупочной стоимости  ·  Ассортимент"
    )
    pk.table(
        pdf,
        headers=["Название", "Кол-во", "Цена закуп.", "Сумма"],
        rows=[
            [
                name,
                _qty(float(qty or 0)),
                (_rub(float(cu)) + " ₽") if cu else "—",
                _rub(float(ct or 0)) + " ₽",
            ]
            for name, qty, cu, ct in top_rows
        ],
        col_widths=[90, 24, 30, 30],
        aligns=["L", "R", "R", "R"],
    )

    # ── Залежалые (новая страница) ─────────────────────────────────────────────
    pdf.add_page()
    pk.cover(pdf, f"Залежалые  ·  без движения > {_STALE_MIN_DAYS} дней")
    pk.section_header(
        pdf, f"СРЕЗКА без продаж более {_STALE_MIN_DAYS} дн.  ·  без резерва"
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
            items = stale_by_store[sname]
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
