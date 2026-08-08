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
            LIMIT 10
        """, [date_from, date_to, excluded])
        top_rows = cur.fetchall()

    # ── 3. Отток (оптовики, 180 дней, ≥3 покупки) ───────────────────────────
    with conn.cursor() as cur:
        cur.execute("""
            WITH intervals AS (
                SELECT agent_id,
                       day - LAG(day) OVER (PARTITION BY agent_id ORDER BY day) AS gap
                FROM (
                    SELECT DISTINCT agent_id, day
                    FROM sales_doc
                    WHERE channel = 'опт' AND doc_type = 'demand'
                      AND day >= CURRENT_DATE - 180
                ) d
            ),
            avg_gap AS (
                SELECT agent_id, AVG(gap) AS avg_gap_days, COUNT(*) AS purchases
                FROM intervals WHERE gap IS NOT NULL
                GROUP BY agent_id HAVING COUNT(*) >= 3
            ),
            last_seen AS (
                SELECT agent_id, agent_name, MAX(day) AS last_day
                FROM sales_doc WHERE channel = 'опт' AND doc_type = 'demand'
                GROUP BY agent_id, agent_name
            )
            SELECT l.agent_id, a.avg_gap_days, l.last_day,
                   CURRENT_DATE - l.last_day AS days_since,
                   (CURRENT_DATE - l.last_day)::float / NULLIF(a.avg_gap_days, 0) AS ratio
            FROM avg_gap a JOIN last_seen l USING (agent_id)
            WHERE (CURRENT_DATE - l.last_day) > a.avg_gap_days * 1.5
              AND (CURRENT_DATE - l.last_day) >= 7
            ORDER BY ratio DESC
            LIMIT 15
        """)
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

    pk.section_header(pdf, "Топ-10 клиентов по выручке")
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
    pk.cover(pdf, "Возможный отток  ·  оптовики")
    pk.section_header(pdf, "Оптовики без заказа дольше обычного  ·  180 дней")

    if churn_rows:
        pk.table(
            pdf,
            headers=["Клиент", "Посл. заказ", "Дней назад", "Обычно дн.", "Просрочка"],
            rows=[
                [
                    f"Клиент #{i}",
                    last_day.strftime("%d.%m.%Y") if hasattr(last_day, "strftime") else str(last_day),
                    str(int(days_since or 0)),
                    f"{float(avg_gap or 0):.0f}",
                    f"×{float(ratio or 0):.1f}",
                ]
                for i, (_aid, avg_gap, last_day, days_since, ratio) in enumerate(churn_rows, 1)
            ],
            col_widths=[52, 30, 26, 26, 26],
            aligns=["L", "L", "R", "R", "R"],
            font_size=8.5,
        )
        pk.callout(pdf, "Отток — скользящие 180 дней, независимо от выбранного периода.", kind="warn")
    else:
        pk.callout(pdf, "Отставших оптовиков не обнаружено.", kind="ok")

    return bytes(pdf.output())
