"""PDF-отчёт «Расходы» — брендовый стиль ЦБД."""
from __future__ import annotations

from datetime import date

from . import config, pdf_kit as pk


def _rub(kop: float) -> str:
    return f"{int(kop) // 100:,}".replace(",", " ")


def build_cashflow_pdf(conn, date_from: date, date_to: date) -> bytes:
    """PDF «Расходы» — операционные + выводы собственникам."""
    period = f"{date_from.strftime('%d.%m')}–{date_to.strftime('%d.%m.%Y')}"

    owner_items = list(config.OWNER_EXPENSE_ITEMS or [])
    excl_owner = (
        "AND (expense_item_name IS NULL OR NOT (expense_item_name = ANY(%s)))"
        if owner_items else ""
    )

    def _op_params(extra: list | None = None) -> list:
        base = [date_from, date_to] + ([owner_items] if owner_items else [])
        return base + (extra or [])

    # ── 1. Операционные расходы — детальные строки ──────────────────────────
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT day, expense_item_name, agent_name, project_name, amount_kop
            FROM cashflow_event
            WHERE day BETWEEN %s AND %s AND direction = 'out'
              {excl_owner}
            ORDER BY day DESC, amount_kop DESC
        """, _op_params())
        op_rows = cur.fetchall()

    op_total = sum(int(r[4] or 0) for r in op_rows)
    unique_items = len({r[1] or "Без статьи" for r in op_rows})

    # ── 2. Разбивка по складам и статьям ────────────────────────────────────
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT COALESCE(NULLIF(project_name, ''), 'Без склада'),
                   COALESCE(NULLIF(expense_item_name, ''), 'Без статьи'),
                   SUM(amount_kop) AS kop
            FROM cashflow_event
            WHERE day BETWEEN %s AND %s AND direction = 'out'
              {excl_owner}
            GROUP BY 1, 2
            ORDER BY 1, kop DESC
        """, _op_params())
        store_item_rows = cur.fetchall()

    # ── 3. Выводы собственникам ──────────────────────────────────────────────
    if owner_items:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT day, expense_item_name, amount_kop
                FROM cashflow_event
                WHERE day BETWEEN %s AND %s AND direction = 'out'
                  AND expense_item_name = ANY(%s)
                ORDER BY day DESC
            """, [date_from, date_to, owner_items])
            owner_rows = cur.fetchall()
    else:
        owner_rows = []

    owner_total = sum(int(r[2] or 0) for r in owner_rows)
    grand_total = op_total + owner_total

    # ── рендеринг ────────────────────────────────────────────────────────────
    pdf = pk.HermesPDF(section_title="Расходы", period=period)
    pdf.add_page()
    pk.cover(pdf, f"Расходы  ·  {period}")

    pk.kpi_row(pdf, [
        ("Операционных",    _rub(op_total),    "₽"),
        ("Выводы собств.",  _rub(owner_total),  "₽"),
        ("Итого расходов",  _rub(grand_total),  "₽"),
        ("Статей расходов", str(unique_items),  ""),
    ])

    # Разбивка по складам и статьям
    if store_item_rows:
        pk.section_header(pdf, "По складам и статьям")
        prev_store = None
        table_rows = []
        for store, item, kop in store_item_rows:
            store_cell = store if store != prev_store else ""
            if store != prev_store:
                prev_store = store
            table_rows.append([store_cell, item, _rub(float(kop or 0)) + " ₽"])
        pk.table(
            pdf,
            headers=["Склад", "Статья", "Сумма"],
            rows=table_rows,
            col_widths=[70, 80, 24],
            aligns=["L", "L", "R"],
            font_size=8.5,
        )

    # Блок 2: выводы собственникам
    pdf.add_page()
    pk.cover(pdf, "Выводы собственникам")
    pk.section_header(pdf, "Выводы собственникам")
    if owner_rows:
        pk.table(
            pdf,
            headers=["Дата", "Получатель", "Сумма"],
            rows=[
                [
                    day.strftime("%d.%m") if hasattr(day, "strftime") else str(day),
                    (item or "")[:58],
                    _rub(float(kop or 0)) + " ₽",
                ]
                for day, item, kop in owner_rows
            ],
            col_widths=[18, 128, 28],
            aligns=["L", "L", "R"],
        )
    else:
        pk.callout(pdf, "Выводов собственникам за период нет.", kind="ok")

    pk.callout(pdf, "Выводы собственникам — не операционные расходы", kind="warn")
    pk.callout(
        pdf,
        f"Операционные {_rub(op_total)} ₽  +  Выводы {_rub(owner_total)} ₽"
        f"  =  Итого {_rub(grand_total)} ₽",
        kind="info",
    )

    # Блок 1: операционные расходы — детально (в конце)
    pdf.add_page()
    pk.cover(pdf, "Операционные расходы")
    pk.section_header(pdf, "Операционные расходы")
    if op_rows:
        pk.table(
            pdf,
            headers=["Дата", "Статья", "Контрагент", "Склад", "Сумма"],
            rows=[
                [
                    day.strftime("%d.%m") if hasattr(day, "strftime") else str(day),
                    (item or "Без статьи")[:30],
                    (agent or "")[:26],
                    (proj or "")[:20],
                    _rub(float(kop or 0)) + " ₽",
                ]
                for day, item, agent, proj, kop in op_rows
            ],
            col_widths=[18, 56, 48, 30, 22],
            aligns=["L", "L", "L", "L", "R"],
            font_size=8.5,
        )
    else:
        pk.callout(pdf, "Операционных расходов за период нет.", kind="ok")

    return bytes(pdf.output())
