"""Генерация PDF-отчёта по всем секциям за выбранный период и склад."""
from __future__ import annotations

import io
import re
from datetime import date

_FONT      = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
_FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
_MARGIN  = 12   # мм
_LINE_H  = 4.8  # мм высота строки

_RE_STRIP = re.compile(
    "[\U0001F000-\U0001FFFF"
    "\U00002600-\U000026FF"
    "\U00002700-\U000027BF"
    "\U0000FE00-\U0000FEFF"
    "]",
    flags=re.UNICODE,
)

_TOC_SECTIONS = [
    ("1", "ПРОДАЖИ",      "выручка, прибыль от продаж, топ позиций"),
    ("2", "ОСТАТКИ",      "СРЕЗКА без резерва на дату"),
    ("3", "ЗАЛЕЖАЛЫЕ",    "СРЕЗКА без движения"),
    ("4", "СПИСАНИЯ",     "документы с позициями"),
    ("5", "РАСХОДЫ",      "движение денег по складам"),
    ("6", "КЛИЕНТЫ",      "топ и возможный отток"),
    ("7", "ПРОГНОЗ",      "заказы, спрос и что брать на фургон"),
    ("8", "ПЕРЕМЕЩЕНИЯ",  "движение товара между складами"),
    ("9", "ИЗМЕНЕНИЯ",    "удалённые и изменённые документы"),
]


def _clean(text: str) -> str:
    return _RE_STRIP.sub("", text)


def _fmt_rub(kop: int) -> str:
    """Форматирует копейки как '1 500 131 ₽' (пробел как разделитель тысяч)."""
    rub = abs(kop) // 100
    sign = "-" if kop < 0 else ""
    return sign + f"{rub:,}".replace(",", " ") + " ₽"


def _pdf_summary(conn, d_from: date, d_to: date, store_name: str | None) -> dict:
    """Сводка периода для титульной страницы PDF. Все суммы в копейках."""
    from .report_stock import STALE_SREZKA_MIN_DAYS
    from . import config as _cfg, calc

    adj = _cfg.ADJUSTMENT_STORES or ["__none__"]
    own = getattr(_cfg, "OWNER_EXPENSE_ITEMS", []) or []

    sf_sd = "AND store_name = %s" if store_name else ""
    p_sd  = [d_from, d_to] + ([store_name] if store_name else [])

    # 1. Выручка, себестоимость МойСклад (N1: методика зафиксирована явно), чеки
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT COALESCE(SUM(revenue_kop), 0),
                   COALESCE(SUM(cost_kop), 0),
                   COALESCE(SUM(checks), 0)
            FROM sales_by_store_day
            WHERE day BETWEEN %s AND %s {sf_sd}
        """, p_sd)
        r = cur.fetchone() or (0, 0, 0)
    rev    = int(r[0])
    cost   = int(r[1])
    checks = int(r[2])
    profit = rev - cost   # прибыль по себестоимости МойСклад

    # 2. Расходы — N2: операционные (без изъятий) и изъятия собственника раздельно
    sf_cf = "AND project_name = %s" if store_name else ""
    p_cf  = [d_from, d_to] + ([store_name] if store_name else [])

    if own:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT COALESCE(SUM(amount_kop), 0)
                FROM cashflow_event
                WHERE day BETWEEN %s AND %s AND direction = 'out' {sf_cf}
                  AND (expense_item_name IS NULL OR expense_item_name != ALL(%s))
            """, p_cf + [own])
            op_expenses = int((cur.fetchone() or (0,))[0])
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT COALESCE(SUM(amount_kop), 0)
                FROM cashflow_event
                WHERE day BETWEEN %s AND %s AND direction = 'out' {sf_cf}
                  AND expense_item_name = ANY(%s)
            """, p_cf + [own])
            owner_kop = int((cur.fetchone() or (0,))[0])
    else:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT COALESCE(SUM(amount_kop), 0)
                FROM cashflow_event
                WHERE day BETWEEN %s AND %s AND direction = 'out' {sf_cf}
            """, p_cf)
            op_expenses = int((cur.fetchone() or (0,))[0])
        owner_kop = 0

    # 3. Списания — N3: порча розницы vs корректировки учёта (ADJUSTMENT_STORES)
    sf_ls = "AND d.store_name = %s" if store_name else ""
    p_ls  = [d_from, d_to] + ([store_name] if store_name else [])

    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT COALESCE(SUM(i.total_kop), 0)
            FROM loss_doc d JOIN loss_item i ON i.doc_id = d.doc_id
            WHERE d.day BETWEEN %s AND %s {sf_ls}
              AND NOT (d.store_name = ANY(%s))
        """, p_ls + [adj])
        loss_spoil = int((cur.fetchone() or (0,))[0])

    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT COALESCE(SUM(i.total_kop), 0)
            FROM loss_doc d JOIN loss_item i ON i.doc_id = d.doc_id
            WHERE d.day BETWEEN %s AND %s {sf_ls}
              AND (d.store_name = ANY(%s))
        """, p_ls + [adj])
        loss_adj = int((cur.fetchone() or (0,))[0])

    loss      = loss_spoil + loss_adj
    result    = profit - op_expenses - loss
    avg_check = calc.avg_check(rev, checks)  # M1: единая формула round(), как в report_sales

    # 4. «Требует внимания» ────────────────────────────────────────────────────
    attention: list[str] = []
    stale_cnt, stale_kop = 0, 0

    # N4: залежалая СРЕЗКА — тот же фильтр, что раздел 3 (days_idle > threshold)
    sf_ss = "AND ss.store_name = %s" if store_name else ""
    p_ss  = [d_to] + ([store_name] if store_name else []) + [d_to, STALE_SREZKA_MIN_DAYS]
    try:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT COUNT(*), COALESCE(SUM(
                    ss.stock_qty * COALESCE(NULLIF(pp.price_kop, 0), ss.cost_price_kop)
                ), 0)
                FROM stock_snapshot ss
                LEFT JOIN LATERAL (
                    SELECT price_kop FROM purchase_price_asof p
                    WHERE p.product_id = ss.product_id AND p.priced_from <= ss.day
                    ORDER BY p.priced_from DESC LIMIT 1
                ) pp ON true
                WHERE ss.day = %s
                  AND ss.is_srezka
                  AND ss.stock_qty > 0
                  AND ss.reserve_qty = 0
                  {sf_ss}
                  AND (%s - COALESCE(
                    (SELECT MAX(spd.day) FROM sales_by_product_day spd
                     WHERE spd.assortment_id = ss.product_id AND spd.sell_qty > 0),
                    '2000-01-01'::date
                  )) > %s
            """, p_ss)
            sr = cur.fetchone() or (0, 0)
        stale_cnt = int(sr[0] or 0)
        stale_kop = round(sr[1] or 0)  # M2: round() как в _rub() report_stock
        if stale_cnt > 0:
            attention.append(
                f"Залежалая СРЕЗКА: {_fmt_rub(stale_kop)} ({stale_cnt} поз.)"
            )
    except Exception:
        conn.rollback()

    # Порча > 10% выручки
    if rev > 0 and loss_spoil > 0:
        pct = loss_spoil / rev * 100
        if pct > 10:
            attention.append(f"Порча: {pct:.0f}% от выручки (порог 10%)")

    # СРЕЗКА без закупочной цены
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*)
                FROM product_dim pd
                WHERE pd.is_srezka
                  AND NOT EXISTS (
                    SELECT 1 FROM product_price pp
                    WHERE pp.product_id = pd.product_id AND pp.buy_price_kop > 0
                  )
            """)
            no_price_cnt = int((cur.fetchone() or (0,))[0])
        if no_price_cnt > 0:
            attention.append(f"Без закупочной цены: {no_price_cnt} поз.")
    except Exception:
        conn.rollback()

    return {
        "rev": rev, "profit": profit,
        "op_expenses": op_expenses, "owner_kop": owner_kop,
        "loss_spoil": loss_spoil, "loss_adj": loss_adj, "loss": loss,
        "result": result, "checks": checks, "avg_check": avg_check,
        "stale_cnt": stale_cnt, "stale_kop": stale_kop,
        "attention": attention,
    }


def build_pdf(
    conn, client, d_from: date, d_to: date, store_name: str | None = None
) -> bytes:
    """Сформировать PDF-отчёт. Возвращает bytes."""
    try:
        from fpdf import FPDF
    except ImportError:
        raise RuntimeError("Установите fpdf2: pip install fpdf2")

    period_str = (
        d_from.strftime("%d.%m.%Y") if d_from == d_to
        else f"{d_from.strftime('%d.%m.%Y')} - {d_to.strftime('%d.%m.%Y')}"
    )
    store_label = store_name or "Все склады"

    try:
        summary = _pdf_summary(conn, d_from, d_to, store_name)
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        summary = None

    try:
        from . import charts as _charts
        _charts_ok = True
    except Exception:
        _charts_ok = False

    class _PDF(FPDF):
        def header(self):
            self.set_x(self.l_margin)
            self.set_font("DejaVu_B", size=8)
            self.set_text_color(140, 140, 140)
            self.cell(
                self.w - self.l_margin - self.r_margin, 5,
                f"Hermes | {period_str} | {store_label}",
                align="R", new_x="LMARGIN", new_y="NEXT",
            )
            self.set_text_color(0, 0, 0)
            self.ln(1)

        def footer(self):
            self.set_y(-12)
            self.set_x(self.l_margin)
            self.set_font("DejaVu", size=8)
            self.set_text_color(140, 140, 140)
            self.cell(
                self.w - self.l_margin - self.r_margin, 6,
                f"Стр. {self.page_no()}", align="C",
            )
            self.set_text_color(0, 0, 0)

    pdf = _PDF(orientation="P", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=16)
    pdf.add_font("DejaVu",   style="", fname=_FONT)
    pdf.add_font("DejaVu_B", style="", fname=_FONT_BOLD)
    pdf.set_margins(_MARGIN, _MARGIN, _MARGIN)

    eff_w = 210 - _MARGIN - _MARGIN   # 186 мм
    lbl_w = 118                        # ширина подписи
    val_w = eff_w - lbl_w             # ширина значения

    # ── Титульная страница ──────────────────────────────────────────────────────
    pdf.add_page()
    pdf.ln(10)

    # Заголовок — N12: период уже в колонтитуле, не дублируем в теле
    pdf.set_x(_MARGIN)
    pdf.set_font("DejaVu_B", size=16)
    pdf.cell(eff_w, 12, "ОТЧЁТ", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.set_x(_MARGIN)
    pdf.cell(eff_w, 12, "Цветочная База Дубравиных",
             align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(8)

    if summary:
        s = summary

        def _row(label: str, amount_kop: int, note: str = "",
                 bold: bool = False, red: bool = False) -> None:
            pdf.set_x(_MARGIN)
            if red:
                pdf.set_text_color(190, 40, 40)
            if bold:
                pdf.set_font("DejaVu_B", size=11)
            else:
                pdf.set_font("DejaVu", size=11)
            pdf.cell(lbl_w, 8, label, align="L")
            val_str = _fmt_rub(amount_kop)
            if note:
                val_str += f"  ({note})"
            pdf.cell(val_w, 8, val_str, align="R", new_x="LMARGIN", new_y="NEXT")
            pdf.set_font("DejaVu", size=11)
            if red:
                pdf.set_text_color(0, 0, 0)

        rev    = s["rev"]
        profit = s["profit"]

        profit_pct = f"{profit / rev * 100:.0f}%" if rev else ""
        spoil_pct  = f"{s['loss_spoil'] / rev * 100:.1f}% от выручки" if rev else ""

        # N1: явная подпись методики — «по себест. МойСклад»
        _row("Выручка",                        rev,    "")
        _row("Прибыль (по себест. МойСклад)",  profit, profit_pct)
        pdf.ln(2)

        # N2: операционные расходы и изъятия — раздельно
        _row("Операционные расходы",           s["op_expenses"], "")
        if s["owner_kop"] > 0:
            _row("Изъятия собственника",       s["owner_kop"], "не в Результате")

        # N3: списания с разбивкой по типу
        _row("Порча (розничные точки)",        s["loss_spoil"], spoil_pct)
        if s["loss_adj"] > 0:
            _row("Корректировки учёта (База)", s["loss_adj"], "")

        # Горизонтальная черта
        pdf.ln(2)
        y_hr = pdf.get_y()
        pdf.set_draw_color(100, 100, 100)
        pdf.set_line_width(0.4)
        pdf.line(_MARGIN, y_hr, 210 - _MARGIN, y_hr)
        pdf.set_draw_color(0, 0, 0)
        pdf.set_line_width(0.2)
        pdf.ln(3)

        is_loss = s["result"] < 0
        _row("Результат (без изъятий)", s["result"], "", bold=True, red=is_loss)
        pdf.ln(4)

        # Чеки — avg_check вычислен один раз в _pdf_summary (N10)
        pdf.set_font("DejaVu", size=11)
        pdf.set_x(_MARGIN)
        checks_str = (
            f"Чеков: {s['checks']:,}".replace(",", " ")
            + f"   .   Средний чек: {_fmt_rub(s['avg_check'])}"
            if s["checks"] else "Чеков: нет данных"
        )
        pdf.cell(eff_w, 8, checks_str, align="C", new_x="LMARGIN", new_y="NEXT")

        if s["attention"]:
            pdf.ln(5)
            pdf.set_x(_MARGIN)
            pdf.set_font("DejaVu_B", size=11)
            pdf.cell(eff_w, 8, "Требует внимания:", align="L", new_x="LMARGIN", new_y="NEXT")
            pdf.set_font("DejaVu", size=11)
            for item in s["attention"]:
                pdf.set_x(_MARGIN + 4)
                pdf.cell(eff_w - 4, 8, f"• {item}", align="L", new_x="LMARGIN", new_y="NEXT")

    else:
        pdf.set_font("DejaVu", size=11)
        pdf.set_x(_MARGIN)
        pdf.set_text_color(150, 150, 150)
        pdf.cell(eff_w, 8, "Сводка периода недоступна", align="C", new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(0, 0, 0)

    # N11: оглавление — грид (N | раздел | описание), левое выравнивание
    pdf.ln(8)
    pdf.set_text_color(110, 110, 110)
    n_col    = 7
    name_col = 36
    desc_col = eff_w - n_col - name_col
    for num, name, desc in _TOC_SECTIONS:
        pdf.set_x(_MARGIN)
        pdf.set_font("DejaVu", size=9)
        pdf.cell(n_col, 7, num, align="R")
        pdf.set_font("DejaVu_B", size=9)
        pdf.cell(name_col, 7, name, align="L")
        pdf.set_font("DejaVu", size=9)
        pdf.cell(desc_col, 7, desc, align="L", new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(0, 0, 0)

    # ── Вспомогательные функции ─────────────────────────────────────────────────

    def _section(title: str, content: str) -> None:
        pdf.add_page()
        pdf.set_x(_MARGIN)
        pdf.set_font("DejaVu_B", size=12)
        pdf.cell(eff_w, 9, title, new_x="LMARGIN", new_y="NEXT")
        y_line = pdf.get_y()
        pdf.set_draw_color(180, 180, 180)
        pdf.set_line_width(0.3)
        pdf.line(_MARGIN, y_line, 210 - _MARGIN, y_line)
        pdf.set_draw_color(0, 0, 0)
        pdf.set_line_width(0.2)
        pdf.ln(3)
        pdf.set_font("DejaVu", size=8)

        for raw_line in content.split("\n"):
            cline = _clean(raw_line)
            cline = "".join(ch for ch in cline if ch >= " " or ch == "\n")
            if not cline.strip():
                pdf.ln(2)
                continue
            words = cline.split()
            cline = " ".join(w if len(w) <= 60 else w[:60] + "..." for w in words)
            pdf.set_x(_MARGIN)
            stripped = cline.lstrip()
            is_hdr   = stripped.startswith("--") or stripped.startswith("==")
            try:
                if is_hdr:
                    pdf.set_font("DejaVu_B", size=8)
                    pdf.multi_cell(eff_w, _LINE_H, cline, new_x="LMARGIN", new_y="NEXT")
                    pdf.set_font("DejaVu", size=8)
                else:
                    pdf.multi_cell(eff_w, _LINE_H, cline, new_x="LMARGIN", new_y="NEXT")
            except Exception:
                pdf.set_font("DejaVu", size=8)
                pdf.set_x(_MARGIN)
                short = cline[:80] if len(cline) > 80 else cline
                try:
                    pdf.multi_cell(eff_w, _LINE_H, short, new_x="LMARGIN", new_y="NEXT")
                except Exception:
                    pass

    def _image_page(title: str, png: bytes) -> None:
        pdf.add_page()
        pdf.set_x(_MARGIN)
        pdf.set_font("DejaVu_B", size=11)
        pdf.cell(eff_w, 8, title, new_x="LMARGIN", new_y="NEXT")
        pdf.ln(2)
        img_buf = io.BytesIO(png)
        img_h = round(eff_w / 1.5, 1)
        try:
            pdf.image(img_buf, x=_MARGIN, w=eff_w, h=img_h)
        except Exception:
            pdf.set_font("DejaVu", size=9)
            pdf.set_text_color(150, 150, 150)
            pdf.cell(eff_w, 8, "[График не удалось встроить]", align="C",
                     new_x="LMARGIN", new_y="NEXT")
            pdf.set_text_color(0, 0, 0)

    # ── Секция 1: Продажи ──────────────────────────────────────────────────────
    from .report_sales import build_sales_analytics
    _section("1. ПРОДАЖИ", build_sales_analytics(conn, d_from, d_to, store_name))

    if _charts_ok:
        try:
            png = _charts.chart_revenue_by_day(conn, d_from, d_to, store_name)
            if png:
                _image_page("График: выручка и прибыль по дням", png)
        except Exception:
            pass
        try:
            png = _charts.chart_stores_compare(conn, d_from, d_to)
            if png:
                _image_page("График: сравнение складов", png)
        except Exception:
            pass

    # ── Секция 2: Остатки ─────────────────────────────────────────────────────
    from .report_stock import build_stock_by_qty
    _section("2. ОСТАТКИ (СРЕЗКА)", build_stock_by_qty(conn, d_to, store_name, "СРЕЗКА"))

    # ── Секция 3: Залежалые ───────────────────────────────────────────────────
    from .report_stock import build_stock_report
    _section("3. ЗАЛЕЖАЛЫЕ (СРЕЗКА)", build_stock_report(conn, d_to, store_name, "СРЕЗКА", max_items=20))

    # ── Секция 4: Списания ────────────────────────────────────────────────────
    from .report_loss import build_loss_report
    _section("4. СПИСАНИЯ", build_loss_report(conn, d_from, d_to, store_name, max_docs=15))

    if _charts_ok:
        try:
            png = _charts.chart_losses_vs_revenue(conn, d_from, d_to, store_name)
            if png:
                _image_page("График: выручка и списания по дням", png)
        except Exception:
            pass

    # ── Секция 5: Расходы ─────────────────────────────────────────────────────
    from .report_cashflow import build_expenses_report
    _section("5. РАСХОДЫ", build_expenses_report(conn, d_from, d_to, store_name, max_items=20))

    # ── Секция 6: Клиенты ─────────────────────────────────────────────────────
    from .report_clients import build_clients_report
    _section("6. КЛИЕНТЫ", build_clients_report(conn, d_from, d_to, store_name))

    # ── Секция 7: Прогноз ─────────────────────────────────────────────────────
    from .report_forecast import build_forecast_report
    try:
        forecast_text = build_forecast_report(client, conn)
    except Exception as e:
        forecast_text = f"Не удалось построить прогноз: {e}"
    _section("7. ПРОГНОЗ", forecast_text)

    # ── Секция 8: Перемещения ─────────────────────────────────────────────────
    from .report_move import build_move_report
    _section("8. ПЕРЕМЕЩЕНИЯ", build_move_report(conn, d_from, d_to, store_name, max_docs=15))

    # ── Секция 9: Изменения ───────────────────────────────────────────────────
    from .report_audit import build_audit_report
    try:
        audit_text = build_audit_report(client, d_from, d_to)
    except Exception as e:
        audit_text = f"Не удалось получить аудит изменений: {e}"
    _section("9. ИЗМЕНЕНИЯ И УДАЛЕНИЯ", audit_text)

    return bytes(pdf.output())
