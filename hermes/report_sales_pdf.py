"""PDF-отчёт «Продажи» в брендовом стиле ЦБД.

Использует pdf_kit для единого визуального языка.
Прибыль: единая формула через costs.unit_cost() — фолбэк = цена продажи − 40%.
Три уровня прибыли: Валовая / Чистая / После потерь.
Разделение дохода: асимметричный дележ — шары Ленина/Воровского Коле 100%,
остальное 50/50. Справочно — выкуп своих как контрагентов (sales_doc.agent_name).
"""
from __future__ import annotations

from datetime import date

from . import calc, config
from . import pdf_kit as pk
from .costs import unit_cost  # noqa: F401
from .report_cashflow import get_operational_expenses, get_owner_withdrawals
from .report_sales import _sales_purchase_data, _store_id_for

# Корневая группа шаров (НЕ внутри Ассортимента — отдельный корень ШАРЫ).
_BALLS_ROOT = "ШАРЫ"
# Склады, где выручка шаров идёт Коле 100% (его отдельный поток).
_KOLA_BALL_STORES = frozenset({"Киров, Ленина 102А", "Розница Воровского 107/1"})

# Маппинг «своих» контрагентов: короткое имя → SQL LIKE паттерны.
# ВАЖНО: не путать Николая / Дмитрия / Ольгу — все Дубравины.
# «О 313 Юрья Николай» — оптовик, НЕ ловится (нет «дубравин»).
_SVOI: dict[str, list[str]] = {
    "Коля":  ["%николай дуб%", "%дубравин николай%"],
    "Дима":  ["%дубравин дмитрий%"],
    "Мама":  ["%мама%", "%дубравина ольга%"],
}


def _rub(kop: int | float) -> str:
    """Копейки -> '1 234 567' (без знака валюты)."""
    rub = abs(int(kop)) // 100
    sign = "-" if kop < 0 else ""
    return sign + f"{rub:,}".replace(",", " ")


def _pct(a, b) -> str:
    return f"{a / b * 100:.0f}" if b else "0"


def _trunc(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n - 1] + "…"


def _rub_pct(kop: int | float, base: int | float) -> str:
    """'116 604 ₽  · 47%' — сумма и мелкий процент в одной строке."""
    pct = f"{abs(kop) / base * 100:.0f}" if base else "0"
    return f"{_rub(kop)} ₽  · {pct}%"


# -- SQL-фрагменты для топ-товаров (единая формула) ---------------------------
_ASOF = """
    LEFT JOIN LATERAL (
        SELECT price_kop FROM purchase_price_asof p
        WHERE p.product_id = spd.assortment_id AND p.priced_from <= spd.day
        ORDER BY p.priced_from DESC LIMIT 1
    ) pp ON true
"""
_PCOST = (
    "CASE "
    "WHEN pp.price_kop IS NOT NULL AND pp.price_kop > 0 "
    "THEN round(spd.sell_qty * pp.price_kop) "
    "ELSE round(spd.revenue_kop * 0.6) END"
)
_UNCOV = "bool_or(pp.price_kop IS NULL OR pp.price_kop <= 0)"


def _get_balls_by_store(conn, d_from: date, d_to: date) -> list[tuple]:
    """Продажи корневой группы ШАРЫ по складам.

    Возвращает [(store_name, rev_kop, qty), ...] sorted DESC by rev.
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT ssd.store_name,
                   COALESCE(SUM(spd.revenue_kop), 0) AS rev_kop,
                   COALESCE(SUM(spd.sell_qty), 0)    AS qty
            FROM sales_by_product_day spd
            JOIN sales_by_store_day ssd
                 ON ssd.store_id = spd.store_id AND ssd.day = spd.day
            WHERE spd.day BETWEEN %s AND %s
              AND spd.assortment_id IN (
                  SELECT DISTINCT product_id FROM stock_snapshot
                  WHERE folder_path = %s OR folder_path LIKE %s
              )
            GROUP BY ssd.store_name
            ORDER BY rev_kop DESC
        """, (d_from, d_to, _BALLS_ROOT, _BALLS_ROOT + "/%"))
        return [(sn, int(rev or 0), float(qty or 0)) for sn, rev, qty in cur.fetchall()]


def _get_svoi_purchases(conn, d_from: date, d_to: date) -> dict:
    """Покупки «своих» контрагентов (Коля / Дима / Мама) за период, все склады.

    Возвращает {имя: {"total": kop, "docs": int,
                       "rows": [(day_str, store_name, kop), ...]}}.
    """
    result: dict[str, dict] = {}
    for name, pats in _SVOI.items():
        cond = " OR ".join(["lower(agent_name) LIKE %s"] * len(pats))
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT day::text, store_name, sum_kop
                FROM sales_doc
                WHERE day BETWEEN %s AND %s AND ({cond})
                ORDER BY day, sum_kop DESC
            """, (d_from, d_to, *pats))
            rows = cur.fetchall()
        total = sum(int(r[2] or 0) for r in rows)
        result[name] = {
            "total": total,
            "docs":  len(rows),
            "rows":  [(r[0], r[1], int(r[2] or 0)) for r in rows],
        }
    return result


def _get_losses_by_store(conn, d_from: date, d_to: date) -> dict[str, int]:
    """Списания по закупочной стоимости (исключая ADJUSTMENT_STORES)."""
    excl = list(config.ADJUSTMENT_STORES or [])
    with conn.cursor() as cur:
        cur.execute("""
            SELECT d.store_name,
                   SUM(CASE
                       WHEN i.product_id IS NOT NULL
                            AND pp.price_kop IS NOT NULL AND pp.price_kop > 0
                           THEN round(i.qty * pp.price_kop)
                       ELSE i.total_kop END) AS loss_kop
            FROM loss_doc d
            JOIN loss_item i ON i.doc_id = d.doc_id
            LEFT JOIN LATERAL (
                SELECT price_kop FROM purchase_price_asof p
                WHERE i.product_id IS NOT NULL
                  AND p.product_id = i.product_id AND p.priced_from <= d.day
                ORDER BY p.priced_from DESC LIMIT 1
            ) pp ON true
            WHERE d.day BETWEEN %s AND %s
              AND NOT (d.store_name = ANY(%s))
            GROUP BY d.store_name
        """, (d_from, d_to, excl))
        rows = cur.fetchall()

    result: dict[str, int] = {}
    total = 0
    for sn, kop in rows:
        v = int(kop or 0)
        result[sn] = v
        total += v
    result["__total__"] = total
    return result


def _render_income_split(
    pdf: pk.HermesPDF,
    grand_net: int,
    kola_balls_kop: int,
    grand_prof: int = 0,
    exp_total: int = 0,
) -> None:
    """Блок «Разделение дохода» — визуальный каскад по макету ЦБД.

    Шары Ленина + Воровского → Коля 100% (прибыль = выручка, учёт не ведётся).
    Остаток → 50/50. Инвариант: ИТОГО Диме + ИТОГО Коле = grand_net (чистая).
    """
    pk.section_header(pdf, "Разделение дохода  ·  Дима и Коля")

    joint      = grand_net - kola_balls_kop
    dima_share = joint // 2
    kola_joint = joint - dima_share
    kola_total = kola_joint + kola_balls_kop

    LW = 115.0
    VW = float(pk._INNER_W) - LW

    def _v(kop: int) -> str:
        return f"{abs(kop) // 100:,}".replace(",", " ") + " ₽"

    def _sep(color: tuple = pk.GRID, w: float = 0.35) -> None:
        pdf.set_draw_color(*color)
        pdf.set_line_width(w)
        pdf.line(float(pk._MARGIN), pdf.get_y(),
                 float(pk._MARGIN) + float(pk._INNER_W), pdf.get_y())

    def _row(label: str, val: str, bold: bool = False,
             color: tuple = pk.INK, bg: tuple | None = None,
             h: float = 6.0) -> None:
        if bg:
            pdf.set_fill_color(*bg)
            pdf.rect(float(pk._MARGIN), pdf.get_y(),
                     float(pk._INNER_W), h, style="F")
        pdf.set_font("DejaVu_B" if bold else "DejaVu",
                     size=10.0 if bold else 9.0)
        pdf.set_text_color(*color)
        pdf.set_x(pk._MARGIN)
        pdf.cell(LW, h, label)
        pdf.cell(VW, h, val, align="R", new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(*pk.INK)

    # ── каскад ────────────────────────────────────────────────────────────────
    if grand_prof:
        _row("Вал. прибыль (выручка − закупка)", _v(grand_prof))
        _sep(pk.GRID, 0.2)
        _row("  − Операционные расходы", "−" + _v(exp_total), color=pk.TERRA)
        _sep()
        pdf.ln(1)
    _row("= Чистая прибыль", _v(grand_net), bold=True, bg=pk.SAGE_L, h=7.5)
    pdf.ln(1)
    _sep()
    _row("  − Шары Коли (Ленина + Воровского)",
         "−" + _v(kola_balls_kop), color=pk.TERRA)
    _sep()
    pdf.ln(1.5)
    _row("= Совместное", _v(joint), bold=True, bg=pk.SAGE_L, h=7.5)
    pdf.ln(0.5)
    _row("     → Дима (50%)",   _v(dima_share), color=pk.SAGE)
    _row("     → Коля (50%)",   _v(kola_joint), color=pk.SAGE)
    _row("  + Шары Коли (100%, по выручке)", "+" + _v(kola_balls_kop),
         color=pk.SAGE)
    pdf.ln(3)

    # ── ИТОГО плашки ──────────────────────────────────────────────────────────
    _row("  ИТОГО Диме", _v(dima_share), bold=True, bg=pk.SAGE_L, h=8.5)
    pdf.ln(1.5)
    _row("  ИТОГО Коле", _v(kola_total), bold=True, bg=pk.SAGE_L, h=8.5)
    pdf.ln(3)

    # ── проверка и сноска ─────────────────────────────────────────────────────
    ok = (dima_share + kola_total == grand_net)
    pdf.set_font("DejaVu", size=7.5)
    pdf.set_text_color(*(pk.SAGE if ok else pk.TERRA))
    pdf.set_x(pk._MARGIN)
    if ok:
        chk = (f"Проверка: {_rub(dima_share)} + {_rub(kola_total)}"
               f" = {_rub(grand_net)} ₽ ✓")
    else:
        chk = (f"⚠ РАСХОЖДЕНИЕ: {_rub(dima_share + kola_total)}"
               f" ≠ {_rub(grand_net)} ₽")
    pdf.cell(pk._INNER_W, 4.5, chk, align="L", new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(*pk.SAGE)
    pdf.set_x(pk._MARGIN)
    pdf.cell(pk._INNER_W, 4,
             "Шары Коли — по выручке (учёт не ведётся). Совместное делится 50/50.",
             align="L", new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(*pk.INK)
    pdf.ln(4)


def build_sales_pdf(
    conn, d_from: date, d_to: date, store_name: str | None = None
) -> bytes:
    """PDF-отчёт по продажам с тремя уровнями прибыли. Возвращает bytes."""
    days = (d_to - d_from).days + 1
    period_str = (
        d_from.strftime("%d.%m.%Y") if d_from == d_to
        else f"{d_from.strftime('%d.%m.%Y')} – {d_to.strftime('%d.%m.%Y')}"
    )
    store_label = store_name or "Все склады"

    # -- Данные ----------------------------------------------------------------
    store_id_f = _store_id_for(conn, store_name)
    pdata = _sales_purchase_data(conn, d_from, d_to, store_id_f)
    by_store, tot = pdata["by_store"], pdata["tot"]

    sf = "AND store_name = %s" if store_name else ""
    p  = [d_from, d_to] + ([store_name] if store_name else [])
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT store_id, store_name, channel,
                   COALESCE(SUM(revenue_kop), 0),
                   COALESCE(SUM(checks), 0)
            FROM sales_by_store_day
            WHERE day BETWEEN %s AND %s {sf}
            GROUP BY store_id, store_name, channel
            ORDER BY channel, SUM(revenue_kop) DESC
        """, p)
        stores = cur.fetchall()

    # -- Расходы, изъятия, потери, шары, свои ----------------------------------
    exp = get_operational_expenses(conn, d_from, d_to)
    owner_kop = get_owner_withdrawals(conn, d_from, d_to)
    losses = _get_losses_by_store(conn, d_from, d_to)
    losses_total = losses.get("__total__", 0)
    balls_data = _get_balls_by_store(conn, d_from, d_to)
    svoi = _get_svoi_purchases(conn, d_from, d_to)

    # Шары Коли: только Ленина + Воровского (100% его доход).
    kola_balls_kop = sum(rev for sn, rev, _ in balls_data if sn in _KOLA_BALL_STORES)
    # Для подстрок в таблице складов.
    balls_by_store = {sn: rev for sn, rev, _ in balls_data}

    general_exp = exp["by_project"].get("__general__", 0)
    total_rev_for_alloc = sum(r[3] for r in stores if r[3] > 0) or 1

    def _gross(sid, rev_s):
        cost = by_store.get(sid, {}).get("pc", 0)
        return rev_s - cost

    def _net(sid, sn, rev_s):
        if rev_s == 0:
            return 0
        direct = exp["by_project"].get(sn, 0)
        share  = round(general_exp * rev_s / total_rev_for_alloc)
        return _gross(sid, rev_s) - direct - share

    _biz = [r for r in stores if r[2] in ("розница", "опт", "ресторан")]
    grand_rev   = sum(r[3] for r in _biz)
    grand_cost  = sum(by_store.get(r[0], {}).get("pc", 0) for r in _biz)
    grand_prof  = grand_rev - grand_cost
    grand_net   = grand_prof - exp["total"]
    grand_after = grand_net - losses_total
    grand_chk   = sum(r[4] for r in _biz)
    grand_ac    = calc.avg_check(grand_rev, grand_chk)

    # -- Топ-20 по выручке -----------------------------------------------------
    sf2 = "AND spd.store_id = %s" if store_id_f else ""
    p2  = [d_from, d_to] + ([store_id_f] if store_id_f else [])
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT spd.product_name, SUM(spd.sell_qty), SUM(spd.revenue_kop),
                   SUM({_PCOST}),
                   {_UNCOV}
            FROM sales_by_product_day spd {_ASOF}
            WHERE spd.day BETWEEN %s AND %s {sf2}
              AND spd.assortment_id IN (
                  SELECT DISTINCT product_id FROM stock_snapshot
                  WHERE folder_path LIKE %s
              )
            GROUP BY spd.product_name
            ORDER BY SUM(spd.revenue_kop) DESC LIMIT 20
        """, p2 + ["Ассортимент/%"])
        top_rev = cur.fetchall()

    # -- Строим PDF ------------------------------------------------------------
    pdf = pk.HermesPDF(
        section_title="Продажи",
        period=period_str,
        store=store_label,
    )
    pdf.add_page()

    pk.cover(pdf, f"Продажи ({days} дн.)")

    # KPI: 5 плашек — выручка / вал.прибыль(%) / чистая(%) / после потерь(%) / ср.чек
    pk.kpi_row(pdf, [
        ("Выручка",      _rub(grand_rev),   "₽"),
        ("Вал.прибыль",  _rub(grand_prof),  "₽", _pct(grand_prof, grand_rev) + "%"),
        ("Чист.прибыль", _rub(grand_net),   "₽", _pct(grand_net, grand_rev)  + "%"),
        ("После потерь", _rub(grand_after), "₽", _pct(grand_after, grand_rev) + "%"),
        ("Ср. чек",      _rub(grand_ac),    "₽"),
    ])

    # -- Каскад P&L ------------------------------------------------------------
    pk.section_header(pdf, "Отчёт о прибылях и убытках")
    cascade_rows = [
        ("Выручка", grand_rev, "income"),
        ("Закупочная стоимость", grand_cost, "deduct"),
        (None, None, None),
        ("= ВАЛОВАЯ ПРИБЫЛЬ", grand_prof, "subtotal", _pct(grand_prof, grand_rev) + "%"),
        ("Операционные расходы", exp["total"], "deduct"),
        (None, None, None),
        ("= ЧИСТАЯ ПРИБЫЛЬ", grand_net, "subtotal", _pct(grand_net, grand_rev) + "%"),
        ("Списания (порча)", losses_total, "deduct"),
        (None, None, None),
        ("= ПРИБЫЛЬ ПОСЛЕ ПОТЕРЬ", grand_after, "subtotal",
         _pct(grand_after, grand_rev) + "%"),
    ]
    if owner_kop > 0:
        cascade_rows.append((None, None, None))
        cascade_rows.append((
            "Изъятия собственника (справочно — не в прибыли)",
            owner_kop, "note",
        ))
    pk.pl_cascade(pdf, cascade_rows)

    # Методика (одна строка)
    pdf.set_x(pk._MARGIN)
    pdf.set_font("DejaVu", size=8)
    pdf.set_text_color(*pk.SAGE)
    card_pct = round(tot["cov_card"])
    est_pct  = 100 - card_pct
    cov_note = (
        f"Прибыль посчитана точно у {card_pct}% продаж (закупка из карточки). "
        f"У остальных {est_pct}% закупка оценена как цена продажи − 40%. "
        "Чистая = валовая − операционные расходы (без изъятий собственника)."
    )
    pdf.multi_cell(pk._INNER_W, 4, cov_note, align="L")
    pdf.set_text_color(*pk.INK)
    pdf.ln(3)

    # -- Графики ---------------------------------------------------------------
    try:
        from . import charts as _ch
        if _ch._MPL_OK:
            png1 = _ch.chart_revenue_by_day(conn, d_from, d_to, store_name)
            if png1:
                pk.embed_chart(pdf, png1, "Выручка и маржа по дням")
            if not store_name:
                png2 = _ch.chart_stores_compare(conn, d_from, d_to)
                if png2:
                    pk.embed_chart(pdf, png2, "Выручка, прибыль и маржа по складам")
    except Exception:
        pass

    # -- Таблица: каналы и склады (6 колонок, % inline) ------------------------
    pk.section_header(pdf, "Итоги по каналам и складам")

    # [Склад, Выручка, Вал.приб.·%, Чист.приб.·%, Чек] = 174мм
    hdrs = ["Склад / Канал", "Выручка", "Вал.приб. · %", "Чист.приб. · %", "Чек"]
    cws  = [56, 27, 42, 33, 16]
    alns = ["L", "R", "R", "R", "R"]

    mixed = set(config.MIXED_CHANNEL_STORES or [])
    chan_rows: list[list[str]] = []
    chan_styles: list[str | None] = []
    mixed_in_table: list[str] = []

    for channel in ("розница", "опт", "ресторан"):
        chan = [r for r in stores if r[2] == channel]
        c_rev = sum(r[3] for r in chan)
        if c_rev == 0:
            continue
        c_gross = sum(_gross(r[0], r[3]) for r in chan)
        c_net   = sum(_net(r[0], r[1], r[3]) for r in chan)
        c_chk   = sum(r[4] for r in chan)

        for sid, sn, _, rev_s, chk_s in chan:
            if rev_s == 0:
                continue
            gp  = _gross(sid, rev_s)
            np_ = _net(sid, sn, rev_s)
            marker = " †" if sn in mixed else ""
            if sn in mixed:
                mixed_in_table.append(sn)
            sn_short = _trunc(sn, 22)
            chan_rows.append([
                f"{channel.upper()}  {sn_short}{marker}",
                _rub(rev_s) + " ₽",
                _rub_pct(gp, rev_s),
                _rub_pct(np_, rev_s),
                str(chk_s),
            ])
            chan_styles.append(None)
            ball_rev = balls_by_store.get(sn, 0)
            if ball_rev > 0 and not store_name:
                ball_lbl = ("в т.ч. шары (Коля)"
                            if sn in _KOLA_BALL_STORES
                            else "в т.ч. шары (совместное)")
                chan_rows.append([f"  {ball_lbl}", _rub(ball_rev) + " ₽",
                                  "", "", ""])
                chan_styles.append("detail")

        chan_rows.append([
            f"  Итого {channel}",
            _rub(c_rev)   + " ₽",
            _rub_pct(c_gross, c_rev),
            _rub_pct(c_net, c_rev),
            str(c_chk),
        ])
        chan_styles.append(None)

    chan_rows.append([
        "ИТОГО",
        _rub(grand_rev)  + " ₽",
        _rub_pct(grand_prof, grand_rev),
        _rub_pct(grand_net, grand_rev),
        str(grand_chk),
    ])
    chan_styles.append(None)

    pk.table(pdf, headers=hdrs, rows=chan_rows, col_widths=cws, aligns=alns,
             font_size=8.5, row_styles=chan_styles)

    # Сноски под таблицей
    pdf.set_x(pk._MARGIN)
    pdf.set_font("DejaVu", size=7.5)
    pdf.set_text_color(*pk.SAGE)
    footnotes = [
        "Чистая прибыль: прямые расходы склада + доля общих расходов пропорционально выручке — оценка.",
    ]
    if mixed_in_table:
        footnotes.append("† Смешанная касса — точка обслуживает несколько каналов.")
    for fn in footnotes:
        pdf.set_x(pk._MARGIN)
        pdf.cell(pk._INNER_W, 4, fn, align="L", new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(*pk.INK)
    pdf.ln(2)

    # -- Блок «Разделение дохода» (асимметричный дележ) ------------------------
    if not store_name:
        _render_income_split(
            pdf,
            grand_net=grand_net,
            kola_balls_kop=kola_balls_kop,
            grand_prof=grand_prof,
            exp_total=exp["total"],
        )

    # -- Блок «ШАРЫ» (корневая группа, аналитический срез) --------------------
    if not store_name and balls_data:
        pk.section_header(pdf, "Шары (корневая группа ШАРЫ)")
        balls_total_rev = sum(rev for _, rev, _ in balls_data)
        balls_tbl = []
        for sn, rev, _ in balls_data:
            balls_tbl.append([_trunc(sn, 50), _rub(rev) + " ₽"])
        balls_tbl.append(["ИТОГО", _rub(balls_total_rev) + " ₽"])
        pk.table(
            pdf,
            headers=["Склад", "Выручка"],
            rows=balls_tbl,
            col_widths=[134, 40],
            aligns=["L", "R"],
            font_size=8.5,
        )
        pdf.set_x(pk._MARGIN)
        pdf.set_font("DejaVu", size=7.5)
        pdf.set_text_color(*pk.SAGE)
        kola_b_note = _rub(sum(rev for sn, rev, _ in balls_data
                               if sn in _KOLA_BALL_STORES))
        joint_b_note = _rub(sum(rev for sn, rev, _ in balls_data
                                if sn not in _KOLA_BALL_STORES))
        pdf.cell(
            pk._INNER_W, 4,
            (f"Шары Ленина/Воровского ({kola_b_note} ₽) — доход Коли 100%. "
             f"Шары прочих складов ({joint_b_note} ₽) — в совместном."),
            align="L", new_x="LMARGIN", new_y="NEXT",
        )
        pdf.set_text_color(*pk.INK)
        pdf.ln(3)

    # -- Блок «Продажи своим» (справочно) -------------------------------------
    if not store_name:
        pk.section_header(pdf, "Продажи своим (справочно)")

        def _svoi_row(label: str, val_str: str, bold: bool = False,
                      indent: int = 0, color: tuple = pk.INK) -> None:
            h = 6.0 if bold else 5.0
            lw = 120.0
            vw = float(pk._INNER_W) - lw
            pdf.set_font("DejaVu_B" if bold else "DejaVu", size=8.5 if bold else 7.5)
            pdf.set_text_color(*color)
            pdf.set_x(pk._MARGIN)
            pdf.cell(lw, h, "  " * indent + label)
            pdf.cell(vw, h, val_str, align="R", new_x="LMARGIN", new_y="NEXT")
            pdf.set_text_color(*pk.INK)

        any_svoi = False
        for person, data in svoi.items():
            if data["total"] == 0 and data["docs"] == 0:
                _svoi_row(person, "0 ₽ за период")
                continue
            any_svoi = True
            _svoi_row(
                person,
                f"{_rub(data['total'])} ₽  ·  {data['docs']} "
                + ("документ" if data["docs"] == 1 else "документов"),
                bold=True,
            )
            for day_s, sn, kop in data["rows"]:
                _svoi_row(
                    f"  {day_s}  ·  {_trunc(sn, 30)}",
                    _rub(kop) + " ₽",
                    indent=0,
                    color=pk.SAGE,
                )

        pdf.set_x(pk._MARGIN)
        pdf.set_font("DejaVu", size=7.5)
        pdf.set_text_color(*pk.SAGE)
        pdf.multi_cell(
            pk._INNER_W, 4,
            "Справочно — что выкупили связанные лица; "
            "входит в общую прибыль, отдельно не вычитается.",
            align="L",
        )
        pdf.set_text_color(*pk.INK)
        pdf.ln(3)

    # -- Списания по складам ---------------------------------------------------
    store_losses = {k: v for k, v in losses.items() if k != "__total__"}
    if store_losses:
        pk.section_header(pdf, "Списания и прибыль после потерь по складам")
        loss_tbl_rows = []
        sum_net = 0; sum_after = 0
        for sid, sn, ch, rev_s, _ in stores:
            if rev_s == 0:
                continue
            s_loss  = store_losses.get(sn, 0)
            s_net   = _net(sid, sn, rev_s)
            s_after = s_net - s_loss
            sum_net += s_net; sum_after += s_after
            loss_tbl_rows.append([
                _trunc(sn, 38),
                _rub(s_loss)  + " ₽",
                _rub(s_net)   + " ₽",
                _rub(s_after) + " ₽",
            ])
        loss_tbl_rows.append([
            "ИТОГО",
            _rub(losses_total) + " ₽",
            _rub(sum_net)      + " ₽",
            _rub(sum_after)    + " ₽",
        ])
        pk.table(
            pdf,
            headers=["Склад", "Списания", "Чист.прибыль", "После потерь"],
            rows=loss_tbl_rows,
            col_widths=[86, 28, 30, 30],
            aligns=["L", "R", "R", "R"],
            font_size=8.5,
        )
        convergence_ok = abs(sum_after - grand_after) < 500
        if not convergence_ok:
            pk.callout(
                pdf,
                f"Расхождение сходимости: сумма по складам {_rub(sum_after)} ₽ "
                f"≠ итого {_rub(grand_after)} ₽ (Δ{_rub(abs(sum_after - grand_after))} ₽).",
                kind="warn",
            )
        pdf.ln(1)

    # -- Топ-20 по выручке -----------------------------------------------------
    if top_rev:
        pk.section_header(pdf, f"Топ-{len(top_rev)} товаров по выручке")
        top_rows = []
        any_miss = False
        for i, (name, qty, rev_p, pcost, uncov) in enumerate(top_rev, 1):
            rev_p  = int(rev_p  or 0)
            profit = rev_p - int(pcost or 0)
            star   = " *" if uncov else ""
            if uncov:
                any_miss = True
            nm = _trunc(name + star, 50)
            top_rows.append([
                str(i),
                nm,
                f"{float(qty):.1f}" if float(qty) != int(float(qty)) else str(int(float(qty))),
                _rub(rev_p)  + " ₽",
                _rub_pct(profit, rev_p),
            ])
        pk.table(
            pdf,
            headers=["#", "Товар", "Ед.", "Выручка", "Вал.приб. · %"],
            rows=top_rows,
            col_widths=[8, 82, 14, 28, 42],
            aligns=["R", "L", "R", "R", "R"],
            font_size=8,
            max_rows=20,
        )
        if any_miss:
            pdf.set_x(pk._MARGIN)
            pdf.set_font("DejaVu", size=7.5)
            pdf.set_text_color(*pk.SAGE)
            pdf.cell(pk._INNER_W, 4,
                     "* нет закупочной цены в карточке — закуп оценён как цена продажи − 40%",
                     align="L", new_x="LMARGIN", new_y="NEXT")
            pdf.set_text_color(*pk.INK)

    # -- Callout: аномально низкая валовая маржа --------------------------------
    import statistics
    retail_margins = [
        (sn, _gross(sid, rev_s) / rev_s * 100, rev_s)
        for sid, sn, ch, rev_s, _ in stores
        if ch == "розница" and sn not in mixed and rev_s > 0
    ]
    flags = []
    if len(retail_margins) >= 2:
        med = statistics.median(m for _, m, _ in retail_margins)
        for sn, m, rev_s in retail_margins:
            if med > 0 and m < med / 2:
                gap = round(rev_s * (med - m) / 100)
                flags.append(
                    f"• {sn}: маржа {m:.0f}% против {med:.0f}% медианы розницы "
                    f"(разрыв ~{_rub(gap)} ₽)"
                )
    if flags:
        pk.callout(pdf, "Требует внимания:\n" + "\n".join(flags), kind="warn")

    return bytes(pdf.output())
