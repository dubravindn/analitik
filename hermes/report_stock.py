"""Отчёт «Аналитик остатков» — залежалые позиции, суммарные остатки, резервы.

Пороги залежалости (решение владельца):
  СРЕЗКА        — 3 дня без продаж
  Все остальные — 30 дней без продаж
"""
from __future__ import annotations

from datetime import date

STALE_SREZKA_DAYS = 3
STALE_OTHER_DAYS = 30


def _rub(kop: int | float) -> str:
    return f"{kop / 100:,.0f}".replace(",", " ")


def _qty(q: float) -> str:
    return f"{q:,.0f}".replace(",", " ")


def _last_sale_by_product(conn, day: date) -> dict[str, date]:
    """Дата последней продажи по product_name за всё время, что есть в БД."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT product_name, MAX(day) AS last_day
            FROM sales_by_product_day
            GROUP BY product_name
            """
        )
        return {row[0]: row[1] for row in cur.fetchall()}


def _fetch_stale(conn, day: date) -> tuple[list[dict], list[dict]]:
    """Вернуть два списка залежалых: СРЕЗКА и прочие.

    Позиции с stock_qty <= 0 не включаем (нет остатка — нечего выделять).
    """
    last_sales = _last_sale_by_product(conn, day)

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT product_name, is_srezka, folder_path,
                   stock_qty, reserve_qty, available_qty, cost_price_kop
            FROM stock_snapshot
            WHERE day = %s AND stock_qty > 0
            ORDER BY is_srezka DESC, stock_qty DESC
            """,
            (day,),
        )
        cols = [c.name for c in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]

    stale_srezka: list[dict] = []
    stale_other: list[dict] = []

    for r in rows:
        last = last_sales.get(r["product_name"])
        if last is None:
            days_idle = 9999  # ни разу не продавалось в имеющейся истории
        else:
            days_idle = (day - last).days

        threshold = STALE_SREZKA_DAYS if r["is_srezka"] else STALE_OTHER_DAYS
        if days_idle < threshold:
            continue

        # Для НЕ-СРЕЗКИ: показываем только те товары, которые реально продавались
        # (есть в истории продаж) и потом залежались. «Никогда не продавалось» — не наш
        # сигнал: это могут быть новые поступления или сервисные позиции.
        if not r["is_srezka"] and days_idle == 9999:
            continue

        r["days_idle"] = days_idle
        if r["is_srezka"]:
            stale_srezka.append(r)
        else:
            stale_other.append(r)

    stale_srezka.sort(key=lambda x: x["days_idle"], reverse=True)
    stale_other.sort(key=lambda x: x["days_idle"], reverse=True)
    return stale_srezka, stale_other


def _fetch_summary(conn, day: date) -> dict:
    """Итоговые цифры: кол-во позиций, остаток штук и себестоимость.

    Исключаем позиции без истории продаж с остатком > 500 шт —
    это сервисные заглушки (надувки, пустые шары и т.п.).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                s.is_srezka,
                COUNT(*)                                        AS positions,
                SUM(s.stock_qty)                                AS total_stock,
                SUM(s.reserve_qty)                             AS total_reserve,
                SUM(s.stock_qty * s.cost_price_kop)             AS total_cost_kop
            FROM stock_snapshot s
            WHERE s.day = %s
              AND s.stock_qty > 0
              AND (
                  s.is_srezka                          -- СРЕЗКА всегда показываем
                  OR s.stock_qty <= 500                -- небольшой остаток — точно реальный товар
                  OR EXISTS (                          -- товар хоть раз продавался
                      SELECT 1 FROM sales_by_product_day sp
                      WHERE sp.product_name = s.product_name
                  )
              )
            GROUP BY s.is_srezka
            ORDER BY s.is_srezka DESC
            """,
            (day,),
        )
        rows = cur.fetchall()
    return {bool(r[0]): {"positions": r[1], "stock": r[2], "reserve": r[3], "cost_kop": r[4]}
            for r in rows}


def build_stock_report(conn, day: date) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM stock_snapshot WHERE day = %s", (day,))
        count = cur.fetchone()[0]

    lines: list[str] = []
    lines.append(f"📦 Остатки на {day.strftime('%d.%m.%Y')}")
    lines.append("")

    if not count:
        lines.append("Снимок остатков за этот день не найден (выгрузка не проводилась).")
        return "\n".join(lines)

    stale_srezka, stale_other = _fetch_stale(conn, day)
    summary = _fetch_summary(conn, day)

    # --- Залежалые СРЕЗКА ---
    if stale_srezka:
        lines.append(f"🚨 ЗАЛЕЖАЛЫЕ СРЕЗКА (≥{STALE_SREZKA_DAYS} дн. без продаж): {len(stale_srezka)} поз.")
        for p in stale_srezka[:15]:
            idle_str = f"{p['days_idle']} дн." if p["days_idle"] < 9000 else "нет данных о продаже"
            cost_total = p["stock_qty"] * p["cost_price_kop"]
            lines.append(
                f"  • {p['product_name']}: {_qty(p['stock_qty'])} шт · {idle_str}"
                + (f" · себест. {_rub(cost_total)} ₽" if cost_total else "")
            )
        if len(stale_srezka) > 15:
            lines.append(f"  ... и ещё {len(stale_srezka) - 15} позиций")
        lines.append("")

    # --- Залежалые прочие ---
    if stale_other:
        lines.append(f"⚠️ ЗАЛЕЖАЛЫЕ прочие (≥{STALE_OTHER_DAYS} дн. без продаж): {len(stale_other)} поз.")
        for p in stale_other[:10]:
            idle_str = f"{p['days_idle']} дн." if p["days_idle"] < 9000 else "нет данных"
            cost_total = p["stock_qty"] * p["cost_price_kop"]
            lines.append(
                f"  • {p['product_name']}: {_qty(p['stock_qty'])} шт · {idle_str}"
                + (f" · себест. {_rub(cost_total)} ₽" if cost_total else "")
            )
        if len(stale_other) > 10:
            lines.append(f"  ... и ещё {len(stale_other) - 10} позиций")
        lines.append("")

    if not stale_srezka and not stale_other:
        lines.append("✅ Залежалых позиций нет.")
        lines.append("")

    # --- Итоговые остатки ---
    lines.append("— ИТОГО В НАЛИЧИИ —")
    for is_srezka_flag, label in [(True, "СРЕЗКА"), (False, "Прочие товары")]:
        s = summary.get(is_srezka_flag)
        if not s:
            continue
        lines.append(
            f"  {label}: {_qty(s['stock'])} шт ({int(s['positions'])} поз.) · "
            f"резерв {_qty(s['reserve'])} · себест. {_rub(s['cost_kop'])} ₽"
        )

    return "\n".join(lines)
