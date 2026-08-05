"""Прогноз закупки: заказы «Ближайшая поставка» + категорийный прогноз с поправкой на праздники."""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Optional

from .moysklad import MoyskladClient

log = logging.getLogger("hermes.report_forecast")

_PAGE = 100
_PROJECT_KEYWORD = "ближайшая поставка"


# ─── МойСклад: заказы ────────────────────────────────────────────────────────

def _find_project_href(client: MoyskladClient) -> Optional[str]:
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


def _fetch_order_positions(client: MoyskladClient, order_id: str) -> list[dict]:
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


# ─── БД: фургоны, категории, праздники ───────────────────────────────────────

def _van_dates(conn, limit: int = 10) -> list[date]:
    from . import config
    suppliers = config.MOSCOW_SUPPLIERS
    if not suppliers:
        return []
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT day FROM supply_doc
            WHERE agent_name = ANY(%s)
            ORDER BY day DESC
            LIMIT %s
        """, (suppliers, limit))
        return sorted([r[0] for r in cur.fetchall()])


def _categories(conn) -> list[str]:
    """Все категории второго уровня из product_dim (Ассортимент/...)."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT SPLIT_PART(folder_path, '/', 2)
            FROM product_dim
            WHERE folder_path LIKE %s
              AND folder_path IS NOT NULL AND folder_path != ''
            ORDER BY 1
        """, ("Ассортимент/%",))
        return [r[0] for r in cur.fetchall() if r[0]]


def _category_daily_avg(conn, category: str, d_from: date, d_to: date) -> float:
    """Средний суточный расход по категории (продажи + списания) за период."""
    days = (d_to - d_from).days + 1
    if days <= 0:
        return 0.0

    # Продажи через product_dim
    with conn.cursor() as cur:
        cur.execute("""
            SELECT COALESCE(SUM(spd.sell_qty), 0)
            FROM sales_by_product_day spd
            JOIN product_dim pd ON pd.product_id = spd.assortment_id
            WHERE spd.day BETWEEN %s AND %s
              AND pd.folder_path LIKE %s
              AND SPLIT_PART(pd.folder_path, '/', 2) = %s
        """, (d_from, d_to, "Ассортимент/%", category))
        sales_qty = float(cur.fetchone()[0] or 0)

    # Списания через product_name (folder_path в loss_item — UUID, не путь)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT COALESCE(SUM(li.qty), 0)
            FROM loss_item li
            JOIN loss_doc ld ON ld.doc_id = li.doc_id
            JOIN product_dim pd ON pd.product_name = li.product_name
            WHERE ld.day BETWEEN %s AND %s
              AND pd.folder_path LIKE %s
              AND SPLIT_PART(pd.folder_path, '/', 2) = %s
        """, (d_from, d_to, "Ассортимент/%", category))
        loss_qty = float(cur.fetchone()[0] or 0)

    return (sales_qty + loss_qty) / days


def _holidays_in_window(conn, d_from: date, d_to: date) -> list[dict]:
    """Праздники, чей ажиотажный период пересекается с [d_from, d_to]."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT holiday_date, name, lead_days, fallback_multiplier
            FROM holiday
            WHERE holiday_date - (lead_days || ' days')::interval <= %s
              AND holiday_date >= %s
            ORDER BY holiday_date
        """, (d_to, d_from))
        return [
            {"date": r[0], "name": r[1], "lead_days": r[2], "fallback": float(r[3])}
            for r in cur.fetchall()
        ]


def _holiday_multiplier(conn, holiday: dict, category: str) -> tuple[float, str]:
    """Фактический множитель по категории за прошлый год или fallback."""
    hdate     = holiday["date"]
    lead_days = holiday["lead_days"]
    try:
        prev_date = hdate.replace(year=hdate.year - 1)
    except ValueError:
        return holiday["fallback"], "справочник"

    window_from = prev_date - timedelta(days=lead_days)
    window_to   = prev_date

    holiday_avg  = _category_daily_avg(conn, category, window_from, window_to)
    baseline_avg = _category_daily_avg(
        conn, category,
        window_from - timedelta(days=21),
        window_from - timedelta(days=1),
    )

    if baseline_avg > 0 and holiday_avg > 0:
        return holiday_avg / baseline_avg, f"факт {prev_date.year} г."
    return holiday["fallback"], "справочник (нет истории)"


def _category_stock(conn, category: str) -> float:
    """Текущий свободный остаток по категории (без резерва)."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT COALESCE(SUM(ss.available_qty), 0)
            FROM stock_snapshot ss
            JOIN product_dim pd ON pd.product_id = ss.product_id
            WHERE ss.day = (SELECT MAX(day) FROM stock_snapshot)
              AND ss.reserve_qty = 0
              AND pd.folder_path LIKE %s
              AND SPLIT_PART(pd.folder_path, '/', 2) = %s
        """, ("Ассортимент/%", category))
        return float(cur.fetchone()[0] or 0)


# ─── Форматирование ───────────────────────────────────────────────────────────

def _qty(q: float) -> str:
    if q == int(q):
        return f"{int(q):,}".replace(",", " ")
    return f"{q:,.1f}".replace(",", " ")


def _rub(kop: float) -> str:
    return f"{kop / 100:,.0f}".replace(",", " ")


# ─── Главная функция ──────────────────────────────────────────────────────────

def build_forecast_report(client: MoyskladClient, conn) -> str:
    today = date.today()
    lines: list[str] = ["🛒 Прогноз закупки — ближайшая поставка", ""]

    # ── 1. Заказы с проектом «Ближайшая поставка» ────────────────────────────
    project_href = _find_project_href(client)
    if project_href:
        orders = _fetch_orders(client, project_href)
        if orders:
            lines.append(f"📋 Заказов в проекте: {len(orders)}")

            demand: dict[str, dict] = {}
            for order in orders:
                try:
                    positions = _fetch_order_positions(client, order["id"])
                except Exception as e:
                    log.warning("Позиции заказа %s недоступны: %s", order["id"], e)
                    continue
                for pos in positions:
                    assort = pos.get("assortment", {})
                    name  = assort.get("name", "—")
                    qty   = float(pos.get("quantity", 0) or 0)
                    price = round(pos.get("price", 0) or 0)
                    if name not in demand:
                        demand[name] = {"qty": 0.0, "sum_kop": 0}
                    demand[name]["qty"]     += qty
                    demand[name]["sum_kop"] += round(qty * price)

            if demand:
                # Остатки для сравнения
                stock_by_name: dict[str, float] = {}
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT product_name, SUM(available_qty)
                        FROM stock_snapshot
                        WHERE day = (SELECT MAX(day) FROM stock_snapshot)
                          AND reserve_qty = 0
                        GROUP BY product_name
                    """)
                    for pn, av in cur.fetchall():
                        stock_by_name[pn] = float(av or 0)

                # Продажи прошлая неделя и год назад
                week_ago = today - timedelta(days=7)
                year_ago = today - timedelta(days=365)

                def _sales_period(d_from: date, d_to: date) -> dict[str, float]:
                    with conn.cursor() as cur:
                        cur.execute("""
                            SELECT spd.product_name, SUM(spd.sell_qty)
                            FROM sales_by_product_day spd
                            JOIN product_dim pd ON pd.product_id = spd.assortment_id
                            WHERE spd.day BETWEEN %s AND %s
                              AND pd.folder_path LIKE %s
                            GROUP BY spd.product_name
                        """, (d_from, d_to, "Ассортимент/%"))
                        return {r[0]: float(r[1] or 0) for r in cur.fetchall()}

                sales_week = _sales_period(week_ago, today - timedelta(days=1))
                sales_year = _sales_period(year_ago - timedelta(days=3), year_ago + timedelta(days=3))

                total_ordered = sum(d["qty"]     for d in demand.values())
                total_kop     = sum(d["sum_kop"] for d in demand.values())
                lines.append(f"Всего заказано: {_qty(total_ordered)} ед. · ≈{_rub(total_kop)} ₽")
                lines.append("")

                need_buy = [(n, d, stock_by_name.get(n, 0.0)) for n, d in
                            sorted(demand.items(), key=lambda x: -x[1]["qty"])
                            if d["qty"] - stock_by_name.get(n, 0.0) > 0]
                ok_list  = [(n, d, stock_by_name.get(n, 0.0)) for n, d in
                            sorted(demand.items(), key=lambda x: -x[1]["qty"])
                            if d["qty"] - stock_by_name.get(n, 0.0) <= 0]

                if need_buy:
                    lines.append(f"🛒 Нужно докупить ({len(need_buy)} поз.):")
                    for name, d, on_hand in need_buy:
                        to_buy = d["qty"] - on_hand
                        ctx = []
                        if sales_week.get(name, 0) > 0:
                            ctx.append(f"прошл.нед. {_qty(sales_week[name])}")
                        if sales_year.get(name, 0) > 0:
                            ctx.append(f"год назад {_qty(sales_year[name])}")
                        line = f"  • {name}: {_qty(to_buy)} ед.  (заказ {_qty(d['qty'])}, ост. {_qty(on_hand)})"
                        if ctx:
                            line += f"  [{', '.join(ctx)}]"
                        lines.append(line)
                    lines.append("")

                if ok_list:
                    lines.append(f"✅ Покрыто остатком ({len(ok_list)} поз.):")
                    for name, d, on_hand in ok_list:
                        lines.append(f"  • {name}: {_qty(d['qty'])} ед. — ост. {_qty(on_hand)}")
                    lines.append("")
    else:
        lines.append(f"⚠️ Проект «{_PROJECT_KEYWORD}» не найден в МойСклад.")
        lines.append("")

    # ── 2. Категорийный прогноз по циклам с поправкой на праздники ───────────
    van_list = _van_dates(conn)
    if len(van_list) >= 3:
        last_van = van_list[-1]
        gaps = [(van_list[i + 1] - van_list[i]).days for i in range(len(van_list) - 1)]
        avg_cycle = round(sum(gaps) / len(gaps))
        next_van  = last_van + timedelta(days=avg_cycle)
        days_left = (next_van - today).days

        lines.append("── Категорийный прогноз по циклам ──")
        lines.append(
            f"  Последняя приемка: {last_van.strftime('%d.%m.%Y')}"
            f" | Цикл: ~{avg_cycle} дн."
            f" | Следующая: ~{next_van.strftime('%d.%m.%Y')}"
            f" (через {max(days_left, 0)} дн.)"
        )
        lines.append("")

        # Праздники в окне прогноза
        holidays = _holidays_in_window(conn, today, next_van)
        if holidays:
            hnames = ", ".join(h["name"] for h in holidays)
            lines.append(f"  ⚠️ Праздники в окне: {hnames}")
            lines.append("")

        # По каждой категории
        categories = _categories(conn)
        if not categories:
            lines.append("  Нет данных в product_dim — запусти sync-stock для заполнения.")
        else:
            # Базовый период: последние 2–3 цикла (без текущего)
            if len(van_list) >= 4:
                base_from = van_list[-4]
            else:
                base_from = van_list[0]
            base_to = last_van - timedelta(days=1)

            for cat in categories:
                base_daily = _category_daily_avg(conn, cat, base_from, base_to)
                if base_daily == 0:
                    continue

                # Рассчитываем прогноз с учётом праздников
                # Для каждого дня в окне [today, next_van) определяем множитель
                total_forecast = 0.0
                holiday_notes: list[str] = []

                # Помечаем дни как «ажиотажные» (по max-множителю)
                day_mult: dict[date, tuple[float, str]] = {}
                for h in holidays:
                    mult, src = _holiday_multiplier(conn, h, cat)
                    h_start = h["date"] - timedelta(days=h["lead_days"])
                    d = max(h_start, today)
                    while d <= min(h["date"], next_van):
                        if d not in day_mult or mult > day_mult[d][0]:
                            day_mult[d] = (mult, f"{h['name']} ×{mult:.1f} ({src})")
                        d += timedelta(days=1)

                d = today
                while d < next_van:
                    if d in day_mult:
                        mult, _ = day_mult[d]
                        total_forecast += base_daily * mult
                    else:
                        total_forecast += base_daily
                    d += timedelta(days=1)

                # Уникальные заметки о праздниках
                seen_notes: set[str] = set()
                for mult, note in day_mult.values():
                    if note not in seen_notes:
                        holiday_notes.append(note)
                        seen_notes.add(note)

                stock = _category_stock(conn, cat)
                to_buy = max(0.0, total_forecast - stock)
                norm = base_daily * avg_cycle

                line = (
                    f"  📦 {cat}: обычно ~{_qty(norm)} ед./цикл"
                    f" → прогноз {_qty(total_forecast)} ед."
                    f" | ост. {_qty(stock)} | докупить {_qty(to_buy)} ед."
                )
                lines.append(line)
                for note in holiday_notes:
                    lines.append(f"     ⚠️ {note}")

            lines.append("")

    elif van_list:
        last_van = van_list[-1]
        lines.append(
            f"  ⚠️ Недостаточно истории приемок для прогноза по циклам"
            f" (нужно ≥3, найдено {len(van_list)})."
            f" Последняя: {last_van.strftime('%d.%m.%Y')}."
        )
    else:
        lines.append(
            "  ⚠️ Нет данных о приемках от московских поставщиков в supply_doc."
            " Проверь MOSCOW_SUPPLIERS в config.py."
        )

    return "\n".join(lines)
