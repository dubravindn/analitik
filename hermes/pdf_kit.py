"""Переиспользуемые PDF-компоненты в брендовом стиле ЦБД.

Все функции принимают HermesPDF как первый аргумент и мутируют его на месте.
Caller в конце вызывает pdf.output() -> bytes.

Шрифты: "DejaVu" (обычный) и "DejaVu_B" (жирный) — отдельные имена, не стиль.
"""
from __future__ import annotations

import io
from datetime import date

try:
    from fpdf import FPDF
except ImportError:
    raise RuntimeError("Установите fpdf2: pip install fpdf2")

# -- Палитра ЦБД ---------------------------------------------------------------
SAGE   = (138, 154, 123)   # #8A9A7B  шалфей насыщенный
SAGE_L = (189, 198, 179)   # #BDC6B3  шалфей светлый
INK    = (26,  26,  26)    # #1A1A1A  чёрный
CREAM  = (253, 251, 247)   # #FDFBF7  кремовый фон
TERRA  = (199, 123, 88)    # #C77B58  терракот
GRID   = (227, 225, 218)   # #E3E1DA  сетка
_WHITE = (255, 255, 255)

_FONT      = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
_FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
_MARGIN  = 18         # мм
_PAGE_W  = 210        # A4 ширина
_INNER_W = _PAGE_W - 2 * _MARGIN   # 174 мм


class HermesPDF(FPDF):
    """A4-документ с брендовыми шрифтами, колонтитулами и CREAM-фоном."""

    def __init__(self, section_title: str = "", period: str = "", store: str = ""):
        super().__init__(orientation="P", unit="mm", format="A4")
        self.set_margins(_MARGIN, _MARGIN, _MARGIN)
        self.set_auto_page_break(auto=True, margin=16)
        self.add_font("DejaVu",   style="", fname=_FONT)
        self.add_font("DejaVu_B", style="", fname=_FONT_BOLD)
        self._section_title = section_title
        self._period  = period
        self._store   = store
        self._gen_date = date.today().strftime("%d.%m.%Y")

    def header(self):
        self.set_x(self.l_margin)
        self.set_font("DejaVu_B", size=8)
        self.set_text_color(*SAGE)
        parts = [p for p in [self._period, self._store, self._section_title] if p]
        label = "  ·  ".join(["Hermes"] + parts)
        self.cell(
            self.w - self.l_margin - self.r_margin, 5,
            label, align="R", new_x="LMARGIN", new_y="NEXT",
        )
        self.set_text_color(*INK)
        self.ln(1)

    def footer(self):
        self.set_y(-12)
        self.set_x(self.l_margin)
        self.set_font("DejaVu", size=8)
        self.set_text_color(*SAGE)
        self.cell(
            self.w - self.l_margin - self.r_margin, 6,
            f"Стр. {self.page_no()} · {self._gen_date}", align="C",
        )
        self.set_text_color(*INK)


# -- Вспомогательные -----------------------------------------------------------

def _hline(pdf: HermesPDF, color: tuple = GRID, width: float = 0.3) -> None:
    """Горизонтальная линия на всю ширину текстовой зоны."""
    y = pdf.get_y()
    pdf.set_draw_color(*color)
    pdf.set_line_width(width)
    pdf.line(_MARGIN, y, _PAGE_W - _MARGIN, y)
    pdf.set_draw_color(0, 0, 0)
    pdf.set_line_width(0.2)


# -- Компоненты ----------------------------------------------------------------

def cover(pdf: HermesPDF, title: str) -> None:
    """Крупный заголовок раздела с цветной чертой."""
    pdf.set_font("DejaVu_B", size=20)
    pdf.set_text_color(*INK)
    pdf.set_x(_MARGIN)
    pdf.cell(_INNER_W, 12, title, align="L", new_x="LMARGIN", new_y="NEXT")
    _hline(pdf, SAGE, 0.8)
    pdf.ln(5)


def kpi_row(pdf: HermesPDF, items: list[tuple]) -> None:
    """Ряд KPI-плашек.

    items = [(подпись, значение, единица), ...]
         или [(подпись, значение, единица, pct_str), ...]

    Если pct_str задан — рендерит его мелко серым под значением.
    """
    n = len(items)
    if not n:
        return
    box_w = _INNER_W / n
    box_h = 21
    x0    = float(_MARGIN)
    y0    = pdf.get_y()

    for i, item in enumerate(items):
        label, value, unit = item[0], item[1], item[2]
        pct_str = item[3] if len(item) > 3 else None

        bx = x0 + i * box_w
        pdf.set_fill_color(*(SAGE_L if i % 2 == 0 else CREAM))
        pdf.rect(bx, y0, box_w, box_h, style="F")

        # Значение (жирным, крупно)
        fs = 13 if n <= 3 else 11 if n <= 5 else 9
        pdf.set_font("DejaVu_B", size=fs)
        pdf.set_text_color(*INK)
        display = f"{value} {unit}".strip()
        pdf.set_xy(bx, y0 + 2)
        pdf.cell(box_w, 7, display, align="C")

        # Процент (мелко, серым) — только если передан
        if pct_str:
            pdf.set_font("DejaVu", size=7.5)
            pdf.set_text_color(*SAGE)
            pdf.set_xy(bx, y0 + 9)
            pdf.cell(box_w, 4, pct_str, align="C")

        # Подпись
        pdf.set_font("DejaVu", size=7.5)
        pdf.set_text_color(*SAGE)
        pdf.set_xy(bx, y0 + 14 if pct_str else y0 + 12)
        pdf.cell(box_w, 5, label, align="C")

    pdf.set_y(y0 + box_h + 4)
    pdf.set_text_color(*INK)


def section_header(pdf: HermesPDF, text: str) -> None:
    """Заголовок подраздела (12pt bold)."""
    pdf.ln(3)
    pdf.set_x(_MARGIN)
    pdf.set_font("DejaVu_B", size=12)
    pdf.set_text_color(*INK)
    pdf.cell(_INNER_W, 7, text, align="L", new_x="LMARGIN", new_y="NEXT")
    _hline(pdf, GRID, 0.2)
    pdf.ln(2)


def table(
    pdf: HermesPDF,
    headers: list[str],
    rows: list[list[str]],
    col_widths: list[float] | None = None,
    aligns: list[str] | None = None,
    zebra: bool = True,
    font_size: float = 9,
    max_rows: int | None = None,
    overflow_note: str = "",
    row_styles: list[str | None] | None = None,
) -> None:
    """Таблица: шапка INK/white, чередование строк CREAM/white.

    row_styles[i]: None/'normal' — обычная строка; 'detail' — мелкая SAGE-строка
    (подстроки «в т.ч.»), не учитывается в зебре.
    max_rows — обрезать обычные строки (detail не считаются).
    """
    n_cols = len(headers)
    col_widths = col_widths or [_INNER_W / n_cols] * n_cols
    aligns     = aligns     or ["L"] * n_cols
    row_h = 5.5

    # Обрезка по max_rows: считаем только non-detail строки
    if max_rows is not None and row_styles:
        normal_count = sum(1 for s in row_styles if s != "detail")
        if normal_count > max_rows:
            # Обрезаем с конца, сохраняя detail-строки вместе с родителем
            new_rows, new_styles = [], []
            kept = 0
            for r, s in zip(rows, row_styles):
                if s == "detail":
                    new_rows.append(r); new_styles.append(s)
                elif kept < max_rows:
                    new_rows.append(r); new_styles.append(s); kept += 1
            rows, row_styles = new_rows, new_styles
            hidden = normal_count - max_rows
        else:
            hidden = 0
    elif max_rows and len(rows) > max_rows:
        hidden = len(rows) - max_rows
        rows = rows[:max_rows]
        if row_styles:
            row_styles = row_styles[:max_rows]
    else:
        hidden = 0

    def _header_row():
        pdf.set_x(_MARGIN)
        pdf.set_font("DejaVu_B", size=font_size)
        pdf.set_text_color(*_WHITE)
        pdf.set_fill_color(*INK)
        for j, (hdr, w, al) in enumerate(zip(headers, col_widths, aligns)):
            nx = "LMARGIN" if j == n_cols - 1 else "RIGHT"
            ny = "NEXT"    if j == n_cols - 1 else "TOP"
            pdf.cell(w, row_h + 1, hdr, align=al, fill=True,
                     new_x=nx, new_y=ny)
        pdf.set_text_color(*INK)

    _header_row()

    zebra_idx = 0  # только для non-detail строк
    for i, row in enumerate(rows):
        style = row_styles[i] if row_styles and i < len(row_styles) else None
        is_detail = (style == "detail")

        row_fs   = font_size - 1 if is_detail else font_size
        row_color = SAGE if is_detail else INK
        dh = 5.0 if is_detail else row_h

        if pdf.get_y() + dh > pdf.page_break_trigger:
            pdf.add_page()
            _header_row()

        if is_detail:
            fill = False
        else:
            fill = zebra and (zebra_idx % 2 == 0)
            zebra_idx += 1

        if fill:
            pdf.set_fill_color(*CREAM)

        pdf.set_font("DejaVu", size=row_fs)
        pdf.set_text_color(*row_color)
        pdf.set_x(_MARGIN)
        for j, (cell_text, w, al) in enumerate(zip(row, col_widths, aligns)):
            nx = "LMARGIN" if j == n_cols - 1 else "RIGHT"
            ny = "NEXT"    if j == n_cols - 1 else "TOP"
            pdf.cell(w, dh, str(cell_text), align=al, fill=fill,
                     new_x=nx, new_y=ny)

    pdf.set_fill_color(*_WHITE)
    pdf.set_text_color(*INK)
    pdf.set_font("DejaVu", size=font_size)

    if hidden and overflow_note:
        pdf.set_x(_MARGIN)
        pdf.set_font("DejaVu", size=8)
        pdf.set_text_color(*SAGE)
        pdf.cell(_INNER_W, 5, overflow_note.format(n=hidden), align="R",
                 new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(*INK)


def pl_cascade(pdf: HermesPDF, rows: list[tuple]) -> None:
    """P&L-каскад водопадного типа.

    rows = [(label, value_kop, style, pct_str|None), ...]
      или [(label, value_kop, style)]  — без процента.
    Если label is None — разделительная линия (value/style игнорируются).

    style: 'income'   — выручка (INK bold, без знака)
           'deduct'   — вычет   (TERRA, знак '-', отступ)
           'subtotal' — итог уровня (INK bold, SAGE_L фон)
           'note'     — справочно  (SAGE, знак '-', отступ)
    """
    L_W = 115.0
    V_W = float(_INNER_W) - L_W

    for item in rows:
        label = item[0]
        if label is None:
            _hline(pdf, GRID, 0.4)
            pdf.ln(2)
            continue

        value_kop, style = item[1], item[2]
        pct_str = item[3] if len(item) > 3 else None
        h = 7.0 if style == "subtotal" else 5.5

        if style == "subtotal":
            pdf.set_font("DejaVu_B", size=10)
            pdf.set_text_color(*INK)
            pdf.set_fill_color(*SAGE_L)
            pdf.rect(float(_MARGIN), pdf.get_y(), float(_INNER_W), h, style="F")
        elif style == "deduct":
            pdf.set_font("DejaVu", size=9)
            pdf.set_text_color(*TERRA)
        elif style == "note":
            pdf.set_font("DejaVu", size=8.5)
            pdf.set_text_color(*SAGE)
        else:  # income
            pdf.set_font("DejaVu_B", size=9)
            pdf.set_text_color(*INK)

        indent = "  " if style in ("deduct", "note") else ""
        pdf.set_x(_MARGIN)
        pdf.cell(L_W, h, indent + label)

        abs_kop = abs(int(value_kop))
        sign = "−" if style in ("deduct", "note") else ""
        val_str = f"{sign}{abs_kop // 100:,}".replace(",", " ") + " ₽"
        if pct_str:
            val_str += f"  ({pct_str})"

        pdf.cell(V_W, h, val_str, align="R", new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(*INK)

    pdf.ln(3)


def callout(pdf: HermesPDF, text: str, kind: str = "warn") -> None:
    """Плашка с цветной полоской слева. kind: warn=TERRA, info=SAGE, ok=SAGE_L."""
    color = {"warn": TERRA, "info": SAGE, "ok": SAGE_L}.get(kind, TERRA)
    pdf.ln(2)
    x0 = float(_MARGIN)
    y0 = pdf.get_y()
    pdf.set_x(x0 + 4)
    pdf.set_font("DejaVu", size=9)
    pdf.set_text_color(*color)
    pdf.multi_cell(_INNER_W - 4, 5, text, align="L")
    y1 = pdf.get_y()
    pdf.set_fill_color(*color)
    pdf.rect(x0, y0, 2.5, max(y1 - y0, 6), style="F")
    pdf.set_text_color(*INK)
    pdf.ln(2)


def embed_chart(
    pdf: HermesPDF,
    png_bytes: bytes,
    caption: str = "",
    aspect: float = 5.2 / 11,
) -> None:
    """Вставить PNG-график на всю ширину текстового блока."""
    img_w = float(_INNER_W)
    img_h = round(img_w * aspect, 1)
    if pdf.get_y() + img_h + 12 > pdf.page_break_trigger:
        pdf.add_page()
    buf = io.BytesIO(png_bytes)
    pdf.set_x(_MARGIN)
    pdf.image(buf, x=float(_MARGIN), y=pdf.get_y(), w=img_w, h=img_h)
    pdf.set_y(pdf.get_y() + img_h + 1)
    if caption:
        pdf.set_x(_MARGIN)
        pdf.set_font("DejaVu", size=8)
        pdf.set_text_color(*SAGE)
        pdf.cell(_INNER_W, 4, caption, align="C", new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(*INK)
    pdf.ln(3)
