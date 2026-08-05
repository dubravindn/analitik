"""Прогноз закупки: заказы с проектом «Ближайшая поставка» + исторический контекст."""
from __future__ import annotations

import logging
from datetime import date, timedelta

from .moysklad import MoyskladClient

log = logging.getLogger("hermes.report_forecast")

_PAGE = 100
_PROJECT_KEYWORD = "ближайшая поставка"


def _find_project_href(client: MoyskladClient) -> str | None:
    page = client._get("/entity/project", {"limit": 100})
    for p in page.get("rows", []):
        if _PROJECT_KEYWORD in p.get("name", "").lower():
            return p.get("meta", {}).get("href", "")
    return None


def _fetch_orders(client: MoyskladClient, project_href: str) -> list[dict]:
    orders: list[dict] = []
    offset = 0
    while True:
        page = client._get("/entity/customerorder", {
            "limit": _PAGE, "offset": offset,
            "filter": f"project={project_href}",
            "order": "moment,desc",
        })
        batch = page.get("rows", [])
        orders.extend(batch)
        size = page.get("meta", {}).get("size", 0)
        offset += len(batch)
        if offset >= size or not batch:
            break
    return orders


def _fetch_positions(client: MoyskladClient, order_id: str) -> list[dict]:
    rows: list[dict] = []
    offset = 0
    while True:
        page = client._get(f"/entity/customerorder/{order_id}/positions", {
            "limit": 100, "offset": offset, "expand": "assortment",
        })
        batch = page.get("rows", [])
        rows.extend(batch)
        size = page.get("meta", {}).get("size", 0)
        offset += len(batch)
        if offset >= size or not batch:
            break
    return rows


def _van_cycle_info(conn) -> tuple[list[date], float | None]:
    """Даты последних приемок от московских поставщиков + средний цикл (дней)."""
    from . import config
    suppliers = config.MOSCOW_SUPPLIERS
    if not suppliers:
        return [], None
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT day FROM supply_doc
            WHERE agent_name = ANY(%s)
            ORDER BY day DESC
            LIMIT 10
        """, (suppliers,))
        dates = [row[0] for row in cur.fetchall()]
    if len(dates) < 2:
        return dates, None
    dates_sorted = sorted(dates)
    gaps = [(dates_sorted[i + 1] - dates_sorted[i]).days for i in range(len(dates_sorted) - 1)]
    avg_cycle = sum(gaps) / len(gaps)
    return dates, avg_cycle


def _sales_by_product(conn, d_from: date, d_to: date) -> dict[str, float]:
    """Продажи по товарам за период (только группа Ассортимент)."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT spd.product_name, SUM(spd.sell_qty)
            FROM sales_by_product_day spd
            JOIN product_dim pd ON pd.product_id = spd.assortment_id
            WHERE spd.day BETWEEN %s AND %s
              AND pd.folder_path LIKE %s
            GROUP BY spd.product_name
        """, (d_from, d_to, "Ассортимент/%"))
        return {row[0]: float(row[1] or 0) for row in cur.fetchall()}


def _qty(q: float) -> str:
    if q == int(q):
        return f"{int(q):,}".replace(",", " ")
    return f"{q:,.1f}".replace(",", " ")


def _rub(kop: float) -> str:
    return f"{kop / 100:,.0f}".replace(",", " ")


def build_forecast_report(client: MoyskladClient, conn) -> str:
    today     = date.today()
    week_ago  = today - timedelta(days=7)
    year_ago  = today - timedelta(days=365)

    lines: list[str] = ["🛒 Прогноз закупки — ближайшая поставка", ""]

    # 1. Найти проект
    project_href = _find_project_href(client)
    if not project_href:
        lines.append(
            f"⚠️ Проект «{_PROJECT_KEYWORD}» не найден в МойСклад.\n"
            "Создай проект с таким названием и привязывай к нему заказы под фургон."
        )
        return "\n".join(lines)

    # 2. Загрузить заказы
    orders = _fetch_orders(client, project_href)
    if not orders:
        lines.append("Нет активных заказов с этим проектом.")
        return "\n".join(lines)

    lines.append(f"📋 Заказов в проекте: {len(orders)}")
    lines.append("")

    # 3. Агрегировать позиции по товарам
    demand: dict[str, dict] = {}
    for order in orders:
        order_id = order["id"]
        try:
            positions = _fetch_positions(client, order_id)
        except Exception as e:
            log.warning("Позиции заказа %s недоступны: %s", order_id, e)
            continue
        for pos in positions:
            assort = pos.get("assortment", {})
            name   = assort.get("name", "—")
            qty    = float(pos.get("quantity", 0) or 0)
            price  = round(pos.get("price", 0) or 0)
            if name not in demand:
                demand[name] = {"qty": 0.0, "sum_kop": 0}
            demand[name]["qty"]     += qty
            demand[name]["sum_kop"] += round(qty * price)

    if not demand:
        lines.append("В заказах нет позиций.")
        return "\n".join(lines)

    # 4. Текущие остатки (последний снимок в БД, без резерва)
    stock: dict[str, float] = {}
    with conn.cursor() as cur:
        cur.execute("""
            SELECT product_name, SUM(available_qty)
            FROM stock_snapshot
            WHERE day = (SELECT MAX(day) FROM stock_snapshot)
              AND reserve_qty = 0
            GROUP BY product_name
        """)
        for pname, avail in cur.fetchall():
            stock[pname] = float(avail or 0)

    # 5. Продажи за прошлую неделю и прошлый год (тот же 7-дн. отрезок)
    sales_week     = _sales_by_product(conn, week_ago,              today - timedelta(days=1))
    sales_year_ago = _sales_by_product(conn, year_ago - timedelta(days=3), year_ago + timedelta(days=3))

    # 6. Список «нужно докупить»
    rows_sorted = sorted(demand.items(), key=lambda x: -x[1]["qty"])

    need_buy: list[tuple] = []
    ok_list:  list[tuple] = []

    for name, d in rows_sorted:
        ordered = d["qty"]
        on_hand = stock.get(name, 0.0)
        to_buy  = max(0.0, ordered - on_hand)
        wk      = sales_week.get(name, 0.0)
        yr      = sales_year_ago.get(name, 0.0)
        if to_buy > 0:
            need_buy.append((name, ordered, on_hand, to_buy, wk, yr, d["sum_kop"]))
        else:
            ok_list.append((name, ordered, on_hand, wk, yr))

    total_ordered = sum(d["qty"]     for d in demand.values())
    total_kop     = sum(d["sum_kop"] for d in demand.values())
    lines.append(
        f"Всего заказано: {_qty(total_ordered)} ед. · ≈{_rub(total_kop)} ₽"
    )
    lines.append("")

    if need_buy:
        lines.append(f"🛒 Нужно докупить ({len(need_buy)} поз.):")
        for name, ordered, on_hand, to_buy, wk, yr, kop in need_buy:
            line = f"  • {name}: {_qty(to_buy)} ед."
            line += f"  (заказ {_qty(ordered)}, остаток {_qty(on_hand)})"
            ctx = []
            if wk > 0:
                ctx.append(f"прошл.нед. {_qty(wk)}")
            if yr > 0:
                ctx.append(f"год назад {_qty(yr)}")
            if ctx:
                line += f"  [{', '.join(ctx)}]"
            lines.append(line)
        lines.append("")

    if ok_list:
        lines.append(f"✅ Покрыто остатком ({len(ok_list)} поз.):")
        for name, ordered, on_hand, wk, yr in ok_list:
            line = f"  • {name}: {_qty(ordered)} ед. — остаток {_qty(on_hand)}"
            ctx = []
            if wk > 0:
                ctx.append(f"прошл.нед. {_qty(wk)}")
            if yr > 0:
                ctx.append(f"год назад {_qty(yr)}")
            if ctx:
                line += f"  [{', '.join(ctx)}]"
            lines.append(line)

    # 7. История приемок из supply_doc
    van_dates, avg_cycle = _van_cycle_info(conn)
    if van_dates:
        lines.append("")
        lines.append("── История приемок (фургоны) ──")
        last_van = sorted(van_dates)[-1]
        days_since = (today - last_van).days
        lines.append(f"  Последняя: {last_van.strftime('%d.%m.%Y')} ({days_since} дн. назад)")
        if avg_cycle:
            next_est = last_van + timedelta(days=round(avg_cycle))
            lines.append(
                f"  Средний цикл: {avg_cycle:.0f} дн."
                f" · Следующая ≈ {next_est.strftime('%d.%m.%Y')}"
            )
        lines.append(f"  Приемок в истории: {len(van_dates)}")

    return "\n".join(lines)
