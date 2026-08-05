"""Проактивные алерты: аномалии выручки относительно базовой линии."""
from __future__ import annotations

import logging
from datetime import date

from . import calc

log = logging.getLogger("hermes.alerts")

ANOMALY_THRESHOLD_PCT = 20.0  # % отклонения для триггера алерта


def build_alerts(conn, day: date) -> str | None:
    """Проверить выручку за day.

    Если отклонение от базовой линии (тот же д.н. за 8 нед.) > 20% — вернуть текст алерта.
    Если данных нет или отклонение в норме — вернуть None.
    """
    baseline = calc.weekday_baseline(conn, day)
    if not baseline:
        log.debug("Нет базовой линии для %s", day)
        return None

    avg_kop, n_weeks = baseline

    with conn.cursor() as cur:
        cur.execute(
            "SELECT SUM(revenue_kop) FROM sales_by_store_day WHERE day=%s",
            (day,),
        )
        row = cur.fetchone()
    today_kop = int(row[0] or 0) if row else 0

    if avg_kop == 0 or today_kop == 0:
        return None

    diff = calc.delta_pct(today_kop, avg_kop)
    if diff is None or abs(diff) < ANOMALY_THRESHOLD_PCT:
        return None

    def rub(kop: int) -> str:
        return f"{kop / 100:,.0f}".replace(",", " ")

    sign = "+" if diff >= 0 else ""
    emoji = "🚀" if diff > 0 else "🔴"
    log.info("Алерт выручки %s: факт=%d норма=%d delta=%.1f%%", day, today_kop, avg_kop, diff)
    return (
        f"{emoji} Аномалия выручки за {day.strftime('%d.%m.%Y')}\n"
        f"Факт: {rub(today_kop)} ₽  |  Норма: ~{rub(avg_kop)} ₽\n"
        f"Отклонение: {sign}{diff:.0f}%  (база: {n_weeks} нед.)"
    )
