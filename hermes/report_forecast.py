"""Прогноз закупки: заказы «Ближайшие заказы» (статус «Под заказ») + прогноз по циклам с праздниками."""
from __future__ import annotations

import logging
import math
import re
import statistics
from datetime import date, timedelta
from typing import Optional

from .moysklad import MoyskladClient
from . import config, calc

log = logging.getLogger("hermes.report_forecast")

_PAGE = 100
_PROJECT_KEYWORD = "ближайшая поставка"
_STATE_KEYWORD   = "под заказ"

# Служебные позиции (шары, услуги) имеют остаток-заглушку в МойСклад
# (9 999 / 10 000 / 999 999). В прогнозе их не учитываем.
_SENTINEL_QTY = 9999


# ─── МойСклад: заказы ────────────────────────────────────────────────────────

def _find_project_href(client: MoyskladClient) -> Optional[str]:
    page = client._get("/entity/project", {"limit": 100})
    for p in page.get("rows", []):
        if _PROJECT_KEYWORD in p.get("name", "").lower():
            return p.get("meta", {}).get("href", "")
    return None


def _find_state_href(client: MoyskladClient) -> Optional[str]:
    """Найти href статуса «Под заказ» из метаданных заказов покупателей."""
    meta = client._get("/entity/customerorder/metadata", {})
    for state in meta.get("states", []):
        if _STATE_KEYWORD in state.get("name", "").lower():
            return state.get("meta", {}).get("href", "")
    return None


def _fetch_orders(client: MoyskladClient, project_href: str,
                  state_href: Optional[str] = None) -> list[dict]:
    orders: list[dict] = []
    offset = 0
    flt = f"project={project_href}"
    if state_href:
        flt += f";state={state_href}"
    while True:
        page = client._get("/entity/customerorder", {
            "limit": _PAGE, "offset": offset,
            "filter": flt,
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

def _van_dates(conn, limit: int = 10, cluster_gap: int = 2) -> list[date]:
    """Даты приходов московского фургона (контрагенты MOSCOW_SUPPLIERS).

    Одна машина дробится на несколько документов приёмки в течение нескольких
    дней (плюс мелкие корректировки между рейсами), поэтому считать каждый
    документ отдельной поставкой нельзя — иначе цикл выходит ~2 дня вместо
    недели. Кластеризуем: приёмки в пределах cluster_gap календарных дней —
    одна поставка, дата фургона = первый день кластера. Возвращаем последние
    ~limit кластеров по возрастанию.
    """
    suppliers = config.MOSCOW_SUPPLIERS
    if not suppliers:
        return []
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT day FROM supply_doc
            WHERE agent_name = ANY(%s)
            ORDER BY day
        """, (suppliers,))
        days = [r[0] for r in cur.fetchall()]
    if not days:
        return []
    clusters: list[date] = [days[0]]   # первый день первого кластера
    prev = days[0]
    for d in days[1:]:
        if (d - prev).days > cluster_gap:
            clusters.append(d)         # разрыв больше окна → новый фургон
        prev = d
    return clusters[-limit:]


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
    """Текущий свободный остаток по категории (без резерва, без служебных заглушек)."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT COALESCE(SUM(ss.available_qty), 0)
            FROM stock_snapshot ss
            JOIN product_dim pd ON pd.product_id = ss.product_id
            WHERE ss.day = (SELECT MAX(day) FROM stock_snapshot)
              AND ss.reserve_qty = 0
              AND ss.available_qty < %s
              AND pd.folder_path LIKE %s
              AND SPLIT_PART(pd.folder_path, '/', 2) = %s
        """, (_SENTINEL_QTY, "Ассортимент/%", category))
        return float(cur.fetchone()[0] or 0)


def _log_sentinel_positions(conn) -> int:
    """Залогировать служебные позиции с остатком-заглушкой, отсечённые из прогноза."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT product_name, available_qty
            FROM stock_snapshot
            WHERE day = (SELECT MAX(day) FROM stock_snapshot)
              AND available_qty >= %s
            ORDER BY available_qty DESC
        """, (_SENTINEL_QTY,))
        rows = cur.fetchall()
    for pn, q in rows:
        log.info("Прогноз: отсечена служебная позиция (остаток-заглушка) — %s: %s ед.", pn, q)
    return len(rows)


# ─── Форматирование ───────────────────────────────────────────────────────────

def _qty(q: float) -> str:
    if q == int(q):
        return f"{int(q):,}".replace(",", " ")
    return f"{q:,.1f}".replace(",", " ")


def _rub(kop: float) -> str:
    return f"{kop / 100:,.0f}".replace(",", " ")


# ─── Позиционный прогноз (SKU) ────────────────────────────────────────────────

_PKG_RE = re.compile(r"(\d+)\s*шт\.?", re.IGNORECASE)


_PKG_MAX = 100   # реальная кратность пучка/упаковки цветов ≤ 100


def _pkg_size(name: str) -> "int | None":
    """Кратность упаковки из названия: «Роза … 25 шт.» → 25. None — не распознано.

    Большие «N шт.» (напр. «Кризал 1000 шт.», «Оазис … 500 шт.») — это содержимое
    коробки, а не кратность заказа: их не округляем (иначе 0.7 → 1000 ед.).
    """
    m = _PKG_RE.search(name or "")
    if not m:
        return None
    n = int(m.group(1))
    return n if 1 <= n <= _PKG_MAX else None


def _orders_by_product(client: MoyskladClient, project_href: str,
                       state_href: Optional[str]) -> dict:
    """Оформленные заказы клиентов из проекта → {product_id: {name, qty}}."""
    by_pid: dict[str, dict] = {}
    for order in _fetch_orders(client, project_href, state_href):
        try:
            positions = _fetch_order_positions(client, order["id"])
        except Exception as e:
            log.warning("Позиции заказа %s недоступны: %s", order.get("id"), e)
            continue
        for pos in positions:
            assort = pos.get("assortment", {}) or {}
            pid = assort.get("id", "")
            if not pid:
                continue
            qty = float(pos.get("quantity", 0) or 0)
            e = by_pid.setdefault(pid, {"name": assort.get("name", ""), "qty": 0.0})
            e["qty"] += qty
    return by_pid


def build_sku_forecast(conn, orders_by_pid: dict, today: date, avg_cycle: int,
                       n_cycles: int = 3, round_pkg: bool = False) -> list[dict]:
    """Позиционный прогноз закупки. Возвращает список SKU с «к заказу».

    К заказу = расход_за_цикл + заказы клиентов − свободный остаток.
    Расход_за_цикл = (продано + списано за последние n_cycles циклов) / n_cycles.
    Свободный остаток = available_qty (резерв уже обещан). Служебные позиции
    (sentinel-остаток, не «Ассортимент») исключены. Если история позиции меньше
    n_cycles циклов — enough_history=False (в отчёте «мало истории»).
    round_pkg — округлять вверх до кратности упаковки (2-й проход).
    """
    win_from = today - timedelta(days=n_cycles * avg_cycle)
    win_to   = today - timedelta(days=1)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT product_id, product_name, folder_path FROM product_dim WHERE folder_path LIKE %s",
            ("Ассортимент/%",),
        )
        dim = {r[0]: (r[1], r[2]) for r in cur.fetchall()}

        cur.execute("""
            SELECT assortment_id, SUM(sell_qty)
            FROM sales_by_product_day WHERE day BETWEEN %s AND %s
            GROUP BY assortment_id
        """, (win_from, win_to))
        sales = {r[0]: float(r[1] or 0) for r in cur.fetchall()}

        cur.execute("""
            SELECT pd.product_id, SUM(li.qty)
            FROM loss_item li
            JOIN loss_doc ld ON ld.doc_id = li.doc_id
            JOIN product_dim pd ON pd.product_name = li.product_name
            WHERE ld.day BETWEEN %s AND %s
            GROUP BY pd.product_id
        """, (win_from, win_to))
        loss = {r[0]: float(r[1] or 0) for r in cur.fetchall()}

        cur.execute("""
            SELECT ss.product_id, SUM(ss.available_qty)
            FROM stock_snapshot ss
            WHERE ss.day = (SELECT MAX(day) FROM stock_snapshot)
              AND ss.reserve_qty = 0 AND ss.available_qty < %s
            GROUP BY ss.product_id
        """, (_SENTINEL_QTY,))
        stock = {r[0]: float(r[1] or 0) for r in cur.fetchall()}

        cur.execute("SELECT assortment_id, MIN(day) FROM sales_by_product_day GROUP BY assortment_id")
        first_sale = {r[0]: r[1] for r in cur.fetchall()}

    prices = calc.purchase_prices_asof(conn, today)   # {pid: price_kop}

    pids = set(sales) | set(loss) | set(orders_by_pid) | set(stock)
    items: list[dict] = []
    for pid in pids:
        if pid not in dim:
            continue   # не реальный товар «Ассортимент» (услуга/шар/расходник)
        name, folder = dim[pid]
        cons_sales = sales.get(pid, 0.0) / n_cycles
        cons_loss  = loss.get(pid, 0.0) / n_cycles
        consumption = cons_sales + cons_loss
        free = stock.get(pid, 0.0)
        ordered = orders_by_pid.get(pid, {}).get("qty", 0.0)
        to_order = consumption + ordered - free
        if to_order <= 0:
            continue

        fs = first_sale.get(pid)
        enough = bool(fs and (today - fs).days >= n_cycles * avg_cycle)

        pkg = _pkg_size(name)
        final = to_order
        packs = None
        if round_pkg and pkg:
            final = math.ceil(to_order / pkg) * pkg
            packs = final / pkg
        price = prices.get(pid, 0)
        parts = folder.split("/")
        cat = parts[1] if len(parts) >= 2 else folder

        items.append({
            "pid": pid, "name": name, "cat": cat,
            "consumption": consumption, "cons_sales": cons_sales, "cons_loss": cons_loss,
            "free": free, "orders": ordered,
            "raw_to_order": to_order, "to_order": final, "packs": packs, "pkg": pkg,
            "enough": enough, "price_kop": price, "buy_kop": round(final * price),
        })
    return items


# ─── Главная функция ──────────────────────────────────────────────────────────

def build_forecast_report(client: MoyskladClient, conn) -> str:
    today = config.msk_today()
    state_note = " · статус «Под заказ»" if True else ""
    lines: list[str] = [
        "🛒 Прогноз закупки",
        f"Прогноз. Основан на заказах в проекте «{_PROJECT_KEYWORD}» и остатках "
        f"на {today.strftime('%d.%m.%Y')}. Не учитывает незафиксированные договорённости.",
        "",
    ]

    # ── 1. Заказы проект «Ближайшие заказы» + статус «Под заказ» ────────────
    project_href = _find_project_href(client)
    state_href   = _find_state_href(client)
    if project_href:
        orders = _fetch_orders(client, project_href, state_href)
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
                lines.append("═══ ПОД ОФОРМЛЕННЫЕ ЗАКАЗЫ ═══")
                lines.append("(дефицит по уже оформленным заказам клиентов, не средний расход)")
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

    # ── 2. Позиционный прогноз закупки на фургон ─────────────────────────────
    van_list = _van_dates(conn)
    if len(van_list) >= 3:
        gaps = [(van_list[i + 1] - van_list[i]).days for i in range(len(van_list) - 1)]
        avg_cycle = round(statistics.median(gaps)) or 7   # медиана устойчивее к провалам
        last_van = van_list[-1]
        next_van  = last_van + timedelta(days=avg_cycle)
        days_left = max((next_van - today).days, 0)

        _log_sentinel_positions(conn)
        orders_by_pid = _orders_by_product(client, project_href, state_href) if project_href else {}
        items = build_sku_forecast(conn, orders_by_pid, today, avg_cycle, n_cycles=3, round_pkg=True)
        holidays = _holidays_in_window(conn, today, next_van)

        # ── Блок 1: хватит ли до фургона ──
        lines.append(f"═══ ХВАТИТ ЛИ ДО ФУРГОНА (осталось {days_left} дн.) ═══")
        lines.append(f"Позиции, которые кончатся раньше {next_van.strftime('%d.%m')} — "
                     f"докупить локально или перекинуть с другой точки.")
        runout = []
        for it in items:
            if not it["enough"]:
                continue
            need = it["consumption"] / avg_cycle * days_left
            short = need - it["free"]
            if short > 0.5:
                runout.append((short, need, it))
        runout.sort(key=lambda x: -x[0])
        if runout:
            for short, need, it in runout[:15]:
                lines.append(f"  • {it['name']}: до фургона нужно ~{_qty(need)}, "
                             f"ост. {_qty(it['free'])} → не хватит ~{_qty(short)}")
        else:
            lines.append("  ✅ Свободных остатков хватает до прихода фургона.")
        lines.append("")

        # ── Блок 2: заказ на фургон (позиционный) ──
        lines.append(f"═══ ЗАКАЗ НА ФУРГОН {next_van.strftime('%d.%m')} (цикл {avg_cycle} дн.) ═══")
        lines.append("Расход за цикл + заказы клиентов − свободный остаток.")
        if holidays:
            lines.append(f"⚠️ Праздники в окне: {', '.join(h['name'] for h in holidays)} — "
                         f"расход может быть выше среднего.")
        lines.append("")

        ok_items  = sorted([it for it in items if it["enough"]], key=lambda x: -x["buy_kop"])
        malo_items = [it for it in items if not it["enough"]]
        _TOP = 40
        shown, rest = ok_items[:_TOP], ok_items[_TOP:]

        by_cat: dict[str, list] = {}
        for it in shown:
            by_cat.setdefault(it["cat"], []).append(it)
        star = False
        for cat, its in by_cat.items():
            lines.append(f"── {cat} ──")
            for it in its:
                if it["packs"]:
                    pkg_str = f" ({_qty(it['packs'])} упак.)"
                else:
                    pkg_str = " *"; star = True
                lines.append(f"  • {it['name']}: расход/цикл {_qty(it['consumption'])} "
                             f"(прод. {_qty(it['cons_sales'])} + спис. {_qty(it['cons_loss'])}) · "
                             f"ост. {_qty(it['free'])} · заказы {_qty(it['orders'])}")
                lines.append(f"      → К ЗАКАЗУ {_qty(it['to_order'])} ед.{pkg_str}"
                             + (f"  ≈{_rub(it['buy_kop'])} ₽" if it['buy_kop'] else ""))
                loss_share = it['cons_loss'] / it['consumption'] if it['consumption'] else 0
                if loss_share >= 0.3:
                    lines.append(f"      ⚠️ {loss_share*100:.0f}% расхода — списание, "
                                 f"заказывать столько нельзя (пересмотреть объём)")
            lines.append(f"  Итого по {cat}: {len(its)} поз. · ≈{_rub(sum(i['buy_kop'] for i in its))} ₽")
            lines.append("")
        if rest:
            lines.append(f"… и ещё {len(rest)} позиций на ≈{_rub(sum(i['buy_kop'] for i in rest))} ₽ "
                         f"(полный список — по кнопке «🛒 Прогноз»)")
            lines.append("")
        if star:
            lines.append("* кратность упаковки не распознана из названия — заказать вручную")
        if malo_items:
            names = ", ".join(i["name"] for i in malo_items[:8])
            more = "…" if len(malo_items) > 8 else ""
            lines.append(f"ℹ️ Мало истории (<3 циклов), не прогнозируем: {len(malo_items)} поз. — {names}{more}")

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
