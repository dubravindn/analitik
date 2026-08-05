"""Отчёт «Аналитик продаж» — считает по локальной БД и собирает текст по шаблону.

Текст детерминированный: одни и те же данные всегда дают один и тот же отчёт,
слово в слово. Никаких обращений к языковым моделям.
"""
from __future__ import annotations

from datetime import date

from . import calc


def _rub(kop: int) -> str:
    """Форматирует копейки как рубли с разделителями тысяч: 3593100 → '35 931'."""
    return f"{kop / 100:,.0f}".replace(",", " ")


def fetch_day(conn, day: date) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT store_name, channel, revenue_kop, cost_kop, checks,
                   positions_total, positions_nocost
            FROM sales_by_store_day
            WHERE day = %s
            ORDER BY channel, store_name
            """,
            (day,),
        )
        cols = [c.name for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def top_products(conn, day: date, by: str = "revenue", limit: int = 20) -> list[dict]:
    order_col = "revenue_kop" if by == "revenue" else "profit_kop"
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT product_name,
                   SUM(sell_qty)     AS qty,
                   SUM(revenue_kop)  AS revenue_kop,
                   SUM(profit_kop)   AS profit_kop
            FROM sales_by_product_day
            WHERE day = %s
            GROUP BY product_name
            ORDER BY SUM({order_col}) DESC
            LIMIT %s
            """,
            (day, limit),
        )
        cols = [c.name for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def build_day_report(conn, day: date) -> str:
    stores = fetch_day(conn, day)
    lines: list[str] = []
    lines.append(f"📊 Продажи за {day.strftime('%d.%m.%Y')}")
    lines.append("")

    if not stores:
        lines.append("Данных за этот день нет (выгрузка не проводилась).")
        return "\n".join(lines)

    # По каналам: розница / опт / ресторан
    grand_rev = grand_cost = grand_checks = 0
    for channel in ("розница", "опт", "ресторан"):
        chan_stores = [s for s in stores if s["channel"] == channel]
        if not chan_stores:
            continue
        crev = sum(s["revenue_kop"] for s in chan_stores)
        ccost = sum(s["cost_kop"] for s in chan_stores)
        cchecks = sum(s["checks"] for s in chan_stores)
        if crev == 0 and cchecks == 0:
            continue
        grand_rev += crev
        grand_cost += ccost
        grand_checks += cchecks

        lines.append(f"— {channel.upper()} —")
        for s in chan_stores:
            if s["revenue_kop"] == 0 and s["checks"] == 0:
                continue
            gp = calc.gross_profit(s["revenue_kop"], s["cost_kop"])
            margin = calc.gross_margin_pct(s["revenue_kop"], s["cost_kop"])
            ac = calc.avg_check(s["revenue_kop"], s["checks"])
            warn = ""
            if s["positions_total"]:
                nocost_share = 100 * s["positions_nocost"] / s["positions_total"]
                if nocost_share >= 10:
                    warn = f"  ⚠️ без себест.: {nocost_share:.0f}% позиций"
            lines.append(
                f"  {s['store_name']}: выручка {_rub(s['revenue_kop'])} ₽ · "
                f"прибыль {_rub(gp)} ₽ ({margin:.0f}%) · "
                f"чеков {s['checks']} · ср.чек {_rub(ac)} ₽{warn}"
            )
        lines.append("")

    gp_total = calc.gross_profit(grand_rev, grand_cost)
    margin_total = calc.gross_margin_pct(grand_rev, grand_cost)
    ac_total = calc.avg_check(grand_rev, grand_checks)
    lines.append("— ИТОГО —")
    lines.append(
        f"  Выручка {_rub(grand_rev)} ₽ · Грязная прибыль {_rub(gp_total)} ₽ "
        f"({margin_total:.0f}%) · Чеков {grand_checks} · Ср.чек {_rub(ac_total)} ₽"
    )
    lines.append("")

    # Топ-5 товаров по выручке (для дневного отчёта; полный топ-20 — в недельном)
    top = top_products(conn, day, by="revenue", limit=5)
    if top:
        lines.append("🏆 Топ-5 по выручке:")
        for i, p in enumerate(top, 1):
            lines.append(
                f"  {i}. {p['product_name']}: {_rub(p['revenue_kop'])} ₽ "
                f"(прибыль {_rub(p['profit_kop'])} ₽)"
            )

    return "\n".join(lines)
