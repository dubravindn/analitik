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

    # Сводка за период
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT COUNT(DISTINCT agent_id), COUNT(*), COALESCE(SUM(sum_kop), 0)
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

    # Топ-20 по выручке за период
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT agent_name, COUNT(*) AS orders, SUM(sum_kop) AS rev
            FROM sales_doc
            WHERE day BETWEEN %s AND %s {sf}
              AND agent_id IS NOT NULL AND agent_id != ''
            GROUP BY agent_id, agent_name
            ORDER BY SUM(sum_kop) DESC
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

    # Детектор оттока (канал «опт», история 180 дней, ≥3 покупки)
    with conn.cursor() as cur:
        cur.execute("""
            WITH intervals AS (
                SELECT agent_id, agent_name,
                       day - LAG(day) OVER (PARTITION BY agent_id ORDER BY day) AS gap
                FROM (
                    SELECT DISTINCT agent_id, agent_name, day
                    FROM sales_doc
                    WHERE channel = 'опт' AND day >= CURRENT_DATE - 180
                ) d
            ),
            avg_gap AS (
                SELECT agent_id, agent_name,
                       AVG(gap) AS avg_gap_days,
                       COUNT(*) AS purchases
                FROM intervals
                WHERE gap IS NOT NULL
                GROUP BY agent_id, agent_name
                HAVING COUNT(*) >= 3
            ),
            last_seen AS (
                SELECT agent_id, MAX(day) AS last_day
                FROM sales_doc WHERE channel = 'опт'
                GROUP BY agent_id
            )
            SELECT a.agent_name, a.avg_gap_days, l.last_day,
                   CURRENT_DATE - l.last_day AS days_since,
                   (CURRENT_DATE - l.last_day)::float / NULLIF(a.avg_gap_days, 0) AS overdue_ratio
            FROM avg_gap a JOIN last_seen l USING (agent_id)
            WHERE (CURRENT_DATE - l.last_day) > a.avg_gap_days * 1.5
            ORDER BY overdue_ratio DESC
            LIMIT 15
        """)
        churned = cur.fetchall()

    lines.append("")
    lines.append("⚠️ Возможный отток (оптовики):")
    lines.append("Отток — всегда за последние 180 дней, не зависит от выбранного периода.")
    if churned:
        for name, avg_gap, last_day, days_since, ratio in churned:
            last_str = last_day.strftime("%d.%m.%Y") if hasattr(last_day, "strftime") else str(last_day)
            avg_str = f"{float(avg_gap):.0f}"
            lines.append(
                f"  • {name}: посл. заказ {last_str}"
                f" ({int(days_since)} дн. назад, обычно кажд. {avg_str} дн."
                f" — просрочка ×{float(ratio):.1f})"
            )
    else:
        lines.append("✅ Отставших оптовиков не обнаружено")

    return "\n".join(lines)
