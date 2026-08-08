"""Отчёт «Аналитик продаж»: лучшие позиции с разбивкой по складам."""
from __future__ import annotations

import statistics
from datetime import date, timedelta

from . import calc, config
from .costs import unit_cost


def _rub(kop: float) -> str:
    return f"{kop / 100:,.0f}".replace(",", " ")


def _qty(q: float) -> str:
    if q == int(q):
        return f"{int(q):,}".replace(",", " ")
    return f"{q:,.1f}".replace(",", " ")


def _active_holiday(conn, d_from: date, d_to: date) -> str | None:
    """Название праздника, чьё окно [дата − lead_days, дата] пересекает период."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT name FROM holiday
            WHERE holiday_date >= %s AND (holiday_date - lead_days) <= %s
            ORDER BY holiday_date LIMIT 1
        """, (d_from, d_to))
        row = cur.fetchone()
    return row[0] if row else None


def _store_id_for(conn, store_name: str | None) -> str | None:
    if not store_name:
        return None
    with conn.cursor() as cur:
        cur.execute("SELECT store_id FROM sales_by_store_day WHERE store_name=%s LIMIT 1",
                    (store_name,))
        r = cur.fetchone()
    return r[0] if r else None


def _sales_purchase_data(
    conn, d_from: date, d_to: date,
    store_id: str | None = None,
    discount_pids: frozenset | None = None,
) -> dict:
    """Продажи за период с закупочной ценой по единому правилу.

    Закуп: карточка товара → цена продажи × 0.6 (фолбэк).
    Если discount_pids передан — к этим товарам применяется скидка 7% (× 0.93).
    Возвращает агрегаты: by_store, by_product, tot + разбивку покрытия.
    by_store[sid]: "pc" = эффективный закуп, "pc_raw" = до скидки, "discount" = сумма скидки.
    """
    from . import config
    sf = "AND spd.store_id = %s" if store_id else ""
    params = [d_from, d_to] + ([store_id] if store_id else [])
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT spd.store_id, spd.product_name, spd.assortment_id,
                   spd.sell_qty, spd.revenue_kop, pp.price_kop
            FROM sales_by_product_day spd
            LEFT JOIN LATERAL (
                SELECT price_kop FROM purchase_price_asof p
                WHERE p.product_id = spd.assortment_id AND p.priced_from <= spd.day
                ORDER BY p.priced_from DESC LIMIT 1
            ) pp ON true
            WHERE spd.day BETWEEN %s AND %s {sf} AND spd.sell_qty > 0
        """, params)
        rows = cur.fetchall()

    by_store: dict[str, dict] = {}
    by_product: dict[str, dict] = {}
    _z = {"rev": 0, "pc": 0, "pc_raw": 0, "discount": 0, "card": 0, "estimate": 0}
    tot = {**_z}

    disc = discount_pids or frozenset()
    mult = config.SUPPLIER_DISCOUNT_MULTIPLIER

    for sid, name, assort_id, q, rk, card_kop in rows:
        q = float(q); rk = int(rk or 0)
        if card_kop and int(card_kop) > 0:
            raw = round(q * int(card_kop))
            if assort_id and assort_id in disc:
                discount = round(raw * config.SUPPLIER_DISCOUNT_RATE)
            else:
                discount = 0
            cost = raw - discount
            source = "card"
        else:
            raw = round(rk * 0.6)   # revenue × 0.6 = фолбэк «продажа − 40%»
            discount = 0
            cost = raw
            source = "estimate"

        st = by_store.setdefault(sid, {**_z})
        st["rev"] += rk; st["pc"] += cost; st["pc_raw"] += raw
        st["discount"] += discount; st[source] = st.get(source, 0) + rk

        pr = by_product.setdefault(name, {"qty": 0.0, "rev": 0, "pc": 0, "source": "card"})
        pr["qty"] += q; pr["rev"] += rk; pr["pc"] += cost
        if source != "card":
            pr["source"] = source

        tot["rev"] += rk; tot["pc"] += cost; tot["pc_raw"] += raw
        tot["discount"] += discount; tot[source] = tot.get(source, 0) + rk

    r = tot["rev"] or 1
    tot["cov_card"]     = tot["card"]     / r * 100
    tot["cov_estimate"] = tot["estimate"] / r * 100
    tot["cov_missing"]  = 0.0            # обратная совместимость (всегда 0)
    tot["coverage"]     = tot["cov_card"] + tot["cov_estimate"]  # = 100
    return {"by_store": by_store, "by_product": by_product, "tot": tot}


def _attention_block(conn, lines, d_from, d_to, retail_margins, mixed, stores, pc_fn):
    """G5: чистую розницу сравниваем с медианой розницы; смешанные точки — со
    своей историей (медиана маржи за 8 таких же прошлых периодов)."""
    flags: list[str] = []

    # 1. Чистые розничные точки против медианы розницы (разрыв в ₽).
    if len(retail_margins) >= 2:
        med = statistics.median(m for _, m, _ in retail_margins)
        for sn, m, rev in retail_margins:
            if med > 0 and m < med / 2:
                gap = round(rev * (med - m) / 100)
                flags.append(f"• {sn}: маржа {m:.0f}% против {med:.0f}% медианы розницы — "
                             f"разрыв ≈{_rub(gap)} ₽")

    # 2. Смешанные точки (опт+розница) — со своей историей.
    n_days = (d_to - d_from).days + 1
    for sid, sn, channel, rev, chk in stores:
        if sn not in mixed or rev == 0:
            continue
        cur_m = (rev - pc_fn(sid)) / rev * 100
        hist: list[float] = []
        for k in range(1, 9):
            d = _sales_purchase_data(conn, d_from - timedelta(days=n_days * k),
                                     d_to - timedelta(days=n_days * k), sid)
            r = d["tot"]["rev"]
            if r > 0:
                hist.append((r - d["tot"]["pc"]) / r * 100)
        if len(hist) >= 3:
            hm = statistics.median(hist)
            if hm > 0 and cur_m < hm / 1.5:
                flags.append(f"• {sn}: маржа {cur_m:.0f}% против обычных {hm:.0f}% — проверить")

    if flags:
        lines.append("⚠️ Требует внимания")
        lines.extend("  " + f for f in flags)
        lines.append("")


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

    # Основная методика (блок E): себестоимость по закупочным ценам из приёмок
    # на дату продажи, фолбэк на себест. МойСклад для непокрытых позиций.
    store_id_f = _store_id_for(conn, store_name)
    pdata = _sales_purchase_data(conn, d_from, d_to, store_id_f)
    by_store, by_product, tot = pdata["by_store"], pdata["by_product"], pdata["tot"]

    # J1.1: методику в шапке — полными словами, каждая мысль отдельной строкой.
    cov_card = tot["cov_card"]
    card_pct = round(cov_card)
    est_pct  = 100 - card_pct
    lines.append("Как считается прибыль: выручка минус закупочная стоимость товара.")
    lines.append("Расходы и списания в этой прибыли не учтены — они в своих разделах.")
    lines.append(
        f"Прибыль посчитана точно у {card_pct}% продаж (закупка из карточки). "
        f"У остальных {est_pct}% закупка оценена как цена продажи − 40%."
    )
    if store_name == "СОБРАНИЕ":
        lines.append("ℹ️ СОБРАНИЕ работает через перемещения — прибыль считается "
                     "по отгрузкам, поступление товара см. в «🔄 Перемещения».")
    lines.append("")

    # ── Итоги по складам (выручка/чеки — из sales_by_store_day; себест. — закупочная) ──
    sf = "AND store_name = %s" if store_name else ""
    p  = [d_from, d_to] + ([store_name] if store_name else [])

    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT store_id, store_name, channel,
                   SUM(revenue_kop) AS rev, SUM(checks) AS chk
            FROM sales_by_store_day
            WHERE day BETWEEN %s AND %s {sf}
            GROUP BY store_id, store_name, channel
            ORDER BY channel, SUM(revenue_kop) DESC
        """, p)
        stores = cur.fetchall()

    if not stores:
        lines.append("Нет данных за выбранный период.")
        return "\n".join(lines)

    def _pc(sid):   # закупочная себестоимость склада (с фолбэком уже внутри by_store)
        return by_store.get(sid, {}).get("pc", 0)

    mixed = set(config.MIXED_CHANNEL_STORES or [])
    retail_margins: list[tuple[str, float, int]] = []   # (склад, маржа%, разрыв-база ₽) чистой розницы

    grand_rev = grand_cost = grand_chk = 0
    for channel in ("розница", "опт", "ресторан"):
        chan = [r for r in stores if r[2] == channel]
        c_rev = sum(r[3] for r in chan)
        if c_rev == 0:
            continue
        c_cost = sum(_pc(r[0]) for r in chan)
        c_chk  = sum(r[4] for r in chan)
        grand_rev += c_rev; grand_cost += c_cost; grand_chk += c_chk
        gp     = c_rev - c_cost
        margin = gp / c_rev * 100 if c_rev else 0
        lines.append(f"── {channel.upper()} ──")
        for sid, sn, _, rev, chk in chan:
            if rev == 0:
                continue
            sp = rev - _pc(sid)
            sm = sp / rev * 100 if rev else 0
            sa = calc.avg_check(rev, chk)
            lines.append(
                f"  📍 {sn}\n"
                f"     Выручка {_rub(rev)} ₽ · Прибыль {_rub(sp)} ₽ ({sm:.0f}%)\n"
                f"     Чеков {chk} · Ср.чек {_rub(sa)} ₽"
            )
            # J1.2: пояснение смешанной кассы — отдельными строками под цифрами,
            # понятными без контекста (не суффиксом в строке цифр).
            if sn in mixed:
                lines.append(
                    "     ℹ️ Через эту кассу продаётся и опт, и розница, поэтому процент\n"
                    "     прибыли здесь всегда ниже, чем у чисто розничных точек. Это норма."
                )
            if channel == "розница" and sn not in mixed:
                retail_margins.append((sn, sm, rev))
        lines.append(
            f"  Итого: {_rub(c_rev)} ₽ · {_rub(gp)} ₽ ({margin:.0f}%) · {c_chk} чек."
        )
        lines.append("")

    gp_t = grand_rev - grand_cost
    mg_t = gp_t / grand_rev * 100 if grand_rev else 0
    ac_t = calc.avg_check(grand_rev, grand_chk)
    lines.append("── ИТОГО ──")
    lines.append(
        f"  Выручка: {_rub(grand_rev)} ₽\n"
        f"  Прибыль: {_rub(gp_t)} ₽ ({mg_t:.0f}%)\n"
        f"  Чеков: {grand_chk} · Ср.чек: {_rub(ac_t)} ₽"
    )

    lines.append("")

    # G5 (переработан): «Требует внимания». Чистые розничные точки сравниваем с
    # медианой розницы; смешанные (опт+розница через одну кассу) — с их же историей.
    if not store_name:
        _attention_block(conn, lines, d_from, d_to, retail_margins, mixed, stores, _pc)

    # ── Топ товаров (прибыль по закупочным ценам, * = нет данных о приёмке) ──
    sf2 = "AND spd.store_id = %s" if store_id_f else ""
    p2  = [d_from, d_to] + ([store_id_f] if store_id_f else [])
    _asof = """
        LEFT JOIN LATERAL (
            SELECT price_kop FROM purchase_price_asof p
            WHERE p.product_id = spd.assortment_id AND p.priced_from <= spd.day
            ORDER BY p.priced_from DESC LIMIT 1
        ) pp ON true
    """
    _pcost = (
        "CASE "
        "WHEN pp.price_kop IS NOT NULL AND pp.price_kop > 0 "
        "THEN round(spd.sell_qty * pp.price_kop) "
        "ELSE round(spd.revenue_kop * 0.6) END"
    )
    _srez = ("spd.assortment_id IN (SELECT DISTINCT product_id FROM stock_snapshot "
             "WHERE folder_path LIKE %s)")
    any_star = False

    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT spd.product_name, SUM(spd.sell_qty) AS qty, SUM(spd.revenue_kop) AS rev,
                   SUM({_pcost}) AS pcost,
                   bool_or(pp.price_kop IS NULL OR pp.price_kop <= 0) AS uncov
            FROM sales_by_product_day spd {_asof}
            WHERE spd.day BETWEEN %s AND %s {sf2} AND {_srez}
            GROUP BY spd.product_name
            ORDER BY SUM(spd.revenue_kop) DESC LIMIT 20
        """, p2 + ["Ассортимент/%"])
        top_rev = cur.fetchall()

    if top_rev:
        lines.append(f"🏆 Топ-{len(top_rev)} по выручке:")
        for i, (name, qty, rev, pcost, uncov) in enumerate(top_rev, 1):
            rev = int(rev or 0); profit = rev - int(pcost or 0)
            mg = profit / rev * 100 if rev else 0
            star = " *" if uncov else ""
            any_star = any_star or bool(uncov)
            lines.append(
                f"  {i:2}. {name}{star}\n"
                f"      {_qty(float(qty))} ед. · {_rub(rev)} ₽ · прибыль {_rub(profit)} ₽ ({mg:.0f}%)"
            )
        lines.append("")

    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT spd.product_name, SUM(spd.revenue_kop) AS rev,
                   SUM(spd.revenue_kop) - SUM({_pcost}) AS profit,
                   SUM({_pcost}) AS pcost,
                   bool_or(pp.price_kop IS NULL OR pp.price_kop <= 0) AS uncov
            FROM sales_by_product_day spd {_asof}
            WHERE spd.day BETWEEN %s AND %s {sf2} AND {_srez}
            GROUP BY spd.product_name
            ORDER BY SUM(spd.revenue_kop) - SUM({_pcost}) DESC LIMIT 25
        """, p2 + ["Ассортимент/%"])
        rows = cur.fetchall()

    # J1.3: «Топ-10 по прибыли» удалён (прибыль и так печатается рядом с каждой
    # позицией в топ-20 по выручке). Запрос сохранён ради дефекта 3 ниже:
    # позиции без себестоимости (pcost=0 → фальшивая маржа 100%) — отдельным
    # списком «ошибка данных», а не в топе.
    nocost = [r for r in rows if int(r[3] or 0) == 0 and int(r[1] or 0) > 0]

    if nocost:
        lines.append("⚠️ Нет закупочной цены в карточке — себестоимость оценена (цена × 0.6):")
        for name, rev, _profit, _pc, _uncov in nocost[:10]:
            lines.append(f"  • {name}: выручка {_rub(int(rev or 0))} ₽")
        lines.append("")

    if any_star:
        lines.append("* нет закупочной цены в карточке — использована оценка (цена продажи × 0.6)")

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
