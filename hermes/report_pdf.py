"""Генерация PDF-отчёта по всем секциям за выбранный период и склад."""
from __future__ import annotations

import re
from datetime import date

_FONT      = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
_FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
_MARGIN  = 12   # мм (отступ слева, справа, сверху)
_LINE_H  = 4.8  # мм высота строки

# Убираем emoji и символы, которых нет в DejaVu Sans
_RE_STRIP = re.compile(
    "[\U0001F000-\U0001FFFF"  # emoji supplemental planes
    "\U00002600-\U000026FF"   # misc symbols ☀ ⚡ etc.
    "\U00002700-\U000027BF"   # dingbats ✀ ✅ etc.
    "\U0000FE00-\U0000FEFF"   # variation selectors
    "]",
    flags=re.UNICODE,
)


def _clean(text: str) -> str:
    """Убрать emoji, которых нет в DejaVu Sans."""
    return _RE_STRIP.sub("", text)


def build_pdf(
    conn, d_from: date, d_to: date, store_name: str | None = None
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

    class _PDF(FPDF):
        def header(self):
            self.set_x(self.l_margin)
            self.set_font("DejaVu_B", size=8)
            self.set_text_color(140, 140, 140)
            self.cell(
                self.w - self.l_margin - self.r_margin,
                5,
                f"Hermes | {period_str} | {store_label}",
                align="R",
                new_x="LMARGIN",
                new_y="NEXT",
            )
            self.set_text_color(0, 0, 0)
            self.ln(1)

        def footer(self):
            self.set_y(-12)
            self.set_x(self.l_margin)
            self.set_font("DejaVu", size=8)
            self.set_text_color(140, 140, 140)
            self.cell(
                self.w - self.l_margin - self.r_margin,
                6,
                f"Стр. {self.page_no()}",
                align="C",
            )
            self.set_text_color(0, 0, 0)

    pdf = _PDF(orientation="P", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=16)
    pdf.add_font("DejaVu",   style="", fname=_FONT)
    pdf.add_font("DejaVu_B", style="", fname=_FONT_BOLD)
    pdf.set_margins(_MARGIN, _MARGIN, _MARGIN)

    # Эффективная ширина текстовой области
    eff_w = 210 - _MARGIN - _MARGIN  # 186 мм

    # ── Титульная страница ──────────────────────────────────────────────────────
    pdf.add_page()
    pdf.ln(18)
    pdf.set_x(_MARGIN)
    pdf.set_font("DejaVu_B", size=15)
    pdf.cell(eff_w, 12, "ОТЧЁТ", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.set_x(_MARGIN)
    pdf.cell(eff_w, 12, "Цветочная База Дубравиных", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)
    pdf.set_font("DejaVu", size=11)
    pdf.set_x(_MARGIN)
    pdf.cell(eff_w, 9, f"Период: {period_str}", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.set_x(_MARGIN)
    pdf.cell(eff_w, 9, f"Склад: {store_label}", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(8)
    pdf.set_font("DejaVu", size=9)
    pdf.set_text_color(100, 100, 100)
    for s in [
        "1. Продажи   — выручка, прибыль, топ позиций",
        "2. Остатки   — СРЕЗКА без резерва на дату",
        "3. Залежалые — СРЕЗКА без движения",
        "4. Списания  — документы с позициями",
        "5. Расходы   — движение денег",
    ]:
        pdf.set_x(_MARGIN)
        pdf.cell(eff_w, 7, s, align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(0, 0, 0)

    # ── Вспомогательная функция секции ─────────────────────────────────────────
    def _section(title: str, content: str) -> None:
        pdf.add_page()
        pdf.set_x(_MARGIN)
        pdf.set_font("DejaVu_B", size=12)
        pdf.cell(eff_w, 9, title, new_x="LMARGIN", new_y="NEXT")
        # Горизонтальная черта под заголовком через set_draw_color + rect
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
            # Дополнительно удаляем непечатаемые управляющие символы
            cline = "".join(ch for ch in cline if ch >= " " or ch == "\n")
            if not cline.strip():
                pdf.ln(2)
                continue
            # Обрезаем очень длинные строки без пробелов (название без пробела > 60 chars)
            words = cline.split()
            cline = " ".join(w if len(w) <= 60 else w[:60] + "…" for w in words)
            # Всегда сбрасываем x в левый отступ перед multi_cell
            pdf.set_x(_MARGIN)
            stripped = cline.lstrip()
            is_hdr = stripped.startswith("--") or stripped.startswith("==")
            try:
                if is_hdr:
                    pdf.set_font("DejaVu_B", size=8)
                    pdf.multi_cell(
                        eff_w, _LINE_H, cline,
                        new_x="LMARGIN", new_y="NEXT",
                    )
                    pdf.set_font("DejaVu", size=8)
                else:
                    pdf.multi_cell(
                        eff_w, _LINE_H, cline,
                        new_x="LMARGIN", new_y="NEXT",
                    )
            except Exception:
                # Сбрасываем шрифт и отступ; пробуем усечённую строку
                pdf.set_font("DejaVu", size=8)
                pdf.set_x(_MARGIN)
                short = cline[:80] if len(cline) > 80 else cline
                try:
                    pdf.multi_cell(
                        eff_w, _LINE_H, short,
                        new_x="LMARGIN", new_y="NEXT",
                    )
                except Exception:
                    pass  # пропускаем строку которую совсем нельзя нарисовать

    # ── Секция 1: Продажи ──────────────────────────────────────────────────────
    from .report_sales import build_sales_analytics
    _section("1. ПРОДАЖИ", build_sales_analytics(conn, d_from, d_to, store_name))

    # ── Секция 2: Остатки СРЕЗКА (на последний день периода) ──────────────────
    from .report_stock import build_stock_by_qty
    _section("2. ОСТАТКИ (СРЕЗКА)", build_stock_by_qty(conn, d_to, store_name, "СРЕЗКА"))

    # ── Секция 3: Залежалые СРЕЗКА ────────────────────────────────────────────
    from .report_stock import build_stock_report
    _section("3. ЗАЛЕЖАЛЫЕ (СРЕЗКА)", build_stock_report(conn, d_to, store_name, "СРЕЗКА"))

    # ── Секция 4: Списания ─────────────────────────────────────────────────────
    from .report_loss import build_loss_report
    _section("4. СПИСАНИЯ", build_loss_report(conn, d_from, d_to, store_name))

    # ── Секция 5: Расходы ──────────────────────────────────────────────────────
    from .report_cashflow import build_expenses_report
    _section("5. РАСХОДЫ", build_expenses_report(conn, d_from, d_to, store_name))

    return bytes(pdf.output())
