"""Клиентская аналитика: топ клиентов + детектор оттока (churn)."""
from __future__ import annotations

from datetime import date


def _rub(kop: float) -> str:
    return f"{kop / 100:,.0f}".replace(",", " ")


def build_clients_report(conn, d_from: date, d_to: date, store_name: str | None = None) -> str:
    period_str = (
        d_from.strftime("%d.%m.%Y") if d_from == d_to
        else f"{d_from.strftime('%d.%m.%Y')} – {d_to.strftime('%d.%m.%Y')}"
    )
    store_label = f" · {store_name}" if store_name else " · Все склады"
    lines: list[str] = [f"👥 Клиенты {period_str}{store_label}", ""]

    sf = "AND store_name = %s" if store_name else ""
    p  = [d_from, d_to] + ([store_name] if store_name else [])

    # Сводка
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT COUNT(DISTINCT agent_id), COUNT(*), COALESCE(SUM(amount_kop), 0)
            FROM sales_doc
            WHERE day BETWEEN %s AND %s {sf}
              AND agent_id IS NOT NULL AND agent_id != ''
        """, p)
        row = cur.fetchone()

    if not row or not row[0]:
        lines.append("Данных по клиентам за этот период нет.")
        return "\n".join(lines)

    unique_clients = int(row[0])
    total_docs     = int(row[1])
    total_kop      = float(row[2])
    lines.append(
        f"📋 Клиентов: {unique_clients} · Заказов: {total_docs}"
        f" · Сумма: {_rub(total_kop)} ₽"
    )
    lines.append("")

    # Топ-20 по выручке
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT agent_name, COUNT(*) AS orders, SUM(amount_kop) AS rev
            FROM sales_doc
            WHERE day BETWEEN %s AND %s {sf}
              AND agent_id IS NOT NULL AND agent_id != ''
            GROUP BY agent_id, agent_name
            ORDER BY SUM(amount_kop) DESC
            LIMIT 20
        """, p)
        top = cur.fetchall()

    if top:
        lines.append("🏆 Топ-20 клиентов:")
        for i, (name, orders, rev) in enumerate(top, 1):
            avg = float(rev) / orders if orders else 0
            lines.append(
                f"  {i:2}. {name}\n"
                f"      {orders} зак. · {_rub(float(rev))} ₽ · ср.чек {_rub(avg)} ₽"
            )
        lines.append("")

    # Детектор оттока: клиенты с интервалом ≥ 2 покупок, давно не приходившие
    with conn.cursor() as cur:
        cur.execute("""
            WITH agent_stats AS (
                SELECT
                    agent_id,
                    agent_name,
                    MAX(day) AS last_order,
                    (CURRENT_DATE - MAX(day)) AS days_since,
                    AVG(
                        COALESCE(
                            day - LAG(day) OVER (PARTITION BY agent_id ORDER BY day),
                            0
                        )
                    ) AS avg_gap
                FROM sales_doc
                WHERE agent_id IS NOT NULL AND agent_id != ''
                GROUP BY agent_id, agent_name
                HAVING COUNT(*) >= 2
            )
            SELECT agent_name, last_order, days_since::int, ROUND(avg_gap)::int
            FROM agent_stats
            WHERE avg_gap > 0 AND days_since > avg_gap * 1.5
              AND days_since > 14
            ORDER BY days_since DESC
            LIMIT 15
        """)
        churned = cur.fetchall()

    if churned:
        lines.append("⚠️ Возможный отток клиентов:")
        for name, last_day, days_since, avg_gap in churned:
            last_str = last_day.strftime("%d.%m.%Y") if hasattr(last_day, "strftime") else str(last_day)
            lines.append(
                f"  • {name}: посл. заказ {last_str}"
                f" ({days_since} дн. назад, обычно кажд. {avg_gap} дн.)"
            )

    return "\n".join(lines)
