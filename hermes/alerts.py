"""Проактивные алерты: аномалии выручки по складам относительно базовой линии."""
from __future__ import annotations

import logging
from datetime import date

log = logging.getLogger("hermes.alerts")

ANOMALY_THRESHOLD = 0.20  # 20% отклонения = триггер


def build_alerts(conn, day: date) -> str | None:
    """Проверить выручку по каждому складу за day.

    Сравниваем с тем же днём недели за последние 8 недель (окно 7–56 дней назад).
    Если хотя бы один склад отклонился на ≥20% — формируем сообщение.
    Возвращает None, если всё в норме или данных недостаточно.
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT s.store_name, s.revenue_kop, AVG(h.revenue_kop) AS baseline
            FROM sales_by_store_day s
            JOIN sales_by_store_day h
              ON h.store_id = s.store_id
             AND h.day BETWEEN s.day - 56 AND s.day - 7
             AND EXTRACT(DOW FROM h.day) = EXTRACT(DOW FROM s.day)
            WHERE s.day = %s
            GROUP BY s.store_name, s.revenue_kop
        """, (day,))
        rows = cur.fetchall()

    if not rows:
        return None

    alerts: list[str] = []
    for store_name, rev, baseline in rows:
        if not baseline or float(baseline) == 0:
            continue
        rev_f  = float(rev or 0)
        base_f = float(baseline)
        diff   = (rev_f - base_f) / base_f
        if abs(diff) < ANOMALY_THRESHOLD:
            continue
        direction = "выше" if diff > 0 else "ниже"
        pct = abs(diff) * 100
        alerts.append(
            f"⚠️ {store_name}: выручка {pct:.0f}% {direction} обычного для этого дня недели"
        )

    if not alerts:
        return None

    log.info("Алерты выручки %s: %d складов", day, len(alerts))
    return "🔔 Обратить внимание:\n\n" + "\n".join(alerts)
