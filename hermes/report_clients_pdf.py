"""PDF-отчёт «Клиенты» — брендовый стиль ЦБД."""
from __future__ import annotations

from datetime import date

from . import config, pdf_kit as pk


def _rub(kop: float) -> str:
    return f"{int(kop) // 100:,}".replace(",", " ")


def build_clients_pdf(conn, date_from: date, date_to: date) -> bytes:
    """PDF «Клиенты» — топ + возможный отток."""
    period = f"{date_from.strftime('%d.%m')}–{date_to.strftime('%d.%m.%Y')}"

    placeholders = list(config.RETAIL_PLACEHOLDER_AGENTS or [])
    internal     = list(config.INTERNAL_AGENTS or [])
    excluded     = placeholders + internal

    # ── 1. Сводка ────────────────────────────────────────────────────────────
    with conn.cursor() as cur:
        cur.execute("""
            SELECT COUNT(DISTINCT agent_id),
                   COUNT(*) FILTER (WHERE doc_type = 'demand'),
                   COALESCE(SUM(sum_kop), 0)
            FROM sales_doc
            WHERE day BETWEEN %s AND %s
              AND agent_id IS NOT NULL AND agent_id != ''
        """, [date_from, date_to])
        row = cur.fetchone()

    unique_clients = int(row[0] or 0)
    total_docs     = int(row[1] or 0)
    total_kop      = float(row[2] or 0)
    avg_check      = total_kop / total_docs if total_docs else 0

    # Возвращаемость: клиенты с ≥2 заказами / всего клиентов
    with conn.cursor() as cur:
        cur.execute("""
            SELECT COUNT(*) FROM (
                SELECT agent_id
                FROM sales_doc
                WHERE day BETWEEN %s AND %s
                  AND agent_id IS NOT NULL AND agent_id != ''
                  AND doc_type = 'demand'
                GROUP BY agent_id
                HAVING COUNT(*) >= 2
            ) t
        """, [date_from, date_to])
        repeat_clients = int(cur.fetchone()[0] or 0)

    return_pct = repeat_clients / unique_clients * 100 if unique_clients else 0

    # ── 2. Топ-10 клиентов ───────────────────────────────────────────────────
    with conn.cursor() as cur:
        cur.execute("""
            SELECT agent_id,
                   COUNT(*) FILTER (WHERE doc_type = 'demand') AS orders,
                   COALESCE(SUM(sum_kop), 0) AS rev
            FROM sales_doc
            WHERE day BETWEEN %s AND %s
              AND agent_id IS NOT NULL AND agent_id != ''
              AND NOT (agent_name = ANY(%s))
            GROUP BY agent_id
            ORDER BY SUM(sum_kop) DESC
            LIMIT 35
        """, [date_from, date_to, excluded])
        top_rows = cur.fetchall()

    # ── 3. Клиенты без заказа более 10 дней (активные за 90 дн.) ────────────
    with conn.cursor() as cur:
        cur.execute("""
            SELECT agent_id, agent_name,
                   MAX(day) AS last_day,
                   CURRENT_DATE - MAX(day) AS days_since,
                   COUNT(*) AS orders
            FROM sales_doc
            WHERE doc_type = 'demand'
              AND agent_id IS NOT NULL AND agent_id != ''
              AND NOT (agent_name = ANY(%s))
              AND day >= CURRENT_DATE - 90
            GROUP BY agent_id, agent_name
            HAVING MAX(day) < CURRENT_DATE - 10
            ORDER BY last_day DESC
            LIMIT 40
        """, [excluded])
        churn_rows = cur.fetchall()

    # ── рендеринг ─────────────────────────────────────────────────────────────
    pdf = pk.HermesPDF(section_title="Клиенты", period=period)
    pdf.add_page()
    pk.cover(pdf, f"Клиенты  ·  {period}")

    pk.kpi_row(pdf, [
        ("Уникальных клиентов", str(unique_clients),             ""),
        ("Чеков всего",         str(total_docs),                 ""),
        ("Средний чек",         _rub(avg_check),                 "₽"),
        ("Возвращаемость",      f"{return_pct:.0f}",             "%"),
    ])

    pk.section_header(pdf, "Топ клиентов по выручке")
    if top_rows:
        pk.table(
            pdf,
            headers=["Клиент", "Чеков", "Сумма", "Ср. чек"],
            rows=[
                [
                    f"Клиент #{i}",
                    str(int(orders or 0)),
                    _rub(float(rev or 0)) + " ₽",
                    _rub(float(rev or 0) / int(orders) if orders else 0) + " ₽",
                ]
                for i, (_aid, orders, rev) in enumerate(top_rows, 1)
            ],
            col_widths=[60, 24, 52, 38],
            aligns=["L", "R", "R", "R"],
        )
    else:
        pk.callout(pdf, "Данных по клиентам за период нет.", kind="info")

    # ── Стр. 2: отток ─────────────────────────────────────────────────────────
    pdf.add_page()
    pk.cover(pdf, "Клиенты без заказа  ·  > 10 дней")
    pk.section_header(pdf, "Без заказа более 10 дней  ·  активные за 90 дн.")

    if churn_rows:
        pk.table(
            pdf,
            headers=["Клиент", "Посл. заказ", "Дней без заказа", "Заказов"],
            rows=[
                [
                    f"Клиент #{i}",
                    last_day.strftime("%d.%m.%Y") if hasattr(last_day, "strftime") else str(last_day),
                    str(int(days_since or 0)),
                    str(int(orders or 0)),
                ]
                for i, (_aid, _name, last_day, days_since, orders) in enumerate(churn_rows, 1)
            ],
            col_widths=[60, 36, 44, 34],
            aligns=["L", "L", "R", "R"],
            font_size=8.5,
        )
    else:
        pk.callout(pdf, "Клиентов без заказа более 10 дней не обнаружено.", kind="ok")

    return bytes(pdf.output())
