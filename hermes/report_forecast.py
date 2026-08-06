"""Прогноз закупки (J4): пять блоков в фиксированном порядке.

1. ЧТО ЗАКАЗАНО — заказы клиентов (проект «Ближайшая поставка», статус «Под заказ»).
2. ЗАКАЗАНО МИНУС ОСТАТОК — сколько из заказанного не покрыто свободным остатком.
3. ПРОГНОЗ СПРОСА — продажи за прошлую неделю и тот же период год назад.
4. ИТОГО К ЗАКАЗУ НА ФУРГОН — прогноз спроса + заказы − свободный остаток,
   округление до упаковки (только СРЕЗКА). Списания в расчёт НЕ входят.
5. ЧТО БРАТЬ НЕ НАДО — остатка хватает надолго или позиция не продаётся.
"""
from __future__ import annotations

import logging
import math
import re
from datetime import date, timedelta
from typing import Optional

from .moysklad import MoyskladClient
from . import config

log = logging.getLogger("hermes.report_forecast")

_PAGE = 100
_PROJECT_KEYWORD = "ближайшая поставка"
_STATE_KEYWORD   = "под заказ"

# Служебные позиции (шары, услуги) имеют остаток-заглушку в МойСклад
# (9 999 / 10 000 / 999 999). В прогнозе их не учитываем.
_SENTINEL_QTY = 9999

# Блок 5: остатка «хватает надолго», если он ≥ этого числа недельных спросов.
_OVERSTOCK_WEEKS = 2
# Блок 5(б): залежалая СРЕЗКА — строго больше стольких дней без продаж.
_STALE_SREZKA_DAYS = 5

# Лимиты вывода на блок (защита от лимита Telegram 4096).
_TOP_DEMAND = 30
_TOP_ORDER  = 40
_TOP_NOBUY  = 25


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


def _orders_detail(client: MoyskladClient, project_href: str,
                   state_href: Optional[str]) -> dict:
    """Оформленные заказы клиентов → {product_id: {name, qty, n_orders}}.

    n_orders — в скольких разных заказах встречается позиция (для «(N заказов)»).
    """
    by_pid: dict[str, dict] = {}
    for order in _fetch_orders(client, project_href, state_href):
        oid = order.get("id", "")
        try:
            positions = _fetch_order_positions(client, oid)
        except Exception as e:
            log.warning("Позиции заказа %s недоступны: %s", oid, e)
            continue
        for pos in positions:
            assort = pos.get("assortment", {}) or {}
            pid = (assort.get("id", "") or "").split("?")[0]
            if not pid:
                continue
            qty = float(pos.get("quantity", 0) or 0)
            e = by_pid.setdefault(pid, {"name": assort.get("name", ""),
                                        "qty": 0.0, "orders": set()})
            e["qty"] += qty
            e["orders"].add(oid)
    # свернуть set заказов в число
    for e in by_pid.values():
        e["n_orders"] = len(e.pop("orders"))
    return by_pid


# ─── БД: фургоны, праздники ──────────────────────────────────────────────────

def _van_dates(conn, limit: int = 10, cluster_gap: int = 2) -> list[date]:
    """Даты приходов московского фургона (контрагенты MOSCOW_SUPPLIERS).

    Одна машина дробится на несколько документов приёмки за несколько дней,
    поэтому кластеризуем: приёмки в пределах cluster_gap дней — одна поставка.
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
    clusters: list[date] = [days[0]]
    prev = days[0]
    for d in days[1:]:
        if (d - prev).days > cluster_gap:
            clusters.append(d)
        prev = d
    return clusters[-limit:]


def _holidays_in_window(conn, d_from: date, d_to: date) -> list[str]:
    """Названия праздников, чьё ажиотажное окно пересекает [d_from, d_to]."""
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT name FROM holiday
                WHERE holiday_date - (lead_days || ' days')::interval <= %s
                  AND holiday_date >= %s
                ORDER BY holiday_date
            """, (d_to, d_from))
            return [r[0] for r in cur.fetchall()]
    except Exception:
        return []


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


_PKG_RE = re.compile(r"(\d+)\s*шт\.?", re.IGNORECASE)
_PKG_MAX = 100   # реальная кратность пучка/упаковки цветов ≤ 100


def _pkg_size(name: str) -> "int | None":
    """Кратность упаковки из названия: «Роза … 25 шт.» → 25. None — не распознано.

    Большие «N шт.» (напр. «Кризал 1000 шт.») — содержимое коробки, не кратность.
    """
    m = _PKG_RE.search(name or "")
    if not m:
        return None
    n = int(m.group(1))
    return n if 1 <= n <= _PKG_MAX else None


# ─── Сбор данных по товарам (Ассортимент) ─────────────────────────────────────

def _gather(conn, today: date, orders_by_pid: dict) -> dict:
    """Собрать по product_id: имя, категория, заказы, свободный остаток,
    продажи за прошлую неделю и год назад, дату последней продажи."""
    week_from = today - timedelta(days=7)
    week_to   = today - timedelta(days=1)
    year_from = week_from - timedelta(days=365)
    year_to   = week_to - timedelta(days=365)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT product_id, product_name, folder_path FROM product_dim "
            "WHERE folder_path LIKE %s", ("Ассортимент/%",),
        )
        dim = {r[0]: (r[1], r[2]) for r in cur.fetchall()}

        cur.execute("""
            SELECT assortment_id, SUM(sell_qty)
            FROM sales_by_product_day WHERE day BETWEEN %s AND %s
            GROUP BY assortment_id
        """, (week_from, week_to))
        sales_week = {r[0]: float(r[1] or 0) for r in cur.fetchall()}

        cur.execute("""
            SELECT assortment_id, SUM(sell_qty)
            FROM sales_by_product_day WHERE day BETWEEN %s AND %s
            GROUP BY assortment_id
        """, (year_from, year_to))
        sales_year = {r[0]: float(r[1] or 0) for r in cur.fetchall()}

        cur.execute("""
            SELECT ss.product_id, SUM(ss.available_qty)
            FROM stock_snapshot ss
            WHERE ss.day = (SELECT MAX(day) FROM stock_snapshot)
              AND ss.reserve_qty = 0 AND ss.available_qty < %s
            GROUP BY ss.product_id
        """, (_SENTINEL_QTY,))
        free = {r[0]: float(r[1] or 0) for r in cur.fetchall()}

        cur.execute("SELECT assortment_id, MAX(day) FROM sales_by_product_day GROUP BY assortment_id")
        last_sale = {r[0]: r[1] for r in cur.fetchall()}

        cur.execute("SELECT MIN(day) FROM sales_by_product_day")
        min_sales_day = cur.fetchone()[0]

    # Есть ли история продаж за прошлогоднее окно (иначе честная пометка).
    has_year = bool(min_sales_day and min_sales_day <= year_to)

    items: dict[str, dict] = {}
    pids = set(dim) & (set(sales_week) | set(sales_year) | set(free) | set(orders_by_pid))
    for pid in pids:
        if pid not in dim:
            continue
        name, folder = dim[pid]
        parts = folder.split("/")
        cat = parts[1] if len(parts) >= 2 else folder
        items[pid] = {
            "name": name, "cat": cat,
            "ordered": orders_by_pid.get(pid, {}).get("qty", 0.0),
            "n_orders": orders_by_pid.get(pid, {}).get("n_orders", 0),
            "free": free.get(pid, 0.0),
            "week": sales_week.get(pid, 0.0),
            "year": sales_year.get(pid, 0.0),
            "last_sale": last_sale.get(pid),
        }
    return {"items": items, "has_year": has_year,
            "week_from": week_from, "week_to": week_to}


# ─── Главная функция ──────────────────────────────────────────────────────────

def build_forecast_report(client: MoyskladClient, conn) -> str:
    today = config.msk_today()
    lines: list[str] = [f"🛒 Прогноз закупки на {today.strftime('%d.%m.%Y')}", ""]

    # Методика (I7) — полными словами, без сокращений.
    lines += [
        "Как читать прогноз (пять блоков):",
        "1. Что заказано — товары, которые клиенты уже заказали.",
        "2. Заказано минус остаток — сколько из заказанного не покрыто остатком.",
        "3. Прогноз спроса — продажи за прошлую неделю и тот же период год назад.",
        "4. Итого к заказу на фургон — прогноз спроса плюс заказы минус свободный",
        "   остаток, с округлением до упаковки (только срезка). Списания не входят.",
        "5. Что брать не надо — остатка хватает надолго или товар не продаётся.",
        "Остатки и заказы — на текущий момент, от периода отчёта не зависят.",
        "",
    ]

    _log_sentinel_positions(conn)

    project_href = _find_project_href(client)
    state_href   = _find_state_href(client)
    orders_by_pid: dict = {}
    if project_href:
        orders_by_pid = _orders_detail(client, project_href, state_href)
    else:
        lines.append(f"⚠️ Проект «{_PROJECT_KEYWORD}» в МойСклад не найден — "
                     f"блоки заказов пустые.")
        lines.append("")

    data = _gather(conn, today, orders_by_pid)
    items = data["items"]
    has_year = data["has_year"]

    # ── Блок 1. ЧТО ЗАКАЗАНО ──────────────────────────────────────────────────
    lines.append("═══ 1. ЧТО ЗАКАЗАНО ═══")
    lines.append("Заказы клиентов: проект «Ближайшая поставка» + статус «Под заказ».")
    ordered_items = sorted([it for it in items.values() if it["ordered"] > 0],
                           key=lambda x: -x["ordered"])
    if ordered_items:
        for it in ordered_items:
            no = it["n_orders"]
            suff = f" ({no} заказ.)" if no else ""
            lines.append(f"  • {it['name']}: {_qty(it['ordered'])} ед.{suff}")
        tot_qty = sum(it["ordered"] for it in ordered_items)
        lines.append(f"  Итого: {len(ordered_items)} поз. · {_qty(tot_qty)} ед.")
    else:
        lines.append("  Оформленных заказов нет.")
    lines.append("")

    # ── Блок 2. ЗАКАЗАНО МИНУС ОСТАТОК ────────────────────────────────────────
    lines.append("═══ 2. ЗАКАЗАНО МИНУС ОСТАТОК ═══")
    lines.append("Что реально нужно докупить под уже оформленные заказы.")
    short_items = sorted(
        [(it["ordered"] - it["free"], it) for it in items.values()
         if it["ordered"] > 0 and it["ordered"] - it["free"] > 0],
        key=lambda x: -x[0],
    )
    if short_items:
        for gap, it in short_items:
            lines.append(f"  • {it['name']}: заказано {_qty(it['ordered'])} · "
                         f"свободный остаток {_qty(it['free'])} → докупить {_qty(gap)}")
        lines.append("  (позиции, где остатка хватает, сюда не входят)")
    else:
        lines.append("  Всё заказанное покрыто свободным остатком.")
    lines.append("")

    # ── Блок 3. ПРОГНОЗ СПРОСА ────────────────────────────────────────────────
    lines.append("═══ 3. ПРОГНОЗ СПРОСА ═══")
    lines.append("Продажи за прошлую неделю и тот же период год назад.")
    if not has_year:
        lines.append("(истории за прошлый год пока нет — будет после загрузки истории)")
    movers = sorted([it for it in items.values() if it["week"] > 0],
                    key=lambda x: -x["week"])
    if movers:
        for it in movers[:_TOP_DEMAND]:
            if has_year and it["year"] > 0:
                pct = (it["week"] / it["year"] - 1) * 100
                sign = "+" if pct >= 0 else "−"
                yr = f"год назад {_qty(it['year'])} ед. ({sign}{abs(pct):.0f}%)"
            elif has_year:
                yr = "год назад 0 ед."
            else:
                yr = "год назад — нет данных"
            lines.append(f"  • {it['name']}: прошлая неделя {_qty(it['week'])} ед. · {yr}")
        if len(movers) > _TOP_DEMAND:
            lines.append(f"  … и ещё {len(movers) - _TOP_DEMAND} позиций с продажами")
    else:
        lines.append("  Продаж за прошлую неделю нет.")
    lines.append("")

    # ── Блок 4. ИТОГО К ЗАКАЗУ НА ФУРГОН ──────────────────────────────────────
    van = _van_dates(conn)
    van_hint = ""
    if len(van) >= 2:
        gaps = [(van[i + 1] - van[i]).days for i in range(len(van) - 1)]
        cyc = round(sorted(gaps)[len(gaps) // 2]) or 7
        next_van = van[-1] + timedelta(days=cyc)
        if next_van >= today:
            van_hint = f" (ближайший фургон ≈ {next_van.strftime('%d.%m')})"
    lines.append(f"═══ 4. ИТОГО К ЗАКАЗУ НА ФУРГОН{van_hint} ═══")
    lines.append("Прогноз спроса + заказано − свободный остаток, округление до "
                 "упаковки (только срезка).")
    holidays = _holidays_in_window(conn, today, today + timedelta(days=8))
    if holidays:
        lines.append(f"⚠️ Впереди праздник: {', '.join(holidays)} — спрос может быть выше.")

    to_order = []
    for it in items.values():
        raw = it["week"] + it["ordered"] - it["free"]
        if raw <= 0:
            continue
        pkg = _pkg_size(it["name"]) if it["cat"] == "СРЕЗКА" else None
        if pkg:
            final = math.ceil(raw / pkg) * pkg
            packs = final // pkg
        else:
            final = math.ceil(raw)
            packs = None
        if final < 1:
            continue
        to_order.append({**it, "raw": raw, "final": final, "packs": packs})
    to_order.sort(key=lambda x: -x["final"])

    if to_order:
        # N7: полный итог до обрезки по лимиту
        tot_all_qty = sum(it["final"] for it in to_order)
        if len(to_order) > _TOP_ORDER:
            lines.append(
                f"  Показаны топ-{_TOP_ORDER} по объёму. "
                f"Итого по всем {len(to_order)} поз.: {_qty(tot_all_qty)} ед."
            )
        for it in to_order[:_TOP_ORDER]:
            pk = f" ({it['packs']} упак.)" if it["packs"] else ""
            lines.append(
                f"  • {it['name']}: {_qty(it['week'])} + {_qty(it['ordered'])} − "
                f"{_qty(it['free'])} = {_qty(it['raw'])} → "
                f"К ЗАКАЗУ {_qty(it['final'])} ед.{pk}"
            )
        shown_qty = sum(it["final"] for it in to_order[:_TOP_ORDER])
        if len(to_order) > _TOP_ORDER:
            rest = len(to_order) - _TOP_ORDER
            rest_qty = tot_all_qty - shown_qty
            lines.append(f"  … и ещё {rest} поз. · {_qty(rest_qty)} ед. (полный список — в PDF)")
        else:
            lines.append(f"  Итого к заказу: {_qty(shown_qty)} ед.")
    else:
        lines.append("  Докупать нечего: спрос и заказы покрыты остатком.")
    lines.append("")

    # ── Блок 5. ЧТО БРАТЬ НЕ НАДО ─────────────────────────────────────────────
    lines.append("═══ 5. ЧТО БРАТЬ НЕ НАДО ═══")
    lines.append("Остатка хватает с запасом или позиция не продаётся.")
    nobuy: list[str] = []
    # (а) остаток ≥ 2× недельного спроса
    over = sorted(
        [it for it in items.values()
         if it["week"] > 0 and it["free"] >= _OVERSTOCK_WEEKS * it["week"]],
        key=lambda x: -x["free"],
    )
    for it in over:
        weeks = int(it["free"] // it["week"]) if it["week"] else 0
        nobuy.append(f"  • {it['name']}: остаток {_qty(it['free'])} · "
                     f"продажи/нед {_qty(it['week'])} — запас на {weeks}+ недель")
    # (б) залежалая СРЕЗКА > 5 дн. без продаж, но с остатком
    stale = []
    for it in items.values():
        if it["cat"] != "СРЕЗКА" or it["free"] <= 0 or it["week"] > 0:
            continue
        last = it["last_sale"]
        idle = (today - last).days if last else None
        if idle is None or idle > _STALE_SREZKA_DAYS:
            stale.append((idle if idle is not None else 10 ** 6, it))
    stale.sort(key=lambda x: -x[0])
    for idle, it in stale:
        when = f"без продаж {idle} дн." if idle < 10 ** 6 else "нет продаж за всю историю"
        nobuy.append(f"  • {it['name']}: {_qty(it['free'])} ед. {when} — "
                     f"не брать, продавать остаток")

    if nobuy:
        for ln in nobuy[:_TOP_NOBUY]:
            lines.append(ln)
        if len(nobuy) > _TOP_NOBUY:
            lines.append(f"  … и ещё {len(nobuy) - _TOP_NOBUY} позиций")
    else:
        lines.append("  Явных излишков не найдено.")

    return "\n".join(lines).rstrip()
