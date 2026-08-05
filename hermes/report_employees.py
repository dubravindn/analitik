"""Отчёт «Аналитика сотрудников» — из /report/profit/byemployee МойСклад.

Не требует ETL: данные запрашиваются напрямую из API и не кешируются.
«Сотрудники» в МойСклад у данной компании — это аккаунты точек продаж.
"""
from __future__ import annotations

from datetime import date

from .moysklad import MoyskladClient

_SKIP_NAMES = {"Цветочная База Дубравиных", "Dubravin&Flowers"}


def _rub(kop: float) -> str:
    return f"{kop / 100:,.0f}".replace(",", " ")


def _pct(v: float) -> str:
    return f"{v * 100:.1f}"


def build_employee_report(client: MoyskladClient, d_from: date, d_to: date) -> str:
    period_str = (
        d_from.strftime("%d.%m.%Y")
        if d_from == d_to
        else f"{d_from.strftime('%d.%m.%Y')} – {d_to.strftime('%d.%m.%Y')}"
    )

    rows_raw = client._get("/report/profit/byemployee", {
        "limit": 100,
        "momentFrom": f"{d_from.isoformat()} 00:00:00",
        "momentTo":   f"{d_to.isoformat()} 23:59:59",
    }).get("rows", [])

    rows = [
        r for r in rows_raw
        if r.get("employee", {}).get("name", "") not in _SKIP_NAMES
        and (r.get("sellSum", 0) > 0 or r.get("returnSum", 0) > 0)
    ]

    lines: list[str] = []
    lines.append(f"👥 Сотрудники за {period_str}")
    lines.append("")

    if not rows:
        lines.append("Нет данных о продажах по сотрудникам за этот период.")
        return "\n".join(lines)

    rows.sort(key=lambda r: r.get("sellSum", 0), reverse=True)

    grand_sell = sum(r.get("sellSum", 0) for r in rows)
    grand_ret  = sum(r.get("returnSum", 0) for r in rows)
    grand_net  = grand_sell - grand_ret
    grand_cost = sum(r.get("sellCostSum", 0) - r.get("returnCostSum", 0) for r in rows)
    grand_prof = grand_net - grand_cost
    grand_margin = grand_prof / grand_net if grand_net else 0

    for i, r in enumerate(rows, 1):
        name = r.get("employee", {}).get("name", "—")
        sell_sum   = r.get("sellSum", 0)
        sell_cost  = r.get("sellCostSum", 0)
        ret_sum    = r.get("returnSum", 0)
        ret_cost   = r.get("returnCostSum", 0)
        sales_cnt  = int(r.get("salesCount", 0) or 0)
        ret_cnt    = int(r.get("returnCount", 0) or 0)
        avg_check  = r.get("salesAvgCheck", 0)
        profit     = r.get("profit", 0)
        margin     = r.get("margin", 0)

        net_rev = sell_sum - ret_sum
        share = net_rev / grand_net * 100 if grand_net else 0

        lines.append(f"{'─' * 30}")
        lines.append(f"{'🥇' if i == 1 else '🥈' if i == 2 else '🥉' if i == 3 else f'{i}.'} {name}")
        lines.append(f"  Выручка:   {_rub(sell_sum)} ₽  ({share:.0f}% от итога)")
        lines.append(f"  Возвраты:  {_rub(ret_sum)} ₽  ({ret_cnt} шт.)")
        lines.append(f"  Нетто:     {_rub(net_rev)} ₽")
        lines.append(f"  Себест.:   {_rub(sell_cost - ret_cost)} ₽")
        lines.append(f"  Прибыль:   {_rub(profit)} ₽  ({_pct(margin)}%)")
        lines.append(f"  Чеков:     {sales_cnt}  · Ср.чек {_rub(avg_check)} ₽")

    lines.append(f"{'─' * 30}")
    lines.append("ИТОГО:")
    lines.append(f"  Нетто:   {_rub(grand_net)} ₽")
    lines.append(f"  Прибыль: {_rub(grand_prof)} ₽  ({_pct(grand_margin)}%)")

    return "\n".join(lines)
