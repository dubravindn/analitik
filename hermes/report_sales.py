"""Отчёт «Аналитик продаж»: лучшие позиции с разбивкой по складам."""
from __future__ import annotations

from datetime import date

from . import calc


def _rub(kop: float) -> str:
    return f"{kop / 100:,.0f}".replace(",", " ")


def _qty(q: float) -> str:
    if q == int(q):
        return f"{int(q):,}".replace(",", " ")
    return f"{q:,.1f}".replace(",", " ")


# ─── Основной аналитический отчёт: лучшие позиции ────────────────────────────

def build_sales_analytics(conn, d_from: date, d_to: date, store_name: str | None = None) -> str:
    """Полная аналитика продаж: итоги + топ товаров, разбивка по складам."""
    days = (d_to - d_from).days + 1
    period_str = (
        d_from.strftime("%d.%m.%Y") if d_from == d_to
        else f"{d_from.strftime('%d.%m.%Y')} – {d_to.strftime('%d.%m.%Y')}"
    )
    store_label = f" · {store_name}" if store_name else " · Все склады"
    lines: list[str] = []
    lines.append(f"📊 Продажи {period_str} ({days} дн.){store_label}")
    if store_name == "СОБРАНИЕ":
        lines.append("ℹ️ СОБРАНИЕ работает через перемещения — прибыль считается "
                     "по отгрузкам, поступление товара см. в «🔄 Перемещения».")
    lines.append("")

    # ── Итоги по складам ──
    sf = "AND store_name = %s" if store_name else ""
    p  = [d_from, d_to] + ([store_name] if store_name else [])

    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT store_name, channel,
                   SUM(revenue_kop) AS rev, SUM(cost_kop) AS cost, SUM(checks) AS chk
            FROM sales_by_store_day
            WHERE day BETWEEN %s AND %s {sf}
            GROUP BY store_name, channel
            ORDER BY channel, SUM(revenue_kop) DESC
        """, p)
        stores = cur.fetchall()

    if not stores:
        lines.append("Нет данных за выбранный период.")
        return "\n".join(lines)

    grand_rev = grand_cost = grand_chk = 0
    for channel in ("розница", "опт", "ресторан"):
        chan = [(sn, ch, rev, cost, chk) for sn, ch, rev, cost, chk in stores if ch == channel]
        if not chan:
            continue
        c_rev  = sum(r[2] for r in chan)
        c_cost = sum(r[3] for r in chan)
        c_chk  = sum(r[4] for r in chan)
        if c_rev == 0:
            continue
        grand_rev  += c_rev
        grand_cost += c_cost
        grand_chk  += c_chk
        gp     = calc.gross_profit(c_rev, c_cost)
        margin = calc.gross_margin_pct(c_rev, c_cost)
        ac     = calc.avg_check(c_rev, c_chk)
        lines.append(f"── {channel.upper()} ──")
        for sn, _, rev, cost, chk in chan:
            if rev == 0:
                continue
            sp = calc.gross_profit(rev, cost)
            sm = calc.gross_margin_pct(rev, cost)
            sa = calc.avg_check(rev, chk)
            lines.append(
                f"  📍 {sn}\n"
                f"     Выручка {_rub(rev)} ₽ · Прибыль {_rub(sp)} ₽ ({sm:.0f}%)\n"
                f"     Чеков {chk} · Ср.чек {_rub(sa)} ₽"
            )
        lines.append(
            f"  Итого: {_rub(c_rev)} ₽ · {_rub(gp)} ₽ ({margin:.0f}%) · {c_chk} чек."
        )
        lines.append("")

    gp_t = calc.gross_profit(grand_rev, grand_cost)
    mg_t = calc.gross_margin_pct(grand_rev, grand_cost)
    ac_t = calc.avg_check(grand_rev, grand_chk)
    lines.append("── ИТОГО ──")
    lines.append(
        f"  Выручка: {_rub(grand_rev)} ₽\n"
        f"  Прибыль: {_rub(gp_t)} ₽ ({mg_t:.0f}%)\n"
        f"  Чеков: {grand_chk} · Ср.чек: {_rub(ac_t)} ₽"
    )
    lines.append("")

    # ── Топ товаров ──
    p2 = [d_from, d_to] + ([store_name] if store_name else [])
    sf2 = ""
    if store_name:
        # Ищем store_id для фильтра product_day (там нет store_name)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT store_id FROM sales_by_store_day WHERE store_name=%s LIMIT 1",
                (store_name,)
            )
            row = cur.fetchone()
        if row:
            sf2 = "AND store_id = %s"
            p2 = [d_from, d_to, row[0]]
        else:
            sf2 = ""
            p2  = [d_from, d_to]

    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT product_name,
                   SUM(sell_qty)    AS qty,
                   SUM(revenue_kop) AS rev,
                   SUM(cost_kop)    AS cost,
                   SUM(profit_kop)  AS profit
            FROM sales_by_product_day
            WHERE day BETWEEN %s AND %s {sf2}
              AND assortment_id IN (
                  SELECT DISTINCT product_id FROM stock_snapshot
                  WHERE folder_path LIKE %s
              )
            GROUP BY product_name
            ORDER BY SUM(revenue_kop) DESC
            LIMIT 20
        """, p2 + ["Ассортимент/%"])
        top_rev = cur.fetchall()

    if top_rev:
        lines.append("🏆 Топ-20 по выручке:")
        for i, (name, qty, rev, cost, profit) in enumerate(top_rev, 1):
            mg = profit / rev * 100 if rev else 0
            lines.append(
                f"  {i:2}. {name}\n"
                f"      {_qty(qty)} ед. · {_rub(rev)} ₽ · прибыль {_rub(profit)} ₽ ({mg:.0f}%)"
            )
        lines.append("")

    # Топ-10 по прибыли (отдельно)
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT product_name,
                   SUM(sell_qty)    AS qty,
                   SUM(revenue_kop) AS rev,
                   SUM(profit_kop)  AS profit
            FROM sales_by_product_day
            WHERE day BETWEEN %s AND %s {sf2}
              AND assortment_id IN (
                  SELECT DISTINCT product_id FROM stock_snapshot
                  WHERE folder_path LIKE %s
              )
            GROUP BY product_name
            ORDER BY SUM(profit_kop) DESC
            LIMIT 10
        """, p2 + ["Ассортимент/%"])
        top_profit = cur.fetchall()

    if top_profit:
        lines.append("💎 Топ-10 по прибыли:")
        for i, (name, qty, rev, profit) in enumerate(top_profit, 1):
            mg = profit / rev * 100 if rev else 0
            lines.append(
                f"  {i:2}. {name}\n"
                f"      {_rub(profit)} ₽ · маржа {mg:.0f}%"
            )

    return "\n".join(lines)


# ─── Дневной / периодный отчёт (для daily push) ───────────────────────────────

def build_day_report(conn, day: date) -> str:
    text = build_sales_analytics(conn, day, day)
    baseline = calc.weekday_baseline(conn, day)
    if baseline:
        avg_kop, n = baseline
        with conn.cursor() as cur:
            cur.execute("SELECT SUM(revenue_kop) FROM sales_by_store_day WHERE day=%s", (day,))
            row = cur.fetchone()
        today_kop = int(row[0] or 0) if row else 0
        diff = calc.delta_pct(today_kop, avg_kop)
        avg_rub = f"{avg_kop / 100:,.0f}".replace(",", " ")
        if diff is not None:
            sign = "+" if diff >= 0 else ""
            text += f"\n\n📊 Обычно ~{avg_rub} ₽ в этот д.н. ({sign}{diff:.0f}% к норме)"
        else:
            text += f"\n\n📊 Обычно ~{avg_rub} ₽ в этот д.н."
    return text


def build_period_report(conn, d_from: date, d_to: date) -> str:
    return build_sales_analytics(conn, d_from, d_to)
