"""PDF-отчёт «Остатки» и «Залежалые» — брендовый стиль ЦБД."""
from __future__ import annotations

from datetime import date

from hermes import calc, pdf_kit as pk
from hermes.report_stock import (
    _ST_JOIN, _ST_UNIT, _ST_VALUE, _STORE_ORDER, STALE_SREZKA_MIN_DAYS,
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

    # Товары ООО «Поставщик» — применяем скидку 7% к закупочной стоимости
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
        """Параметры скидки (disc_list × n) для подстановки в SELECT до WHERE."""
        return [disc_list] * n if has_disc else []

    pdf = pk.HermesPDF(section_title="Остатки", period=period)
    pdf.add_page()
    pk.cover(pdf, f"Остатки  ·  {day.strftime('%d.%m.%Y')}")

    # ── таблица по складам ────────────────────────────────────────────────────
    pk.section_header(pdf, "Остатки по складам  ·  Ассортимент, без резерва")

    store_rows = []
    grand_pos  = 0
    grand_kop  = 0.0

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
            pos  = int(r[0])
            qty  = float(r[1])
            kop  = float(r[2])
            grand_pos += pos
            grand_kop += kop
            store_rows.append([sn, str(pos), _qty(qty), _rub(kop) + " ₽"])

    pk.table(
        pdf,
        headers=["Склад", "Позиций", "Кол-во", "Сумма закуп."],
        rows=store_rows,
        col_widths=[84, 24, 28, 38],
        aligns=["L", "R", "R", "R"],
    )

    pdf.ln(2)
    pk.kpi_row(pdf, [
        ("Позиций всего",    str(grand_pos),  ""),
        ("Закуп. стоимость", _rub(grand_kop), "₽"),
    ])

    # ── топ-10 позиций по стоимости ───────────────────────────────────────────
    pk.section_header(pdf, "Топ-10 позиций по закупочной стоимости")

    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT product_name,
                   SUM(stock_qty)   AS total_qty,
                   MAX({_stu})      AS cost_unit,
                   SUM({_stv})      AS cost_total
            FROM stock_snapshot {_ST_JOIN}
            WHERE day = %s AND stock_qty > 0 AND reserve_qty = 0
              AND folder_path LIKE %s
            GROUP BY product_name
            ORDER BY cost_total DESC
            LIMIT 10
        """, _disc(2) + [day, "Ассортимент/%"])
        top_rows = cur.fetchall()

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

    # ── Залежалые ─────────────────────────────────────────────────────────────
    pdf.add_page()
    pk.cover(pdf, f"Залежалые  ·  без движения > {STALE_SREZKA_MIN_DAYS} дней")
    pk.section_header(pdf, f"СРЕЗКА без продаж более {STALE_SREZKA_MIN_DAYS} дн.  ·  без резерва")

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
            ORDER BY store_name, stock_qty DESC
        """, _disc(2) + [day, "Ассортимент/%"])
        snap_rows = cur.fetchall()

    stale = []
    for sname, pname, qty, cost_unit, cost_total in snap_rows:
        last      = last_sales.get(pname)
        days_idle = (day - last).days if last else 9999
        if days_idle <= STALE_SREZKA_MIN_DAYS:
            continue
        stale.append((sname, pname, float(qty), cost_unit, float(cost_total or 0), days_idle))

    if not stale:
        pk.callout(pdf, "Залежалой СРЕЗКИ не обнаружено.", kind="ok")
    else:
        total_kop = sum(e[4] for e in stale)
        pk.kpi_row(pdf, [
            ("Залежалых позиций", str(len(stale)), ""),
            ("Заморожено",        _rub(total_kop),  "₽"),
        ])

        pk.table(
            pdf,
            headers=["Склад", "Название", "Кол-во", "Дней", "Сумма"],
            rows=[
                [
                    sname,
                    pname,
                    _qty(qty),
                    "нет истории" if days > 900 else f"{days} дн.",
                    _rub(cost_total) + " ₽",
                ]
                for sname, pname, qty, _cu, cost_total, days in stale
            ],
            col_widths=[46, 66, 20, 18, 24],
            aligns=["L", "L", "R", "R", "R"],
            font_size=8.5,
        )

        pdf.ln(2)
        pk.callout(
            pdf,
            f"Залежалая СРЕЗКА: {len(stale)} поз. · {_rub(total_kop)} ₽ заморожено.",
            kind="warn",
        )

    return bytes(pdf.output())
