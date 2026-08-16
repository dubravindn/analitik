"""
PDF «Прогноз закупки СРЕЗКА» — forecast v10.
Движок: calc_forecast.build_forecast() — полная цепочка приёмка→продажи→прогноз.
Ориентация: альбомная A4 (297 × 210 мм) для аналитических таблиц.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

from fpdf import FPDF

from .pdf_kit import (
    INK, SAGE, SAGE_L, CREAM, TERRA, GRID,
    _FONT, _FONT_BOLD,
)
from .calc_forecast import (
    ForecastRow, ForecastStoreConfig, build_forecast, build_forecast_new, summarize,
    FORECAST_ENGINE, SREZKA_STORE_CONFIGS,
    PREV_WEEK_WEIGHT, YEAR_AGO_WEIGHT, SUPPLIER_DISCOUNT,
)

# ── Палитра статусов ──────────────────────────────────────────────────────────
_WHITE  = (255, 255, 255)
_GREEN  = (80,  140, 80)   # высокая достоверность
_YELLOW = (180, 140, 40)   # средняя / ручная проверка
_RED    = (190, 60,  60)   # риск / дефицит
_GRAY   = (140, 140, 140)  # не заказывать

# ── Разметка альбомной страницы ───────────────────────────────────────────────
_M  = 10          # поля 10 мм
_PW = 297         # ширина A4 landscape
_IW = _PW - 2*_M  # 277 мм рабочей ширины

# Колонки главной таблицы заказа (сумма = 277 мм)
_OH = ["№", "Товар", "Уп.", "Г.н.\nшт.", "Пр.н.\nшт.", "Ост.\nшт.",
       "Прог.", "Зак.\nуп.", "Зак.\nшт.", "Дост.", "Причина"]
_OW = [10, 78, 14, 16, 16, 16, 16, 16, 16, 18, 61]
assert sum(_OW) == _IW, f"order table width mismatch: {sum(_OW)} ≠ {_IW}"

# Колонки таблиц «Пропустить» / «Ручная проверка» (277 мм)
_SH = ["Товар", "Подгруппа", "Ост. шт.", "Продажи пр.н.", "Продажи г.н.", "Причина"]
_SW = [90, 30, 20, 22, 22, 93]
assert sum(_SW) == _IW

# NEW engine layout: товар + 3 периода + текущий остаток + заказ.
_NW = [110, 31, 31, 31, 37, 37]
assert sum(_NW) == _IW

_BASE_STORE_ID = "b4a45a8e-3d5e-11f0-0a80-0b690011c5d1"
_VOROVSKOGO_RETAIL_STORE_ID = "acb431e3-3c6b-11f0-0a80-0b6600098edd"
_RECEIPT_STORE_IDS = [_BASE_STORE_ID, _VOROVSKOGO_RETAIL_STORE_ID]


# ── Вспомогательные форматтеры ────────────────────────────────────────────────

def _rub(kop: float) -> str:
    return f"{int(kop) // 100:,}".replace(",", " ") + " ₽"

def _qty(q: float) -> str:
    if q == 0:
        return "—"
    return str(int(q)) if q == int(q) else f"{q:.1f}"

def _pct(p: float) -> str:
    return f"{int(p * 100)}%"

def _conf_color(conf: str) -> tuple:
    return {
        "high":   _GREEN,
        "medium": _YELLOW,
        "low":    _RED,
    }.get(conf, _GRAY)

def _conf_label(conf: str) -> str:
    return {"high": "Высокая", "medium": "Средняя", "low": "Низкая"}.get(conf, "—")

def _subgroup(fp: str) -> str:
    parts = (fp or "").split("/")
    return parts[2] if len(parts) >= 3 else (parts[-1] if parts else "—")


def forecast_group_name(folder_path: str) -> str:
    """Реальная товарная группа после служебного пути каталога СРЕЗКА."""
    parts = [p.strip() for p in (folder_path or "").split("/") if p.strip()]
    if len(parts) >= 5:
        return parts[4]
    return parts[-1] if parts else "Другое"


# ── Базовый PDF в альбомной ориентации ───────────────────────────────────────

class ForecastPDF(FPDF):
    def __init__(self, period: str = ""):
        super().__init__(orientation="L", unit="mm", format="A4")
        self.set_margins(_M, _M, _M)
        self.set_auto_page_break(auto=True, margin=12)
        self.add_font("DejaVu",   style="", fname=_FONT)
        self.add_font("DejaVu_B", style="", fname=_FONT_BOLD)
        self._period = period
        self._gen = date.today().strftime("%d.%m.%Y")

    def header(self):
        self.set_x(_M)
        self.set_font("DejaVu_B", size=7.5)
        self.set_text_color(*SAGE)
        label = f"Hermes  ·  Прогноз СРЕЗКА  ·  {self._period}"
        self.cell(self.w - 2*_M, 4.5, label, align="R",
                  new_x="LMARGIN", new_y="NEXT")
        self.set_text_color(*INK)
        self.ln(1)

    def footer(self):
        self.set_y(-10)
        self.set_x(_M)
        self.set_font("DejaVu", size=7.5)
        self.set_text_color(*SAGE)
        self.cell(self.w - 2*_M, 5,
                  f"Стр. {self.page_no()}  ·  {self._gen}", align="C")
        self.set_text_color(*INK)

    # ── Утилиты разметки ─────────────────────────────────────────────────────

    def hline(self, color=GRID, lw=0.25):
        y = self.get_y()
        self.set_draw_color(*color)
        self.set_line_width(lw)
        self.line(_M, y, _PW - _M, y)
        self.set_draw_color(0, 0, 0)
        self.set_line_width(0.2)

    def section_title(self, text: str, size: int = 14):
        self.set_font("DejaVu_B", size=size)
        self.set_text_color(*INK)
        self.set_x(_M)
        self.cell(_IW, 9, text, align="L", new_x="LMARGIN", new_y="NEXT")
        self.hline(SAGE, 0.6)
        self.ln(3)

    def subsection(self, text: str):
        self.ln(2)
        self.set_font("DejaVu_B", size=9)
        self.set_text_color(*INK)
        self.set_x(_M)
        self.cell(_IW, 6, text, align="L", new_x="LMARGIN", new_y="NEXT")
        self.hline(GRID)
        self.ln(1)

    def kpi_box(self, items: list[tuple[str, str, str]]):
        """items = [(label, value, unit), ...]"""
        n = len(items)
        bw = _IW / n
        bh = 18.0
        x0 = float(_M)
        y0 = self.get_y()
        for i, (lbl, val, unit) in enumerate(items):
            bx = x0 + i * bw
            fill = SAGE_L if i % 2 == 0 else CREAM
            self.set_fill_color(*fill)
            self.rect(bx, y0, bw, bh, style="F")
            self.set_font("DejaVu_B", size=10)
            self.set_text_color(*INK)
            self.set_xy(bx, y0 + 1.5)
            self.cell(bw, 6, f"{val} {unit}".strip(), align="C")
            self.set_font("DejaVu", size=7)
            self.set_text_color(*SAGE)
            self.set_xy(bx, y0 + 10)
            self.cell(bw, 5, lbl, align="C")
        self.set_y(y0 + bh + 3)
        self.set_text_color(*INK)

    def callout(self, text: str, color=SAGE):
        self.ln(1)
        x0 = float(_M)
        y0 = self.get_y()
        self.set_x(x0 + 3.5)
        self.set_font("DejaVu", size=8.5)
        self.set_text_color(*color)
        self.multi_cell(_IW - 3.5, 4.5, text, align="L")
        y1 = self.get_y()
        self.set_fill_color(*color)
        self.rect(x0, y0, 2, max(y1 - y0, 5), style="F")
        self.set_text_color(*INK)
        self.ln(1)

    # ── Таблица с автопереносом заголовка ────────────────────────────────────

    def draw_table_header(self, headers, col_widths, row_h=8.5):
        self.set_x(_M)
        self.set_font("DejaVu_B", size=7.5)
        self.set_fill_color(*INK)
        self.set_text_color(*_WHITE)
        y0 = self.get_y()
        x = float(_M)
        for hdr, w in zip(headers, col_widths):
            lines = hdr.split("\n")
            lh = row_h / max(len(lines), 1)
            self.set_fill_color(*INK)
            self.rect(x, y0, w, row_h, style="FD")
            for li, line in enumerate(lines):
                self.set_xy(x, y0 + li * lh)
                self.cell(w, lh, line, align="C")
            x += w
        self.set_xy(float(_M), y0 + row_h)
        self.set_text_color(*INK)

    def draw_table_row(self, cells, col_widths, aligns, row_h, fill, text_colors=None):
        if self.get_y() + row_h > self.page_break_trigger:
            self.add_page()
            return False  # caller re-draws header
        y0 = self.get_y()
        self.set_fill_color(*fill)
        self.set_font("DejaVu", size=7.5)
        x = float(_M)
        for j, (cell, w, al) in enumerate(zip(cells, col_widths, aligns)):
            tc = text_colors[j] if text_colors else INK
            self.set_text_color(*tc)
            self.set_xy(x, y0)
            self.cell(w, row_h, str(cell), border=1, fill=True, align=al)
            x += w
        self.set_xy(float(_M), y0 + row_h)
        self.set_text_color(*INK)
        return True

    def group_divider(self, label: str, col_widths):
        if self.get_y() + 6 > self.page_break_trigger:
            self.add_page()
        self.set_fill_color(*SAGE)
        self.set_text_color(*_WHITE)
        self.set_font("DejaVu_B", size=8)
        self.set_x(_M)
        self.cell(sum(col_widths), 5.5, f"  {label}",
                  border=0, fill=True, new_x="LMARGIN", new_y="NEXT")
        self.set_text_color(*INK)


# ── Генератор PDF ─────────────────────────────────────────────────────────────


def _query_sales_period(
    conn, pids: list[str], d_from: date, d_to: date,
) -> dict[str, float]:
    """Продажи БАЗА за точный календарный период."""
    if d_to < d_from or not pids:
        return {}
    with conn.cursor() as cur:
        cur.execute("""
            SELECT assortment_id, SUM(sell_qty)
            FROM sales_by_product_day
            WHERE day BETWEEN %s AND %s
              AND store_id = %s
              AND sell_qty > 0
              AND assortment_id = ANY(%s)
            GROUP BY assortment_id
        """, [d_from, d_to, _BASE_STORE_ID, pids])
        return {row[0]: float(row[1]) for row in cur.fetchall()}


def _query_year_ago_receipts(
    conn, pids: list[str], d_from: date, d_to: date,
) -> dict[str, float]:
    """Приёмки год назад: БАЗА + Розница Воровского, ООО «Поставщик»."""
    if not pids:
        return {}
    with conn.cursor() as cur:
        cur.execute("""
            SELECT si.product_id, SUM(si.qty)
            FROM supply_item si
            JOIN supply_doc sd ON sd.doc_id = si.doc_id
            WHERE sd.day BETWEEN %s AND %s
              AND sd.store_id = ANY(%s)
              AND sd.agent_name ILIKE '%%поставщик%%'
              AND si.product_id = ANY(%s)
            GROUP BY si.product_id
        """, [d_from, d_to, _RECEIPT_STORE_IDS, pids])
        return {row[0]: float(row[1] or 0) for row in cur.fetchall()}


def _fmt_range(d_from: date, d_to: date, with_year: bool = False) -> str:
    right = d_to.strftime("%d.%m.%y" if with_year else "%d.%m")
    return f"{d_from.strftime('%d.%m')}-{right}"


def _build_pdf_new(conn, date_from: date, date_to: date, token: str) -> bytes:
    """
    PDF прогноза БАЗА на NEW engine.

    Приёмка год назад — БАЗА + Розница Воровского (историческая преемственность).
    Продажи, остаток и заказ — только БАЗА.
    """
    report_date = date.today()
    period = f"{date_from.strftime('%d.%m')}-{date_to.strftime('%d.%m.%Y')}"
    cutoff = report_date

    current_from = report_date - timedelta(days=report_date.weekday())
    current_to = report_date - timedelta(days=1)
    previous_from = current_from - timedelta(days=7)
    previous_to = current_from - timedelta(days=1)
    year_from = date_from - timedelta(weeks=52)
    year_to = date_to - timedelta(weeks=52)

    with conn.cursor() as cur:
        cur.execute("""
            SELECT product_id, product_name, folder_path
            FROM product_dim
            WHERE is_srezka = TRUE
        """)
        product_meta = {
            row[0]: (row[1], row[2] or "") for row in cur.fetchall()
        }
    pids = list(product_meta)

    year_receipts = _query_year_ago_receipts(conn, pids, year_from, year_to)
    previous_sales = _query_sales_period(conn, pids, previous_from, previous_to)
    current_sales = _query_sales_period(conn, pids, current_from, current_to)

    base_configs = [c for c in SREZKA_STORE_CONFIGS if c.store_id == _BASE_STORE_ID]
    if len(base_configs) != 1:
        raise RuntimeError("Не найден единственный конфиг склада БАЗА")
    new_results = build_forecast_new(
        conn, token, base_configs,
        cutoff_date=cutoff,
        horizon_from=date_from,
        horizon_to=date_to,
    )

    with conn.cursor() as cur:
        cur.execute("""
            SELECT MAX(day)
            FROM stock_snapshot
            WHERE day <= %s AND store_id = %s AND is_srezka = TRUE
        """, [cutoff, _BASE_STORE_ID])
        stock_day = cur.fetchone()[0]

    # Один склад, но оставляем агрегацию по product_id для устойчивости отчёта.
    agg: dict[str, dict] = {}
    for r in new_results:
        if r.product_id not in agg:
            agg[r.product_id] = {"avail": 0.0, "order": 0.0, "name": r.product_name}
        if r.available_stock is not None:
            agg[r.product_id]["avail"] += r.available_stock
        if r.recommended_order_qty is not None:
            agg[r.product_id]["order"] += r.recommended_order_qty

    # 5. Строки: только те, где К заказу > 0
    rows_out = []
    for pid, na in agg.items():
        order = int(na["order"])
        if order <= 0:
            continue
        meta = product_meta.get(pid)
        name = meta[0] if meta else na["name"]
        group = forecast_group_name(meta[1] if meta else "")
        rows_out.append((
            group, name,
            year_receipts.get(pid, 0.0),
            previous_sales.get(pid, 0.0),
            current_sales.get(pid, 0.0),
            na["avail"], order,
        ))

    rows_out.sort(key=lambda x: (x[0].casefold(), x[1].casefold()))

    # 6. Построение PDF
    total_order_qty = sum(r[6] for r in rows_out)
    pdf = ForecastPDF(period)
    pdf.add_page()
    pdf.section_title(f"СРЕЗКА — К ЗАКАЗУ  ·  {period}", size=14)

    # Строка статистики
    pdf.set_font("DejaVu", size=8.5)
    pdf.set_text_color(*SAGE)
    pdf.set_x(_M)
    pdf.cell(_IW, 5,
             f"Склад: БАЗА  ·  Движок: NEW  ·  К заказу: {len(rows_out)} позиций  ·  {total_order_qty} шт.",
             align="L", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(1)

    # Пояснение логики расчёта
    pdf.set_font("DejaVu", size=7.5)
    pdf.set_text_color(*SAGE)
    pdf.set_x(_M)
    pdf.cell(_IW, 4.5,
             f"Приёмка год назад ({_fmt_range(year_from, year_to, True)}): БАЗА + Розница Воровского.",
             align="L", new_x="LMARGIN", new_y="NEXT")
    pdf.set_x(_M)
    pdf.cell(_IW, 4.5,
             f"Продажи БАЗА: прошлая неделя {_fmt_range(previous_from, previous_to)}; эта неделя {_fmt_range(current_from, current_to)}.",
             align="L", new_x="LMARGIN", new_y="NEXT")
    pdf.set_x(_M)
    pdf.cell(_IW, 4.5,
             f"Остаток БАЗА на {stock_day.strftime('%d.%m.%Y') if stock_day else 'нет снимка'}: физический остаток минус резерв.",
             align="L", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(1)
    pdf.set_x(_M)
    pdf.cell(_IW, 4.5,
             "К заказу БАЗА = ожидаемый спрос - доступный остаток; будущие поступления пока не учитываются.",
             align="L", new_x="LMARGIN", new_y="NEXT")

    pdf.set_text_color(*INK)
    pdf.set_font("DejaVu", size=8.5)
    pdf.ln(3)

    if not rows_out:
        pdf.callout("Нет позиций к автоматическому заказу.", SAGE)
        return bytes(pdf.output())

    headers = [
        "Название товара",
        f"Приёмка\nгод назад\n{_fmt_range(year_from, year_to, True)}",
        f"Продажи\nпрошлая нед.\n{_fmt_range(previous_from, previous_to)}",
        f"Продажи\nэта нед.\n{_fmt_range(current_from, current_to)}",
        f"Остаток БАЗА\nна {stock_day.strftime('%d.%m') if stock_day else '—'}",
        f"К заказу БАЗА\n{_fmt_range(date_from, date_to)}",
    ]
    _NA = ["L", "C", "C", "C", "C", "R"]

    def _draw_new_header():
        pdf.draw_table_header(headers, _NW, row_h=13.5)

    _draw_new_header()
    cur_sg = None
    zidx = 0
    for sg, name, year_received, prev, curr, avail, order in rows_out:
        if sg != cur_sg:
            pdf.group_divider(sg, _NW)
            cur_sg = sg
        fill = CREAM if zidx % 2 == 0 else _WHITE
        avail_str   = "0 ⚠" if avail <= 0 else _qty(avail)
        avail_color = _RED if avail <= 0 else INK
        cells = [name, _qty(year_received), _qty(prev), _qty(curr), avail_str, str(order)]
        tcolors = [INK, INK, INK, INK, avail_color, INK]
        ok = pdf.draw_table_row(cells, _NW, _NA, 6.5, fill, tcolors)
        if not ok:
            _draw_new_header()
            pdf.draw_table_row(cells, _NW, _NA, 6.5, fill, tcolors)
        zidx += 1

    # Итого
    total_order = sum(r[6] for r in rows_out)
    y0 = pdf.get_y()
    if y0 + 7 > pdf.page_break_trigger:
        pdf.add_page()
    pdf.set_fill_color(*INK)
    pdf.set_text_color(*_WHITE)
    pdf.set_font("DejaVu_B", size=8)
    pdf.set_x(_M)
    lw = sum(_NW[:-1])
    pdf.cell(lw, 7, f"  Итого к заказу: {len(rows_out)} позиций",
             border=1, fill=True, align="L")
    pdf.cell(_NW[-1], 7, str(total_order),
             border=1, fill=True, align="R",
             new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(*INK)

    return bytes(pdf.output())


def build_forecast_pdf(
    conn,
    date_from:   date,
    date_to:     date,
    report_date: date | None = None,
    token:       str  | None = None,
) -> bytes:
    """
    Полный прогноз закупки СРЕЗКА на альбомных страницах.
    date_from/date_to — целевая неделя заказа.
    token — МойСклад-токен; при FORECAST_ENGINE="new" обязателен.
    """
    if FORECAST_ENGINE == "new" and token:
        return _build_pdf_new(conn, date_from, date_to, token)

    if report_date is None:
        report_date = date.today()

    days      = max((date_to - date_from).days + 1, 1)
    prev_from = date_from - timedelta(days=days)
    prev_to   = date_to   - timedelta(days=days)
    ya_from   = date_from - timedelta(weeks=52)
    ya_to     = date_to   - timedelta(weeks=52)

    period = f"{date_from.strftime('%d.%m')}–{date_to.strftime('%d.%m.%Y')}"
    prev_lbl = f"{prev_from.strftime('%d.%m')}–{prev_to.strftime('%d.%m')}"
    ya_lbl   = f"{ya_from.strftime('%d.%m.%y')}–{ya_to.strftime('%d.%m.%y')}"

    # ── Расчёт ────────────────────────────────────────────────────────────────
    rows = build_forecast(conn, date_from, date_to, report_date)
    if not rows:
        pdf = ForecastPDF(period)
        pdf.add_page()
        pdf.section_title(f"Прогноз закупки СРЕЗКА  ·  {period}")
        pdf.callout("Нет товаров с is_srezka=TRUE в product_dim.", _RED)
        return bytes(pdf.output())

    sm = summarize(rows)

    # ── Разбивка по категориям ───────────────────────────────────────────────
    order_rows  = sorted(
        [r for r in rows if r.category == "order"],
        key=lambda r: (
            0 if r.confidence == "high" else 1 if r.confidence == "medium" else 2,
            -r.order_units,
            r.product_name,
        ),
    )
    skip_rows   = sorted([r for r in rows if r.category == "skip"],
                         key=lambda r: r.product_name)
    manual_rows = sorted([r for r in rows if r.category == "manual"],
                         key=lambda r: r.product_name)

    # ── Построение PDF ────────────────────────────────────────────────────────
    pdf = ForecastPDF(period)

    # ══════════════════════════════════════════════════════════════════════════
    # Страница 1: Сводка
    # ══════════════════════════════════════════════════════════════════════════
    pdf.add_page()
    pdf.section_title(f"Прогноз закупки СРЕЗКА  ·  {period}", size=16)

    # Периоды
    pdf.set_font("DejaVu", size=8.5)
    pdf.set_text_color(*SAGE)
    pdf.set_x(_M)
    pdf.cell(_IW, 5,
             f"Целевая неделя: {period}   "
             f"Прошлая неделя: {prev_lbl}   "
             f"Год назад: {ya_lbl}   "
             f"Данные актуальны на: {report_date.strftime('%d.%m.%Y')}",
             align="L", new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(*INK)
    pdf.ln(3)

    pdf.set_font("DejaVu", size=8)
    pdf.set_x(_M)
    pdf.cell(_IW, 5,
             f"Формула спроса: {int(PREV_WEEK_WEIGHT*100)}% прошлой недели + "
             f"{int(YEAR_AGO_WEIGHT*100)}% год назад  ·  "
             f"Скидка ООО Поставщик: {int(SUPPLIER_DISCOUNT*100)}%",
             align="L", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    # KPI — заказ
    pdf.kpi_box([
        ("Позиций к заказу",     str(sm["n_order"]),      ""),
        ("Упаковок к заказу",    str(sm["total_packs"]),   ""),
        ("Единиц к заказу",      str(sm["total_units"]),   "шт."),
        ("Стоимость до скидки",  _rub(sm["cost_before_kop"]), ""),
        (f"Скидка {int(SUPPLIER_DISCOUNT*100)}%", _rub(sm["discount_kop"]), ""),
        ("Итоговая стоимость",   _rub(sm["cost_after_kop"]),  ""),
    ])

    # KPI — достоверность
    pdf.kpi_box([
        ("Высокая достов.",  str(sm["confidence_high"]),   "поз."),
        ("Средняя достов.",  str(sm["confidence_medium"]), "поз."),
        ("Ручная проверка",  str(sm["confidence_low"]),    "поз."),
        ("Пропустить",       str(sm["n_skip"]),            "поз."),
    ])

    # Риски
    pdf.subsection("Риски и качество данных")
    risk_items = []
    if sm["n_deficit"]:
        risk_items.append(f"⚠ Возможный дефицит: {sm['n_deficit']} позиций — продажи могли быть ограничены отсутствием товара")
    if sm["n_high_remainder"]:
        risk_items.append(f"⚠ Большой непроданный остаток (sell-through < 60%): {sm['n_high_remainder']} позиций — заказ занижен")
    if sm["n_incomplete_obs"]:
        risk_items.append(f"⚠ Неполное окно наблюдения (< 7 дней после приёмки): {sm['n_incomplete_obs']} позиций → перенесены в ручную проверку")
    if sm["n_no_pack"]:
        risk_items.append(f"⚠ Неизвестный размер упаковки (по умолчанию 1 шт.): {sm['n_no_pack']} позиций — проверить карточку товара")

    if risk_items:
        for r in risk_items:
            pdf.callout(r, _RED)
    else:
        pdf.callout("Критических рисков не обнаружено.", _GREEN)

    pdf.ln(3)
    pdf.callout(
        f"Данные: приёмки (supply_item + supply_doc), продажи (sales_by_product_day), "
        f"остатки (stock_snapshot), списания (loss_item). "
        f"Подтверждённые будущие поставки не учтены (нет данных). "
        f"Размер упаковки: парсинг из названия товара.",
        SAGE,
    )

    # ══════════════════════════════════════════════════════════════════════════
    # Страницы: Список заказа
    # ══════════════════════════════════════════════════════════════════════════
    pdf.add_page()
    pdf.section_title(
        f"Список заказа  ·  {sm['n_order']} позиций  ·  "
        f"{_rub(sm['cost_after_kop'])} (со скидкой)"
    )

    _OA = ["C", "L", "C", "R", "R", "R", "R", "R", "R", "C", "L"]

    def _draw_order_header():
        pdf.draw_table_header(_OH, _OW, row_h=8.5)

    def _draw_order_row(n: int, r: ForecastRow, zidx: int):
        fill = CREAM if zidx % 2 == 0 else _WHITE
        c_color = _conf_color(r.confidence)
        cells = [
            str(n),
            r.product_name,
            str(r.pack_size),
            _qty(r.year_ago_demand),
            _qty(r.prev_demand),
            _qty(r.available_stock),
            _qty(r.base_demand),
            str(r.order_packs),
            str(r.order_units),
            _conf_label(r.confidence),
            r.reason,
        ]
        tcolors = [INK]*9 + [c_color, INK]
        ok = pdf.draw_table_row(cells, _OW, _OA, 6.5, fill, tcolors)
        if not ok:
            _draw_order_header()
            pdf.draw_table_row(cells, _OW, _OA, 6.5, fill, tcolors)

    if order_rows:
        # Группировка по подгруппам
        groups: dict[str, list[ForecastRow]] = defaultdict(list)
        for r in order_rows:
            groups[r.subgroup].append(r)

        _draw_order_header()
        n = 1
        zidx = 0
        for sg in sorted(groups):
            pdf.group_divider(sg, _OW)
            for r in groups[sg]:
                _draw_order_row(n, r, zidx)
                n += 1
                zidx += 1

        # Итого
        y0 = pdf.get_y()
        if y0 + 7 > pdf.page_break_trigger:
            pdf.add_page()
        pdf.set_fill_color(*INK)
        pdf.set_text_color(*_WHITE)
        pdf.set_font("DejaVu_B", size=8)
        pdf.set_x(_M)
        lw = _OW[0] + _OW[1] + _OW[2] + _OW[3] + _OW[4] + _OW[5] + _OW[6]
        pdf.cell(lw, 7, f"  Итого к заказу: {sm['n_order']} позиций",
                 border=1, fill=True, align="L")
        pdf.cell(_OW[7], 7, str(sm["total_packs"]),
                 border=1, fill=True, align="R")
        pdf.cell(_OW[8], 7, str(sm["total_units"]),
                 border=1, fill=True, align="R")
        rw = _OW[9] + _OW[10]
        pdf.cell(rw, 7, _rub(sm["cost_after_kop"]),
                 border=1, fill=True, align="R",
                 new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(*INK)

        # Сноска скидка
        pdf.ln(2)
        pdf.set_font("DejaVu", size=7.5)
        pdf.set_text_color(*SAGE)
        pdf.set_x(_M)
        pdf.cell(_IW, 4.5,
                 f"Стоимость до скидки: {_rub(sm['cost_before_kop'])}  ·  "
                 f"Скидка ООО Поставщик 7%: −{_rub(sm['discount_kop'])}  ·  "
                 f"Итого: {_rub(sm['cost_after_kop'])}",
                 align="L", new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(*INK)
    else:
        pdf.callout("Нет позиций к автоматическому заказу.", SAGE)

    # ══════════════════════════════════════════════════════════════════════════
    # Страница: Не заказывать
    # ══════════════════════════════════════════════════════════════════════════
    if skip_rows:
        pdf.add_page()
        pdf.section_title(f"Не заказывать  ·  {len(skip_rows)} позиций")
        pdf.callout(
            "Позиции с нулевым заказом: остаток покрывает спрос, слабые продажи или большой непроданный остаток.",
            SAGE,
        )
        pdf.draw_table_header(_SH, _SW)
        for zidx, r in enumerate(skip_rows):
            fill = CREAM if zidx % 2 == 0 else _WHITE
            cells = [
                r.product_name,
                r.subgroup,
                _qty(r.available_stock),
                _qty(r.prev_demand),
                _qty(r.year_ago_demand),
                r.reason,
            ]
            ok = pdf.draw_table_row(cells, _SW, ["L","C","R","R","R","L"], 6.0, fill)
            if not ok:
                pdf.draw_table_header(_SH, _SW)
                pdf.draw_table_row(cells, _SW, ["L","C","R","R","R","L"], 6.0, fill)

    # ══════════════════════════════════════════════════════════════════════════
    # Страница: Ручная проверка
    # ══════════════════════════════════════════════════════════════════════════
    if manual_rows:
        pdf.add_page()
        pdf.section_title(f"Ручная проверка  ·  {len(manual_rows)} позиций")
        pdf.callout(
            "Позиции с низкой достоверностью: нет истории в обоих периодах, неизвестная упаковка, "
            "неполное окно наблюдения, противоречивые данные. Требуют решения менеджера.",
            _YELLOW,
        )
        _MH = ["Товар", "Подгруппа", "Уп.", "Ост.шт.", "Прод.пр.н.", "Прод.г.н.", "Достов.", "Причина"]
        _MW = [82, 28, 12, 18, 18, 18, 18, 83]
        assert sum(_MW) == _IW
        pdf.draw_table_header(_MH, _MW)
        for zidx, r in enumerate(manual_rows):
            fill = CREAM if zidx % 2 == 0 else _WHITE
            cells = [
                r.product_name,
                r.subgroup,
                str(r.pack_size) + ("*" if r.pack_size_source == "default" else ""),
                _qty(r.available_stock),
                _qty(r.prev_demand),
                _qty(r.year_ago_demand),
                _conf_label(r.confidence),
                r.reason,
            ]
            tcolors = [INK]*6 + [_conf_color(r.confidence), INK]
            ok = pdf.draw_table_row(cells, _MW,
                                    ["L","C","C","R","R","R","C","L"],
                                    6.0, fill, tcolors)
            if not ok:
                pdf.draw_table_header(_MH, _MW)
                pdf.draw_table_row(cells, _MW,
                                   ["L","C","C","R","R","R","C","L"],
                                   6.0, fill, tcolors)

        pdf.ln(2)
        pdf.set_font("DejaVu", size=7.5)
        pdf.set_text_color(*SAGE)
        pdf.set_x(_M)
        pdf.cell(_IW, 4, "* — размер упаковки определён по умолчанию (1 шт.), уточнить в карточке товара.",
                 align="L", new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(*INK)

    return bytes(pdf.output())
