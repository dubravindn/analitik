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
    lines.append(f"(по закупочным ценам из приёмок: {cov_pct:.0f}% позиций, остальные — себест. МойСклад)")
    lines.append("")

    # Конкретный склад — топ-50 по остатку
    if store_name:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT product_name, is_srezka, stock_qty,
                       {_ST_UNIT} AS cost_unit, {_ST_VALUE} AS cost_total
                FROM stock_snapshot {_ST_JOIN}
                WHERE day = %s AND stock_qty > 0 AND reserve_qty = 0 AND store_name = %s {gf}
                ORDER BY stock_qty DESC
                LIMIT 50
            """, [day, store_name] + gp)
            rows = cur.fetchall()

        lines.append(f"── Топ-{min(50, len(rows))} ──")
        nocost_n = 0
        for name, is_srezka, qty, cost_unit, cost_total in rows:
            tag = " [СР]" if is_srezka else ""
            if not cost_unit:
                nocost_n += 1
                price_line = "    ⚠️ нет себестоимости"
            else:
                price_line = f"    Цена: {_rub(cost_unit)} ₽/ед. · Сумма: {_rub(float(cost_total))} ₽"
            lines.append(f"  • {name}{tag}: {_qty(float(qty))} ед.\n{price_line}")
        if nocost_n:
            lines.append("")
            lines.append(f"⚠️ Позиций без себестоимости: {nocost_n} (стоимость запаса занижена)")
        return "\n".join(lines)

    # Все склады — разбивка по складам, топ-15
    for sname in _STORE_ORDER:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT product_name, is_srezka, stock_qty, {_ST_UNIT} AS cost_unit,
                       {_ST_VALUE} AS cost_total,
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
        total_cnt  = rows[0][5]
        store_qty  = float(rows[0][6] or 0)
        store_cost = float(rows[0][7] or 0)
        lines.append(f"── 📍 {sname} ──")
        lines.append(
            f"   Позиций: {total_cnt} · {_qty(store_qty)} ед. · {_rub(store_cost)} ₽"
        )
        for name, is_srezka, qty, cost_unit, cost_total, *_ in rows:
            tag = " [СР]" if is_srezka else ""
            cost_str = "⚠️ нет себест." if not cost_unit else f"{_rub(float(cost_total))} ₽"
            lines.append(
                f"  • {name}{tag}: {_qty(float(qty))} ед. · {cost_str}"
            )
        lines.append("")

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
) -> str:
    """Залежалые позиции: СРЕЗКА ≥N дн., прочие ≥M дн., разбивка по складам."""
    store_label = f" · {store_name}" if store_name else " · Все склады"
    group_label = f" · Группа: {folder_group}" if folder_group else ""
    lines: list[str] = []
    lines.append(f"🚨 Залежалые на {day.strftime('%d.%m.%Y')}{store_label}{group_label}")
    lines.append(f"   СРЕЗКА ≥{STALE_SREZKA_DAYS} дн. · Прочие ≥{STALE_OTHER_DAYS} дн.")
    lines.append("")

    gf, gp = _group_filter(folder_group)
    sf = "AND store_name = %s" if store_name else ""
    p  = [day] + ([store_name] if store_name else []) + gp

    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT COUNT(*) FROM stock_snapshot
            WHERE day = %s AND stock_qty > 0 AND reserve_qty = 0 {sf} {gf}
        """, p)
        total_rows = cur.fetchone()[0]

    if not total_rows:
        lines.append("Снимок остатков за этот день не найден.")
        return "\n".join(lines)

    with conn.cursor() as cur:
        cur.execute("SELECT product_name, MAX(day) FROM sales_by_product_day GROUP BY product_name")
        last_sales: dict[str, date] = {row[0]: row[1] for row in cur.fetchall()}

    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT store_name, product_name, is_srezka, stock_qty,
                   {_ST_UNIT} AS cost_unit, {_ST_VALUE} AS cost_total
            FROM stock_snapshot {_ST_JOIN}
            WHERE day = %s AND stock_qty > 0 AND reserve_qty = 0 {sf} {gf}
            ORDER BY store_name, stock_qty DESC
        """, p)
        rows = cur.fetchall()

    stale_srezka: dict[str, list] = defaultdict(list)
    stale_other:  dict[str, list] = defaultdict(list)

    for sname, pname, is_srezka, qty, cost_unit, cost_total in rows:
        last = last_sales.get(pname)
        days_idle = (day - last).days if last else 9999
        threshold = STALE_SREZKA_DAYS if is_srezka else STALE_OTHER_DAYS
        if days_idle < threshold:
            continue
        if not is_srezka and days_idle == 9999:
            continue
        entry = {
            "name": pname, "qty": float(qty), "cost_unit": cost_unit,
            "cost_total": float(cost_total), "days": days_idle, "is_srezka": is_srezka,
            "nocost": not cost_unit,   # себестоимость 0/NULL — запас занижен
        }
        if is_srezka:
            stale_srezka[sname].append(entry)
        else:
            stale_other[sname].append(entry)

    def _sort_qty(lst):
        return sorted(lst, key=lambda x: x["qty"], reverse=True)

    def _render_stale(by_store, label, emoji):
        total = sum(len(v) for v in by_store.values())
        if not total:
            return
        total_cost = sum(e["cost_total"] for items in by_store.values() for e in items)
        lines.append(f"{emoji} {label}: {total} поз. · {_rub(total_cost)} ₽")
        lines.append("")
        order = _STORE_ORDER if not store_name else [store_name]
        for sn in order:
            items = _sort_qty(by_store.get(sn, []))
            if not items:
                continue
            sc = sum(e["cost_total"] for e in items)
            lines.append(f"  📍 {sn} — {len(items)} поз. · {_rub(sc)} ₽")
            for e in items:
                idle = f"{e['days']} дн." if e["days"] < 9000 else "нет продаж"
                cost_str = "⚠️ нет себест." if e["nocost"] else f"{_rub(e['cost_total'])} ₽"
                lines.append(
                    f"    • {e['name']}: {_qty(e['qty'])} ед. · {idle} · {cost_str}"
                )
            lines.append("")

    _render_stale(stale_srezka, f"ЗАЛЕЖАЛЫЕ СРЕЗКА ≥{STALE_SREZKA_DAYS} дн.", "🚨")
    _render_stale(stale_other,  f"ЗАЛЕЖАЛЫЕ прочие ≥{STALE_OTHER_DAYS} дн.", "⚠️")

    if not stale_srezka and not stale_other:
        lines.append("✅ Залежалых позиций нет.")
        lines.append("")

    # Позиции без себестоимости — занижают стоимость запаса и завышают маржу.
    nocost_n = sum(
        1 for items in list(stale_srezka.values()) + list(stale_other.values())
        for e in items if e["nocost"]
    )
    if nocost_n:
        lines.append(f"⚠️ Позиций без себестоимости: {nocost_n} (стоимость запаса занижена)")
        lines.append("")

    # Итоги по складам — только товар (папка «Ассортимент»).
    # Служебные позиции (папка «РАБОЧЕЕ»: сборка букета, лента, упаковка, шары)
    # в МойСклад имеют остатки-заглушки (999 999 / 10 000 ед.) и искажали итог
    # на 3–4 порядка (2 млн ед. / 200 млн ₽ по рознице). Считаем только реальный
    # товар из «Ассортимент/%».
    lines.append("── ОСТАТКИ ПО СКЛАДАМ (только товар «Ассортимент», закуп. цены) ──")
    order = _STORE_ORDER if not store_name else [store_name]
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
            cnt, sq, sc = r[0], float(r[1] or 0), float(r[2] or 0)
            lines.append(f"  📍 {sn}: {cnt} поз. · {_qty(sq)} ед. · {_rub(sc)} ₽")

    return "\n".join(lines)
