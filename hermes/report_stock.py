"""Отчёт «Аналитик остатков»: позиции по количеству и залежалые по складам."""
from __future__ import annotations

from collections import defaultdict
from datetime import date

STALE_SREZKA_DAYS = 3
STALE_OTHER_DAYS  = 30

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


# Стоимость запаса — по закупочным ценам из приёмок на дату снимка (методика E),
# фолбэк на cost_price_kop МойСклад. Единица = закупочная или фолбэк.
_ST_JOIN = """
    LEFT JOIN LATERAL (
        SELECT price_kop FROM purchase_price_asof p
        WHERE p.product_id = stock_snapshot.product_id AND p.priced_from <= stock_snapshot.day
        ORDER BY p.priced_from DESC LIMIT 1
    ) pp ON true
"""
_ST_UNIT  = "COALESCE(NULLIF(pp.price_kop, 0), stock_snapshot.cost_price_kop)"
_ST_VALUE = f"stock_qty * {_ST_UNIT}"

STALE_SREZKA_MIN_DAYS = 5   # I3: залежалая СРЕЗКА — строго больше 5 дней без продаж


def _two_price(cost_unit) -> str:
    """«закуп X ₽» — закупочная цена из карточки или прочерк."""
    return f"закуп {_rub(cost_unit)} ₽" if cost_unit else "закуп —"


def _supply_days_map(conn):
    """({product_id: дата последней приёмки}, дата первого снимка) — для «дней на складе»."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT si.product_id, MAX(sd.day)
            FROM supply_item si JOIN supply_doc sd ON sd.doc_id = si.doc_id
            WHERE si.product_id IS NOT NULL AND si.product_id != ''
            GROUP BY si.product_id
        """)
        last_supply = {r[0]: r[1] for r in cur.fetchall()}
        cur.execute("SELECT MIN(day) FROM stock_snapshot")
        first_snap = cur.fetchone()[0]
    return last_supply, first_snap


def _render_stock_by_store(conn, lines, day, order):
    """Блок «Остатки по складам» (только «Ассортимент», sentinel-строки отфильтрованы).

    Единый sentinel-фильтр stock_qty < 9999 (тот же, что в прогнозе) — иначе
    служебные заглушки (лента 509 979, шары 9 999) искажают итог.
    """
    lines.append("")
    lines.append("── ОСТАТКИ ПО СКЛАДАМ (Ассортимент, все группы) ──")
    lines.append("(другой срез: весь Ассортимент без фильтра по группе и без исключения резервов)")
    with conn.cursor() as cur:
        for sn in order:
            cur.execute(f"""
                SELECT COUNT(*), SUM(stock_qty), SUM({_ST_VALUE})
                FROM stock_snapshot {_ST_JOIN}
                WHERE day = %s AND stock_qty > 0 AND store_name = %s
                  AND folder_path LIKE %s
            """, [day, sn, "Ассортимент/%"])
            r = cur.fetchone()
            if not r or not r[0]:
                continue
            lines.append(f"  📍 {sn}: {r[0]} поз. · {_qty(float(r[1] or 0))} ед. · {_rub(float(r[2] or 0))} ₽")


# ─── Отчёт «Остатки»: позиции с наибольшим количеством ───────────────────────

def _group_filter(folder_group: str | None) -> tuple[str, list]:
    """Возвращает (SQL-фрагмент WHERE, параметры) для фильтра группы.
    % в LIKE передаётся параметром — psycopg3 не нужно двоить.
    """
    if not folder_group:
        return "", []
    return (
        "AND folder_path LIKE %s AND SPLIT_PART(folder_path, '/', 2) = %s",
        ["Ассортимент/%", folder_group],
    )


def fetch_groups(conn, day: date) -> list[str]:
    """Подгруппы второго уровня внутри 'Ассортимент' за день (для клавиатуры)."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT SPLIT_PART(folder_path, '/', 2)
            FROM stock_snapshot
            WHERE day = %s
              AND folder_path LIKE %s
              AND folder_path IS NOT NULL
              AND folder_path != ''
            ORDER BY 1
        """, [day, "Ассортимент/%"])
        return [r[0] for r in cur.fetchall() if r[0]]


def build_stock_by_qty(
    conn, day: date,
    store_name: str | None = None,
    folder_group: str | None = None,
) -> str:
    """Свободный остаток: только товары БЕЗ резерва (reserve_qty = 0)."""
    store_label = f" · {store_name}" if store_name else " · Все склады"
    group_label = f" · Группа: {folder_group}" if folder_group else ""
    lines: list[str] = []
    lines.append(f"📦 Остатки на {day.strftime('%d.%m.%Y')}{store_label}{group_label}")
    lines.append("(только товары без резерва)")
    lines.append("")

    gf, gp = _group_filter(folder_group)
    sf = "AND store_name = %s" if store_name else ""
    p  = [day] + ([store_name] if store_name else []) + gp

    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT COUNT(*), COALESCE(SUM(stock_qty), 0), COALESCE(SUM({_ST_VALUE}), 0),
                   COUNT(*) FILTER (WHERE pp.price_kop IS NOT NULL AND pp.price_kop > 0)
            FROM stock_snapshot {_ST_JOIN}
            WHERE day = %s AND stock_qty > 0 AND reserve_qty = 0 {sf} {gf}
        """, p)
        row = cur.fetchone()
        total_pos, total_qty, total_cost = row[0], float(row[1] or 0), float(row[2] or 0)
        cov_pos = int(row[3] or 0)

    if not total_pos:
        lines.append("Снимок остатков за этот день не найден.")
        return "\n".join(lines)

    cov_pct = cov_pos / total_pos * 100 if total_pos else 0
    lines.append(
        f"📋 Позиций: {total_pos} · Всего: {_qty(total_qty)} ед. · "
        f"Закуп. стоимость: {_rub(total_cost)} ₽"
    )
    lines.append(f"По закупочным ценам из карточки: {cov_pct:.0f}% позиций · МойСклад: {100 - cov_pct:.0f}%")
    # K2: строка покрытия «Наличка»
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT COUNT(*) FILTER (WHERE np.price_kop IS NOT NULL AND np.price_kop > 0),
                   COUNT(*),
                   COALESCE(SUM(CASE WHEN np.price_kop IS NOT NULL AND np.price_kop > 0
                                    THEN {_ST_VALUE} ELSE 0 END), 0),
                   COALESCE(SUM({_ST_VALUE}), 0)
            FROM stock_snapshot {_ST_JOIN} {_NAL_JOIN}
            WHERE day = %s AND stock_qty > 0 AND reserve_qty = 0 {sf} {gf}
        """, p)
        _nal_r = cur.fetchone() or (0, 0, 0, 0)
    _nal_pos = int(_nal_r[0] or 0)
    _nal_sum = float(_nal_r[2] or 0)
    _nal_all = float(_nal_r[3] or 0)
    if int(_nal_r[1] or 0):
        _pct = _nal_sum / _nal_all * 100 if _nal_all else 0
        _pfx = "⚠️ " if _pct < 90 else ""
        lines.append(f"{_pfx}Наличная цена известна для {_nal_pos} из {int(_nal_r[1])} поз. ({_pct:.0f}% суммы)")
    lines.append("")

    last_supply, first_snap = _supply_days_map(conn)

    def _days_on_shelf(pid) -> str:
        ls = last_supply.get(pid)
        if ls:
            return f"{(day - ls).days} дн."
        if first_snap:
            return f"≥{(day - first_snap).days} дн. (вся история)"
        return "—"

    # Конкретный склад — топ-50 по остатку
    if store_name:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT stock_snapshot.product_id, product_name, is_srezka, stock_qty,
                       {_ST_UNIT} AS cost_unit, {_ST_VALUE} AS cost_total
                FROM stock_snapshot {_ST_JOIN}
                WHERE day = %s AND stock_qty > 0 AND reserve_qty = 0 AND store_name = %s {gf}
                ORDER BY stock_qty DESC
                LIMIT 50
            """, [day, store_name] + gp)
            rows = cur.fetchall()

        lines.append(f"── Топ-{len(rows)} по остатку ──")
        nocost_n = 0
        for pid, name, is_srezka, qty, cost_unit, cost_total in rows:
            tag = " [СР]" if is_srezka else ""
            if not cost_unit:
                nocost_n += 1
            tail = "⚠️ нет закуп. цены" if not cost_unit else _rub(float(cost_total)) + " ₽"
            lines.append(
                f"  • {name}{tag}: {_qty(float(qty))} ед. · {_days_on_shelf(pid)} · "
                f"{_two_price(cost_unit)} · {tail}"
            )
        if nocost_n:
            lines.append("")
            lines.append(f"⚠️ Позиций без закупочной цены: {nocost_n} (заполнить в карточке)")
        lines.append("")
        _render_stock_by_store(conn, lines, day, [store_name])
        return "\n".join(lines)

    # Все склады — разбивка по складам, топ-15
    for sname in _STORE_ORDER:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT stock_snapshot.product_id, product_name, is_srezka, stock_qty,
                       {_ST_UNIT} AS cost_unit, {_ST_VALUE} AS cost_total,
                       COUNT(*) OVER() AS total_cnt,
                       SUM(stock_qty) OVER() AS total_qty,
                       SUM({_ST_VALUE}) OVER() AS total_cost
                FROM stock_snapshot {_ST_JOIN}
                WHERE day = %s AND stock_qty > 0 AND reserve_qty = 0 AND store_name = %s {gf}
                ORDER BY stock_qty DESC
                LIMIT 15
            """, [day, sname] + gp)
            rows = cur.fetchall()

        if not rows:
            continue
        total_cnt  = rows[0][7]
        store_qty  = float(rows[0][8] or 0)
        store_cost = float(rows[0][9] or 0)
        lines.append(f"── 📍 {sname} ──")
        lines.append(
            f"   Позиций: {total_cnt} · {_qty(store_qty)} ед. · {_rub(store_cost)} ₽"
        )
        for pid, name, is_srezka, qty, cost_unit, cost_total, *_ in rows:
            tag = " [СР]" if is_srezka else ""
            tail = "⚠️ нет закуп." if not cost_unit else f"{_rub(float(cost_total))} ₽"
            lines.append(
                f"  • {name}{tag}: {_qty(float(qty))} ед. · {_days_on_shelf(pid)} · "
                f"{_two_price(cost_unit)} · {tail}"
            )
        lines.append("")

    lines.append("─" * 33)
    lines.append("Остатки по складам — весь ассортимент (не только СРЕЗКА, с учётом резерва)")
    lines.append("")
    _render_stock_by_store(conn, lines, day, _STORE_ORDER)

    return "\n".join(lines)


# ─── Отчёт «Резервы» ──────────────────────────────────────────────────────────

def build_reserve_report(conn, day: date, store_name: str | None = None) -> str:
    """Товары в резерве (отложены под клиента)."""
    store_label = f" · {store_name}" if store_name else " · Все склады"
    lines: list[str] = []
    lines.append(f"🎯 Резервы на {day.strftime('%d.%m.%Y')}{store_label}")
    lines.append("")

    sf = "AND store_name = %s" if store_name else ""
    p  = [day] + ([store_name] if store_name else [])

    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT COUNT(*), COALESCE(SUM(reserve_qty), 0),
                   COALESCE(SUM(reserve_qty * cost_price_kop), 0)
            FROM stock_snapshot
            WHERE day = %s AND reserve_qty > 0 {sf}
        """, p)
        cnt, total_qty, total_cost = cur.fetchone()
        total_qty  = float(total_qty or 0)
        total_cost = float(total_cost or 0)

    if not cnt:
        lines.append("Резервов на эту дату нет.")
        return "\n".join(lines)

    lines.append(
        f"📋 Позиций: {cnt} · Всего: {_qty(total_qty)} ед. · Сумма: {_rub(total_cost)} ₽"
    )
    lines.append("")

    order = _STORE_ORDER if not store_name else [store_name]
    for sname in order:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT product_name, is_srezka, reserve_qty, stock_qty,
                       cost_price_kop,
                       reserve_qty * cost_price_kop AS res_cost
                FROM stock_snapshot
                WHERE day = %s AND reserve_qty > 0 AND store_name = %s
                ORDER BY reserve_qty DESC
            """, [day, sname])
            rows = cur.fetchall()

        if not rows:
            continue
        sc = sum(float(r[5]) for r in rows)
        lines.append(f"── 📍 {sname} — {len(rows)} поз. · {_rub(sc)} ₽ ──")
        for name, is_srezka, rqty, sqty, cost_unit, res_cost in rows:
            tag  = " [СР]" if is_srezka else ""
            free = float(sqty) - float(rqty)
            lines.append(
                f"  • {name}{tag}: резерв {_qty(float(rqty))} ед."
                f" / своб. {_qty(free)} ед. · {_rub(float(res_cost))} ₽"
            )
        lines.append("")

    return "\n".join(lines)


# ─── Отчёт «Залежалые» ────────────────────────────────────────────────────────

def build_stock_report(
    conn, day: date,
    store_name: str | None = None,
    folder_group: str | None = None,
    max_items: int | None = None,
) -> str:
    """Залежалые позиции: СРЕЗКА ≥N дн., прочие ≥M дн., разбивка по складам."""
    store_label = f" · {store_name}" if store_name else " · Все склады"
    group_label = f" · Группа: {folder_group}" if folder_group else ""
    lines: list[str] = []
    lines.append(f"🚨 Залежалые СРЕЗКА на {day.strftime('%d.%m.%Y')}{store_label}")
    lines.append(f"   Только СРЕЗКА без продаж > {STALE_SREZKA_MIN_DAYS} дн., по количеству.")
    lines.append("")

    gf, gp = _group_filter("СРЕЗКА")   # I3: только СРЕЗКА
    sf = "AND store_name = %s" if store_name else ""
    p  = [day] + ([store_name] if store_name else []) + gp

    with conn.cursor() as cur:
        cur.execute("SELECT product_name, MAX(day) FROM sales_by_product_day GROUP BY product_name")
        last_sales: dict[str, date] = {row[0]: row[1] for row in cur.fetchall()}

    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT store_name, product_name, stock_qty,
                   {_ST_UNIT} AS cost_unit, {_ST_VALUE} AS cost_total
            FROM stock_snapshot {_ST_JOIN}
            WHERE day = %s AND stock_qty > 0 AND reserve_qty = 0 {sf} {gf}
            ORDER BY store_name, stock_qty DESC
        """, p)
        rows = cur.fetchall()

    stale_srezka: dict[str, list] = defaultdict(list)
    for sname, pname, qty, cost_unit, cost_total in rows:
        last = last_sales.get(pname)
        days_idle = (day - last).days if last else 9999
        if days_idle <= STALE_SREZKA_MIN_DAYS:   # I3: строго > 5 дней
            continue
        stale_srezka[sname].append({
            "name": pname, "qty": float(qty), "cost_unit": cost_unit,
            "cost_total": float(cost_total), "days": days_idle, "nocost": not cost_unit,
        })

    total = sum(len(v) for v in stale_srezka.values())
    if total:
        all_items = [e for items in stale_srezka.values() for e in items]
        total_cost = sum(e["cost_total"] for e in all_items)
        nal_pos = sum(1 for e in all_items if e["nal_unit"])
        nal_cost = sum(e["cost_total"] for e in all_items if e["nal_unit"])
        lines.append(f"🚨 Залежалая СРЕЗКА: {total} поз. · {_rub(total_cost)} ₽")
        if total_cost:
            _pct = nal_cost / total_cost * 100
            _pfx = "⚠️ " if _pct < 90 else ""
            lines.append(f"{_pfx}Наличная цена известна для {nal_pos} из {total} поз. ({_pct:.0f}% суммы)")
        lines.append("")

        # Топ-max_items по стоимости (все склады вместе)
        if max_items is not None and total > max_items:
            # Сортируем all_items по убыванию стоимости, берём топ
            all_sorted   = sorted(all_items, key=lambda x: x["cost_total"], reverse=True)
            shown_idx    = {id(e) for e in all_sorted[:max_items]}
            hidden_n     = total - max_items
            hidden_kop   = sum(e["cost_total"] for e in all_sorted[max_items:])
            stale_shown  = {
                sn: [e for e in items if id(e) in shown_idx]
                for sn, items in stale_srezka.items()
            }
        else:
            stale_shown  = dict(stale_srezka)
            hidden_n, hidden_kop = 0, 0.0

        order = _STORE_ORDER if not store_name else [store_name]
        for sn in order:
            items = sorted(stale_shown.get(sn, []), key=lambda x: x["qty"], reverse=True)
            if not items:
                continue
            sc = sum(e["cost_total"] for e in items)
            lines.append(f"  📍 {sn} — {len(items)} поз. · {_rub(sc)} ₽")
            for e in items:
                idle = ("нет продаж за всю историю" if e["days"] > 90
                        else f"{e['days']} дн. без продаж")
                tail = "⚠️ нет закуп. цены" if e["nocost"] else _rub(e["cost_total"]) + " ₽"
                lines.append(
                    f"    • {e['name']}: {_qty(e['qty'])} ед. · {idle} · "
                    f"{_two_price(e['cost_unit'])} · {tail}"
                )
            lines.append("")

        if hidden_n:
            lines.append(f"  … и ещё {hidden_n} поз. · {_rub(hidden_kop)} ₽ (полный список в PDF)")

    if not stale_srezka:
        lines.append("✅ Залежалой СРЕЗКИ нет.")
        lines.append("")

    # Позиции без закупочной цены — занижают стоимость запаса.
    nocost_n = sum(1 for items in stale_srezka.values() for e in items if e["nocost"])
    if nocost_n:
        lines.append(f"⚠️ Позиций без закупочной цены: {nocost_n} (заполнить в карточке)")

    # Блок «Остатки по складам» перенесён в секцию «Остатки» (I2/I3).
    return "\n".join(lines)
