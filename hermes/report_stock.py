"""Отчёт «Аналитик остатков» — залежалые по складам, итоги.

Пороги залежалости (решение владельца):
  СРЕЗКА        — 3 дня без продаж
  Все остальные — 30 дней без продаж
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date

STALE_SREZKA_DAYS = 3
STALE_OTHER_DAYS = 30

# Порядок складов в отчёте
_STORE_ORDER = [
    "Киров, Ленина 102А",
    "Слободской, Советская 64",
    "Розница Воровского 107/1",
    "База Воровского 107/1",
    "СОБРАНИЕ",
]


def _rub(kop: float) -> str:
    return f"{kop / 100:,.0f}".replace(",", " ")


def _qty(q: float) -> str:
    if q == int(q):
        return f"{int(q):,}".replace(",", " ")
    return f"{q:,.1f}".replace(",", " ")


def _last_sale_by_product(conn) -> dict[str, date]:
    """Дата последней продажи по product_name за всё время, что есть в БД."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT product_name, MAX(day) FROM sales_by_product_day GROUP BY product_name"
        )
        return {row[0]: row[1] for row in cur.fetchall()}


def _fetch_snapshot(conn, day: date) -> list[dict]:
    """Все позиции с остатком > 0 на дату."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT store_id, store_name, product_name, is_srezka,
                   stock_qty, reserve_qty, available_qty, cost_price_kop
            FROM stock_snapshot
            WHERE day = %s AND stock_qty > 0
            ORDER BY store_name, product_name
            """,
            (day,),
        )
        cols = [c.name for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def build_stock_report(conn, day: date) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM stock_snapshot WHERE day = %s", (day,))
        total_rows = cur.fetchone()[0]

    lines: list[str] = []
    lines.append(f"📦 Остатки на {day.strftime('%d.%m.%Y')}")
    lines.append("")

    if not total_rows:
        lines.append("Снимок остатков за этот день не найден (выгрузка не проводилась).")
        return "\n".join(lines)

    last_sales = _last_sale_by_product(conn)
    rows = _fetch_snapshot(conn, day)

    # Разбиваем по складам
    by_store: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_store[r["store_name"]].append(r)

    # Считаем залежалые + итоги по каждому складу
    stale_srezka_by_store: dict[str, list[dict]] = defaultdict(list)
    stale_other_by_store: dict[str, list[dict]] = defaultdict(list)
    summary: dict[str, dict] = {}

    for store_name, items in by_store.items():
        total_stock = sum(float(i["stock_qty"]) for i in items)
        total_reserve = sum(float(i["reserve_qty"]) for i in items)
        total_cost = sum(float(i["stock_qty"]) * i["cost_price_kop"] for i in items)
        summary[store_name] = {
            "positions": len(items),
            "stock": total_stock,
            "reserve": total_reserve,
            "cost_kop": total_cost,
        }

        for item in items:
            last = last_sales.get(item["product_name"])
            days_idle = (day - last).days if last else 9999
            threshold = STALE_SREZKA_DAYS if item["is_srezka"] else STALE_OTHER_DAYS
            if days_idle < threshold:
                continue
            # Для прочих: только товары с реальной историей продаж
            if not item["is_srezka"] and days_idle == 9999:
                continue
            entry = dict(item)
            entry["days_idle"] = days_idle
            entry["cost_total"] = float(item["stock_qty"]) * item["cost_price_kop"]
            if item["is_srezka"]:
                stale_srezka_by_store[store_name].append(entry)
            else:
                stale_other_by_store[store_name].append(entry)

    def _cost_sort(lst):
        return sorted(lst, key=lambda x: x["cost_total"], reverse=True)

    # --- Залежалые СРЕЗКА ---
    total_stale_srezka = sum(len(v) for v in stale_srezka_by_store.values())
    if total_stale_srezka:
        total_stale_cost = sum(
            e["cost_total"]
            for items in stale_srezka_by_store.values()
            for e in items
        )
        lines.append(
            f"🚨 ЗАЛЕЖАЛЫЕ СРЕЗКА ≥{STALE_SREZKA_DAYS} дн.: "
            f"{total_stale_srezka} поз. · себест. {_rub(total_stale_cost)} ₽"
        )
        lines.append("")

        for store_name in _STORE_ORDER:
            items = _cost_sort(stale_srezka_by_store.get(store_name, []))
            if not items:
                continue
            store_cost = sum(e["cost_total"] for e in items)
            lines.append(f"📍 {store_name} — {len(items)} поз. · {_rub(store_cost)} ₽")
            for e in items:
                idle = f"{e['days_idle']} дн." if e["days_idle"] < 9000 else "нет данных"
                cost_str = f" · {_rub(e['cost_total'])} ₽" if e["cost_total"] else ""
                lines.append(f"   {e['product_name']}: {_qty(e['stock_qty'])} шт · {idle}{cost_str}")
            lines.append("")

    # --- Залежалые прочие ---
    total_stale_other = sum(len(v) for v in stale_other_by_store.values())
    if total_stale_other:
        lines.append(f"⚠️ ЗАЛЕЖАЛЫЕ прочие ≥{STALE_OTHER_DAYS} дн.: {total_stale_other} поз.")
        for store_name in _STORE_ORDER:
            items = _cost_sort(stale_other_by_store.get(store_name, []))
            if not items:
                continue
            lines.append(f"📍 {store_name}")
            for e in items:
                cost_str = f" · {_rub(e['cost_total'])} ₽" if e["cost_total"] else ""
                lines.append(
                    f"   {e['product_name']}: {_qty(e['stock_qty'])} шт "
                    f"· {e['days_idle']} дн.{cost_str}"
                )
        lines.append("")

    if not total_stale_srezka and not total_stale_other:
        lines.append("✅ Залежалых позиций нет.")
        lines.append("")

    # --- Итого по складам ---
    lines.append("─ ИТОГО ПО СКЛАДАМ ─")
    for store_name in _STORE_ORDER:
        s = summary.get(store_name)
        if not s or s["stock"] == 0:
            continue
        # Себест. только реальных товаров (stock ≤ 5000 на позицию — убираем шарные заглушки)
        lines.append(
            f"📍 {store_name}: {_qty(s['stock'])} шт · "
            f"резерв {_qty(s['reserve'])} · себест. {_rub(s['cost_kop'])} ₽"
        )

    return "\n".join(lines)
