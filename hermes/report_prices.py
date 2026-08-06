"""H3: отчёт качества цен — где не заполнена закупочная цена (buyPrice).

Закупочная цена из карточки товара (blok G) — основа расчёта прибыли и прогноза.
Там, где её нет, прибыль считается по себестоимости МойСклад (менее точно), а в
прогнозе позиция без цены не оценивается в рублях. Отчёт показывает владельцу/Диме,
какие товары «Ассортимент» надо заполнить — в первую очередь те, что реально
продаются.
"""
from __future__ import annotations

from datetime import timedelta

from . import config

_RECENT_DAYS = 30   # «продаётся» = были продажи за последние столько дней
_TOP_PRIORITY = 40  # сколько приоритетных позиций выводить списком


def _rub(kop: float) -> str:
    return f"{kop / 100:,.0f}".replace(",", " ")


def _nal(sale_prices) -> int:
    """Цена «Наличка» из jsonb sale_prices (0, если нет/не число)."""
    if not isinstance(sale_prices, dict):
        return 0
    v = sale_prices.get("Наличка")
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def build_price_quality_report(conn) -> str:
    today = config.msk_today()
    lines: list[str] = [f"🏷 Качество цен на {today.strftime('%d.%m.%Y')} (снимок карточек)"]
    lines.append("Закупочная цена (buyPrice) из карточки нужна для прибыли и прогноза.")
    lines.append("")

    with conn.cursor() as cur:
        cur.execute("SELECT MAX(day) FROM product_price")
        snap_day = cur.fetchone()[0]
    if not snap_day:
        lines.append("Снимка цен нет — запусти sync-prices.")
        return "\n".join(lines)

    with conn.cursor() as cur:
        cur.execute("""
            SELECT pd.product_id, pd.product_name,
                   NULLIF(SPLIT_PART(pd.folder_path, '/', 2), '') AS cat,
                   pd.is_srezka,
                   COALESCE(pp.buy_price_kop, 0) AS buy,
                   pp.sale_prices
            FROM product_dim pd
            LEFT JOIN product_price pp
              ON pp.product_id = pd.product_id AND pp.day = %s
            WHERE pd.folder_path LIKE %s
        """, (snap_day, "Ассортимент/%"))
        rows = cur.fetchall()

        # Кто продавался за последние _RECENT_DAYS — приоритет заполнения.
        cur.execute("""
            SELECT DISTINCT assortment_id
            FROM sales_by_product_day
            WHERE day BETWEEN %s AND %s AND sell_qty > 0
        """, (today - timedelta(days=_RECENT_DAYS), today))
        sold = {r[0] for r in cur.fetchall()}

    total = len(rows)
    if not total:
        lines.append("Товаров «Ассортимент» в справочнике нет.")
        return "\n".join(lines)

    no_buy = [r for r in rows if int(r[4] or 0) == 0]
    have = total - len(no_buy)
    p_have = have / total * 100
    lines.append(f"Товаров «Ассортимент»: {total}")
    lines.append(f"  ✅ с закупочной ценой: {have} ({p_have:.0f}%)")
    lines.append(f"  ⚠️ без закупочной цены: {len(no_buy)} ({100 - p_have:.0f}%)")
    lines.append("")

    # ── Приоритет: продаётся, но нет закупочной цены ──
    priority = [r for r in no_buy if r[0] in sold]
    priority.sort(key=lambda r: (r[2] or "я", r[1]))   # по категории, затем имени
    if priority:
        lines.append("── ⚠️ Продаётся, но нет закупочной цены (заполнить в первую очередь) ──")
        for pid, name, cat, is_srezka, _buy, _sp in priority[:_TOP_PRIORITY]:
            tag = f" [{cat}]" if cat else ""
            lines.append(f"  • {name}{tag}")
        if len(priority) > _TOP_PRIORITY:
            lines.append(f"  … и ещё {len(priority) - _TOP_PRIORITY} продаваемых позиций")
        lines.append("")

    # ── Остальные без цены — сводка по категориям ──
    rest = [r for r in no_buy if r[0] not in sold]
    if rest:
        by_cat: dict[str, int] = {}
        for _pid, _name, cat, _s, _b, _sp in rest:
            by_cat[cat or "(без категории)"] = by_cat.get(cat or "(без категории)", 0) + 1
        parts = [f"{c}: {n}" for c, n in sorted(by_cat.items(), key=lambda x: -x[1])]
        lines.append("── Без закупочной цены и без продаж за 30 дн. (по категориям) ──")
        lines.append("  " + " · ".join(parts))
        lines.append("")

    # ── Аномалия: «Наличка» ниже закупочной (продажа в убыток по опту) ──
    anomalies = []
    for pid, name, cat, is_srezka, buy, sp in rows:
        buy = int(buy or 0)
        nal = _nal(sp)
        if buy > 0 and 0 < nal < buy:
            anomalies.append((name, buy, nal))
    anomalies.sort(key=lambda x: x[1] - x[2], reverse=True)
    if anomalies:
        lines.append("── 🔴 Наличка ниже закупочной (проверить цены) ──")
        for name, buy, nal in anomalies[:20]:
            lines.append(f"  • {name}: закуп {_rub(buy)} ₽ · нал {_rub(nal)} ₽")
        if len(anomalies) > 20:
            lines.append(f"  … и ещё {len(anomalies) - 20} позиций")

    return "\n".join(lines).rstrip()
