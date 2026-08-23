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
from .report_cashflow import (
    get_cashflow_writeoffs,
    get_operational_expenses,
    get_owner_withdrawals,
)
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


def _qty(value: int | float) -> str:
    """Количество без лишнего нуля после запятой."""
    number = float(value or 0)
    if number.is_integer():
        return f"{int(number):,}".replace(",", " ")
    return f"{number:,.2f}".replace(",", " ").rstrip("0").rstrip(".")


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
    "THEN round(spd.sell_qty * pp.price_kop * "
    "CASE WHEN spd.assortment_id = ANY(%s::text[]) "
    f"THEN {config.SUPPLIER_DISCOUNT_MULTIPLIER}::numeric ELSE 1.0 END) "
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


def _get_losses_by_store(
    conn, d_from: date, d_to: date,
    discount_pids: frozenset | None = None,
    inventory_doc_ids: set[str] | None = None,
) -> dict[str, int]:
    """Обычная порча по закупочной стоимости, без инвентаризации.

    Для товаров ООО «Поставщик» применяет скидку 7% к цене из purchase_price_asof.
    Стоимость i.total_kop (если product_id неизвестен) оставляем без изменений —
    нет информации о поставщике.
    """
    if inventory_doc_ids is None:
        from .report_inventory import inventory_loss_doc_ids as _inventory_ids
        inventory_doc_ids = _inventory_ids(conn, d_from, d_to)
    excluded_docs = sorted(inventory_doc_ids) or ["__none__"]
    disc_list = sorted(discount_pids) if discount_pids else []
    has_disc = bool(disc_list)

    disc_case = (
        " * CASE WHEN i.product_id = ANY(%s::text[]) "
        "THEN 0.93::numeric ELSE 1.0 END"
        if has_disc else ""
    )
    params: list = ([disc_list] if has_disc else []) + [d_from, d_to, excluded_docs]

    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT d.store_name,
                   SUM(CASE
                       WHEN COALESCE(i.total_kop, 0) = 0 THEN 0
                       WHEN i.product_id IS NOT NULL
                            AND pp.price_kop IS NOT NULL AND pp.price_kop > 0
                           THEN round(i.qty * pp.price_kop{disc_case})
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
              AND NOT (d.doc_id = ANY(%s))
            GROUP BY d.store_name
        """, params)
        rows = cur.fetchall()

    result: dict[str, int] = {}
    total = 0
    for sn, kop in rows:
        v = int(kop or 0)
        result[sn] = v
        total += v
    result["__total__"] = total
    return result


def _get_loss_details_by_store(
    conn, d_from: date, d_to: date,
    discount_pids: frozenset | set[str] | None = None,
) -> dict[str, list[tuple]]:
    """Все позиции списаний, сгруппированные по отделу/складу.

    В отличие от P&L-суммы здесь намеренно не исключаются склады
    инвентаризационных корректировок: владелец просит видеть, что конкретно
    списали в каждом отделе. Для ООО «Поставщик» используется та же скидка 7%,
    что и в основной финансовой части отчёта.
    """
    disc_list = sorted(discount_pids or [])
    has_disc = bool(disc_list)
    multiplier = config.SUPPLIER_DISCOUNT_MULTIPLIER
    disc_case = (
        " * CASE WHEN i.product_id = ANY(%s::text[]) "
        f"THEN {multiplier}::numeric ELSE 1.0 END"
        if has_disc else ""
    )
    # disc_case используется дважды: в цене единицы и в сумме строки.
    params: list = ([disc_list, disc_list] if has_disc else []) + [d_from, d_to]

    unit_expr = f"""CASE
        WHEN i.product_id IS NOT NULL
             AND pp.price_kop IS NOT NULL AND pp.price_kop > 0
            THEN round(pp.price_kop{disc_case})
        ELSE i.cost_kop END"""
    total_expr = f"""CASE
        WHEN i.product_id IS NOT NULL
             AND pp.price_kop IS NOT NULL AND pp.price_kop > 0
            THEN round(i.qty * pp.price_kop{disc_case})
        ELSE i.total_kop END"""

    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT d.store_name, d.day, d.moment, d.doc_id,
                   i.product_name, i.qty,
                   COALESCE({unit_expr}, 0) AS unit_kop,
                   COALESCE({total_expr}, 0) AS total_kop
            FROM loss_doc d
            JOIN loss_item i ON i.doc_id = d.doc_id
            LEFT JOIN LATERAL (
                SELECT price_kop FROM purchase_price_asof p
                WHERE i.product_id IS NOT NULL
                  AND p.product_id = i.product_id AND p.priced_from <= d.day
                ORDER BY p.priced_from DESC LIMIT 1
            ) pp ON true
            WHERE d.day BETWEEN %s AND %s
            ORDER BY d.store_name, d.moment, d.doc_id, i.product_name
        """, params)
        rows = cur.fetchall()

    result: dict[str, list[tuple]] = {}
    for store, day, moment, doc_id, product, qty, unit_kop, total_kop in rows:
        result.setdefault(store or "(без отдела)", []).append((
            day, moment, doc_id, product or "(без названия)",
            float(qty or 0), int(unit_kop or 0), int(total_kop or 0),
        ))
    return result


def _wrap_pdf_text(pdf: pk.HermesPDF, text: str, width: float) -> list[str]:
    """Перенос текста по фактической ширине шрифта без обрезания названия."""
    words: list[str] = []
    for raw_word in str(text).split():
        if pdf.get_string_width(raw_word) <= width:
            words.append(raw_word)
            continue
        # Названия часто содержат длинные сочетания через «/» без пробелов.
        # Делим такой фрагмент по символам, иначе он выйдет за правую границу.
        part = ""
        for char in raw_word:
            if part and pdf.get_string_width(part + char) > width:
                words.append(part)
                part = char
            else:
                part += char
        if part:
            words.append(part)
    if not words:
        return [""]
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        candidate = current + " " + word
        if pdf.get_string_width(candidate) <= width:
            current = candidate
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def _append_loss_position_table(
    pdf: pk.HermesPDF, store_name: str, rows: list[tuple],
) -> None:
    """Таблица всех строк списания с переносом длинных названий."""
    headers = ["Дата", "Товар", "Кол-во", "Закупка/ед.", "Сумма"]
    widths = [20.0, 78.0, 20.0, 28.0, 28.0]
    aligns = ["L", "L", "R", "R", "R"]
    line_h = 4.2

    def _header() -> None:
        pdf.set_x(pk._MARGIN)
        pdf.set_font("DejaVu_B", size=7.5)
        pdf.set_text_color(255, 255, 255)
        pdf.set_fill_color(*pk.INK)
        for idx, (header, width, align) in enumerate(zip(headers, widths, aligns)):
            pdf.cell(
                width, 6.5, header, align=align, fill=True,
                new_x="LMARGIN" if idx == len(headers) - 1 else "RIGHT",
                new_y="NEXT" if idx == len(headers) - 1 else "TOP",
            )
        pdf.set_text_color(*pk.INK)

    _header()
    for row_idx, (day, _moment, _doc_id, product, qty, unit_kop, total_kop) in enumerate(rows):
        pdf.set_font("DejaVu", size=7.5)
        product_lines = _wrap_pdf_text(pdf, product, widths[1] - 2)
        row_h = max(5.5, len(product_lines) * line_h + 1.2)
        if pdf.get_y() + row_h > pdf.page_break_trigger:
            pdf.add_page()
            pk.section_header(pdf, f"Списания - {_trunc(store_name, 58)} (продолжение)")
            _header()
        # section_header/_header используют жирный шрифт; каждая строка данных
        # должна начинаться с обычного независимо от разрыва страницы.
        pdf.set_font("DejaVu", size=7.5)

        y0 = pdf.get_y()
        if row_idx % 2 == 0:
            pdf.set_fill_color(*pk.CREAM)
            pdf.rect(float(pk._MARGIN), y0, float(pk._INNER_W), row_h, style="F")

        day_text = day.strftime("%d.%m.%Y") if hasattr(day, "strftime") else str(day)
        values = [
            day_text,
            product_lines,
            _qty(float(qty)),
            _rub(unit_kop) + " ₽",
            _rub(total_kop) + " ₽",
        ]
        x = float(pk._MARGIN)
        pdf.set_text_color(*pk.INK)
        for idx, (value, width, align) in enumerate(zip(values, widths, aligns)):
            pdf.set_xy(x, y0 + 0.7)
            if idx == 1:
                for line_idx, line in enumerate(value):
                    pdf.set_xy(x + 1, y0 + 0.7 + line_idx * line_h)
                    pdf.cell(width - 2, line_h, line, align="L")
            else:
                pdf.cell(width - 1, line_h, str(value), align=align)
            x += width
        pdf.set_y(y0 + row_h)
    pdf.ln(2)


def _append_loss_details_by_store(
    pdf: pk.HermesPDF, details: dict[str, list[tuple]],
    *, inventory: bool = False, reuse_current_page: bool = False,
) -> None:
    """Отдельная страница каждого отдела с позициями одной категории."""
    for detail_index, (store_name, rows) in enumerate(details.items()):
        if detail_index or not reuse_current_page:
            pdf.add_page()
        else:
            pdf.ln(7)
        pk.cover(
            pdf,
            "ИНВЕНТАРИЗАЦИЯ — СПИСАНИЕ"
            if inventory else "ОБЫЧНЫЕ СПИСАНИЯ (ПОРЧА)",
        )
        pdf.set_x(pk._MARGIN)
        pdf.set_font("DejaVu_B", size=11)
        pdf.set_text_color(*pk.INK)
        pdf.multi_cell(pk._INNER_W, 6, store_name, align="L")
        pdf.ln(2)

        documents = len({row[2] for row in rows})
        total_qty = sum(row[4] for row in rows)
        total_kop = sum(row[6] for row in rows)
        pk.kpi_row(pdf, [
            ("Документов", str(documents), ""),
            ("Позиций", str(len(rows)), ""),
            ("Количество", _qty(total_qty), "ед."),
            ("Сумма", _rub(total_kop), "₽"),
        ])
        if inventory:
            pk.callout(
                pdf,
                "Это техническая часть инвентаризации: фактический остаток оказался "
                "меньше учётного. Сумма не считается обычной порчей и не вычитается "
                "из прибыли. Ниже в разделе инвентаризаций она сопоставляется с "
                "оприходованием излишков; итог корректировки = оприходовано − списано.",
                kind="info",
            )
        else:
            pk.callout(
                pdf,
                "Обычная порча. Сумма этого раздела входит в показатель «Списания» "
                "и уменьшает прибыль после списаний.",
                kind="info",
            )
        pk.section_header(
            pdf,
            "Позиции технического списания"
            if inventory else "Все списанные позиции",
        )
        _append_loss_position_table(pdf, store_name, rows)


def _render_income_split(
    pdf: pk.HermesPDF,
    grand_prof: int,
    exp_total: int,
    losses_total: int,
    kola_balls_kop: int,
) -> None:
    """Блок «Разделение прибыли» — каскад от вал.прибыли до ИТОГО Диме/Коле.

    Вал.прибыль → −Расходы → =До списаний → −Списания →
    =Чистая после списаний → −Шары Коли → =Совместная → Дима/Коля.
    Инвариант: ИТОГО Диме + ИТОГО Коле == grand_after.
    """
    pk.section_header(pdf, "Разделение прибыли  ·  Дима и Коля")

    grand_net   = grand_prof - exp_total
    grand_after = grand_net - losses_total
    joint       = grand_after - kola_balls_kop
    dima_share  = joint // 2
    kola_joint  = joint - dima_share
    kola_total  = kola_joint + kola_balls_kop

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
    _row("Валовая прибыль", _v(grand_prof), bold=True)
    _sep(pk.GRID, 0.2)
    _row("  − Операционные расходы", "−" + _v(exp_total), color=pk.TERRA)
    _sep()
    pdf.ln(1)
    _row("= Прибыль до списаний", _v(grand_net), bold=True, bg=pk.SAGE_L, h=7.5)
    pdf.ln(1)
    _sep(pk.GRID, 0.2)
    _row("  − Списания (порча)", "−" + _v(losses_total), color=pk.TERRA)
    _sep()
    pdf.ln(1)
    _row("= Чистая прибыль после списаний", _v(grand_after),
         bold=True, bg=pk.SAGE_L, h=7.5)
    pdf.ln(1)
    _sep()
    _row("  − Шары Коли (Ленина + Воровского)",
         "−" + _v(kola_balls_kop), color=pk.TERRA)
    _sep()
    pdf.ln(1.5)
    _row("= Совместная прибыль", _v(joint), bold=True, bg=pk.SAGE_L, h=7.5)
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
    ok = (dima_share + kola_total == grand_after)
    pdf.set_font("DejaVu", size=7.5)
    pdf.set_text_color(*(pk.SAGE if ok else pk.TERRA))
    pdf.set_x(pk._MARGIN)
    if ok:
        chk = (f"Проверка: {_rub(dima_share)} + {_rub(kola_total)}"
               f" = {_rub(grand_after)} ₽ ✓")
    else:
        chk = (f"⚠ РАСХОЖДЕНИЕ: {_rub(dima_share + kola_total)}"
               f" ≠ {_rub(grand_after)} ₽")
    pdf.cell(pk._INNER_W, 4.5, chk, align="L", new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(*pk.SAGE)
    pdf.set_x(pk._MARGIN)
    pdf.cell(pk._INNER_W, 4,
             "Шары Коли — по выручке (учёт не ведётся). Совместное делится 50/50.",
             align="L", new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(*pk.INK)
    pdf.ln(4)


def _plain_report_text(text: str) -> str:
    """Remove chat-only pictograms while keeping readable PDF punctuation."""
    for marker in (
        "💸", "📍", "📉", "📋", "📅", "📤", "📥", "👥", "🏆",
        "⚠️", "⚠", "✅", "⚙", "🏪", "📊", "🔄", "💰",
        "🔍", "🗑️", "🗑", "✏️", "✏", "🧾",
    ):
        text = text.replace(marker, "")
    return text.replace("\ufe0f", "").strip()


def _append_text_section(pdf: pk.HermesPDF, title: str, content: str) -> None:
    """Append a text report using the established branded sales-PDF layout."""
    pdf.add_page()
    pk.cover(pdf, title)
    for raw in content.splitlines():
        line = _plain_report_text(raw)
        if not line:
            pdf.ln(2)
            continue
        if line.startswith("──"):
            pk.section_header(pdf, line.strip("─ "))
            continue
        if line.startswith("▸") or (line.endswith(":") and len(line) < 72):
            pdf.set_font("DejaVu_B", size=8.5)
        else:
            pdf.set_font("DejaVu", size=8.0)
        pdf.set_text_color(*pk.INK)
        pdf.set_x(pk._MARGIN)
        pdf.multi_cell(pk._INNER_W, 4.3, line, align="L",
                       new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)


def _append_top_clients(
    pdf: pk.HermesPDF, rows: list[tuple], store_name: str,
) -> None:
    """Компактный лист A4 с топ-клиентами выбранного склада."""
    if not rows:
        return
    pdf.add_page()
    pk.cover(pdf, f"ТОП-{len(rows)} КЛИЕНТОВ · БАЗА")
    pdf.set_x(pk._MARGIN)
    pdf.set_font("DejaVu", size=7.2)
    pdf.set_text_color(*pk.SAGE)
    pdf.cell(
        pk._INNER_W, 4, store_name,
        new_x="LMARGIN", new_y="NEXT",
    )
    pdf.ln(2)
    table_rows = []
    for idx, (name, orders, revenue) in enumerate(rows, 1):
        revenue = float(revenue or 0)
        avg = revenue / int(orders or 1)
        table_rows.append([
            str(idx), _trunc(name or "—", 46), str(int(orders or 0)),
            _rub(revenue) + " ₽", _rub(avg) + " ₽",
        ])
    pk.table(
        pdf,
        headers=["#", "Клиент", "Заказов", "Выручка", "Средний чек"],
        rows=table_rows,
        col_widths=[8, 83, 19, 34, 30],
        aligns=["R", "L", "R", "R", "R"],
        font_size=7.2,
        max_rows=40,
    )


def _append_writeoff_clients(pdf: pk.HermesPDF, rows: list[tuple]) -> None:
    """Один лист A4: контрагенты БАЗЫ по статьям «Списание»/«Возврат»."""
    if not rows:
        return
    visible = rows[:34]
    pdf.add_page()
    pk.cover(pdf, "ТОП КЛИЕНТОВ ПО СПИСАНИЯМ · БАЗА")
    pk.callout(
        pdf,
        "Источник: кассовые и банковские расходы БАЗЫ со статьями «Списание» "
        "и «Возврат». "
        "Эти суммы перенесены из операционных расходов в обычные списания и "
        "уменьшают прибыль только один раз. Контрагент показывает, у какого "
        "клиента произошло списание. На листе показаны первые 34 по сумме.",
        kind="info",
    )
    total = sum(int(row[2] or 0) for row in rows)
    pk.kpi_row(pdf, [
        ("Контрагентов", str(len(rows)), ""),
        ("Документов", str(sum(int(row[1] or 0) for row in rows)), ""),
        ("Сумма", _rub(total), "₽"),
    ])
    table_rows = [
        [
            str(index), _trunc(agent or "Контрагент не указан", 55),
            str(int(docs or 0)), _rub(amount) + " ₽", _rub(avg) + " ₽",
        ]
        for index, (agent, docs, amount, avg) in enumerate(visible, 1)
    ]
    pk.table(
        pdf,
        headers=["#", "Контрагент", "Списаний", "Сумма", "Среднее"],
        rows=table_rows,
        col_widths=[8, 84, 20, 32, 30],
        aligns=["R", "L", "R", "R", "R"],
        font_size=7.2,
        max_rows=34,
    )


def _append_churn_clients(pdf: pk.HermesPDF, rows: list[tuple]) -> None:
    """Все клиенты БАЗЫ: без заказа >10 дней, средний чек >=10 000 ₽."""
    pdf.add_page()
    pdf.set_x(pk._MARGIN)
    pk.cover(pdf, "ВОЗМОЖНЫЙ ОТТОК · БАЗА")
    pdf.set_x(pk._MARGIN)
    pdf.set_font("DejaVu", size=7.2)
    pdf.set_text_color(*pk.SAGE)
    pdf.cell(
        pk._INNER_W, 4,
        "Все клиенты БАЗЫ без заказа больше 10 дней · средний чек от 10 000 ₽.",
        new_x="LMARGIN", new_y="NEXT",
    )
    pdf.ln(2)
    if not rows:
        pk.callout(pdf, "Отставших оптовиков не обнаружено.", kind="ok")
        return

    headers = [
        "Клиент", "Последний заказ", "Без заказа", "Заказов",
        "Всего", "Средний чек",
    ]
    widths = [58, 28, 18, 16, 26, 28]
    aligns = ["L", "C", "R", "R", "R", "R"]
    row_h = 4.2

    def _header() -> None:
        pdf.set_x(pk._MARGIN)
        pdf.set_font("DejaVu_B", size=6.5)
        pdf.set_text_color(255, 255, 255)
        pdf.set_fill_color(*pk.INK)
        for idx, (title, width, align) in enumerate(zip(headers, widths, aligns)):
            pdf.cell(
                width, 5.2, title, align=align, fill=True,
                new_x="LMARGIN" if idx == len(headers) - 1 else "RIGHT",
                new_y="NEXT" if idx == len(headers) - 1 else "TOP",
            )
        pdf.set_text_color(*pk.INK)

    _header()
    for idx, (name, last_day, days_since, orders, revenue, avg_check) in enumerate(rows):
        if pdf.get_y() + row_h > pdf.page_break_trigger:
            pdf.add_page()
            _header()
        fill = idx % 2 == 0
        if fill:
            pdf.set_fill_color(*pk.CREAM)
        values = [
            _trunc(name or "—", 31),
            last_day.strftime("%d.%m.%Y") if hasattr(last_day, "strftime") else str(last_day),
            f"{int(days_since)} дн.",
            str(int(orders or 0)),
            _rub(float(revenue or 0)) + " ₽",
            _rub(float(avg_check or 0)) + " ₽",
        ]
        pdf.set_x(pk._MARGIN)
        pdf.set_font("DejaVu", size=6.4)
        pdf.set_text_color(*pk.INK)
        for col, (value, width, align) in enumerate(zip(values, widths, aligns)):
            pdf.cell(
                width, row_h, value, align=align, fill=fill,
                new_x="LMARGIN" if col == len(values) - 1 else "RIGHT",
                new_y="NEXT" if col == len(values) - 1 else "TOP",
            )
    pdf.set_fill_color(255, 255, 255)


def _append_period_changes(
    pdf: pk.HermesPDF, conn, d_from: date, d_to: date,
    store_name: str | None, current: dict,
) -> None:
    """Append a compact current-vs-previous period comparison."""
    from datetime import timedelta

    period_len = (d_to - d_from).days + 1
    prev_to = d_from - timedelta(days=1)
    prev_from = prev_to - timedelta(days=period_len - 1)

    discount_pids = calc.discount_product_ids(conn)
    store_id_f = _store_id_for(conn, store_name)
    pdata = _sales_purchase_data(
        conn, prev_from, prev_to, store_id_f, discount_pids=discount_pids,
    )
    by_store = pdata["by_store"]
    sf = "AND store_name = %s" if store_name else ""
    params = [prev_from, prev_to] + ([store_name] if store_name else [])
    with conn.cursor() as cur_db:
        cur_db.execute(f"""
            SELECT store_id, store_name, channel,
                   COALESCE(SUM(revenue_kop), 0), COALESCE(SUM(checks), 0)
            FROM sales_by_store_day
            WHERE day BETWEEN %s AND %s {sf}
            GROUP BY store_id, store_name, channel
        """, params)
        prev_stores = cur_db.fetchall()
    prev_biz = [r for r in prev_stores if r[2] in config.PROFIT_CHANNELS]
    prev_rev = sum(int(r[3] or 0) for r in prev_biz)
    prev_cost = sum(int(by_store.get(r[0], {}).get("pc", 0)) for r in prev_biz)
    prev_profit = prev_rev - prev_cost
    prev_exp = int(get_operational_expenses(conn, prev_from, prev_to)["total"] or 0)
    prev_loss = int(_get_losses_by_store(
        conn, prev_from, prev_to, discount_pids=discount_pids,
    ).get("__total__", 0))
    prev_loss += int(get_cashflow_writeoffs(
        conn, prev_from, prev_to,
    )["total"] or 0)
    prev_checks = sum(int(r[4] or 0) for r in prev_biz)
    prev = {
        "rev": prev_rev,
        "profit": prev_profit,
        "before": prev_profit - prev_exp,
        "result": prev_profit - prev_exp - prev_loss,
        "loss": prev_loss,
        "op_expenses": prev_exp,
        "checks": prev_checks,
        "avg_check": prev_rev // prev_checks if prev_checks else 0,
    }
    cur = current

    def _delta(value, old) -> str:
        if not old:
            return "—"
        return f"{(value - old) / abs(old) * 100:+.0f}%"

    cur_before = int(cur["before"] or 0)
    prev_before = int(prev["before"] or 0)
    metrics = [
        ("Выручка", int(cur["rev"] or 0), int(prev["rev"] or 0), True),
        ("Валовая прибыль", int(cur["profit"] or 0), int(prev["profit"] or 0), True),
        ("Прибыль до списаний", cur_before, prev_before, True),
        ("Прибыль после списаний", int(cur["result"] or 0), int(prev["result"] or 0), True),
        ("Списания", int(cur["loss"] or 0), int(prev["loss"] or 0), True),
        ("Операционные расходы", int(cur["op_expenses"] or 0), int(prev["op_expenses"] or 0), True),
        ("Чеки", int(cur["checks"] or 0), int(prev["checks"] or 0), False),
        ("Средний чек", int(cur["avg_check"] or 0), int(prev["avg_check"] or 0), True),
    ]

    pdf.add_page()
    pk.cover(pdf, "СРАВНЕНИЕ С ПРЕДЫДУЩИМ ПЕРИОДОМ")
    current_label = (
        d_from.strftime("%d.%m.%Y") if d_from == d_to
        else f"{d_from.strftime('%d.%m.%Y')} - {d_to.strftime('%d.%m.%Y')}"
    )
    previous_label = (
        prev_from.strftime("%d.%m.%Y") if prev_from == prev_to
        else f"{prev_from.strftime('%d.%m.%Y')} - {prev_to.strftime('%d.%m.%Y')}"
    )
    pdf.set_x(pk._MARGIN)
    pdf.set_font("DejaVu", size=9)
    pdf.set_text_color(*pk.INK)


def _period_metrics(conn, d_from: date, d_to: date, store_name: str | None,
                    discount_pids: set[str]) -> dict:
    """Финансовые показатели одного периода той же методикой, что PDF."""
    store_id_f = _store_id_for(conn, store_name)
    pdata = _sales_purchase_data(
        conn, d_from, d_to, store_id_f, discount_pids=discount_pids,
    )
    by_store = pdata["by_store"]
    sf = "AND store_name = %s" if store_name else ""
    params = [d_from, d_to] + ([store_name] if store_name else [])
    with conn.cursor() as cur_db:
        cur_db.execute(f"""
            SELECT store_id, store_name, channel,
                   COALESCE(SUM(revenue_kop), 0), COALESCE(SUM(checks), 0)
            FROM sales_by_store_day
            WHERE day BETWEEN %s AND %s {sf}
            GROUP BY store_id, store_name, channel
        """, params)
        stores = cur_db.fetchall()
    business = [r for r in stores if r[2] in config.PROFIT_CHANNELS]
    revenue = sum(int(r[3] or 0) for r in business)
    purchase_cost = sum(int(by_store.get(r[0], {}).get("pc", 0)) for r in business)
    gross = revenue - purchase_cost
    expenses = int(get_operational_expenses(conn, d_from, d_to)["total"] or 0)
    losses = int(_get_losses_by_store(
        conn, d_from, d_to, discount_pids=discount_pids,
    ).get("__total__", 0))
    losses += int(get_cashflow_writeoffs(conn, d_from, d_to)["total"] or 0)
    checks = sum(int(r[4] or 0) for r in business)
    before_losses = gross - expenses
    return {
        "rev": revenue,
        "profit": gross,
        "before": before_losses,
        "result": before_losses - losses,
        "loss": losses,
        "op_expenses": expenses,
        "checks": checks,
        "avg_check": revenue // checks if checks else 0,
    }


def _append_period_comparisons(
    pdf: pk.HermesPDF, conn, report_from: date, report_to: date,
    store_name: str | None,
) -> None:
    """Сравнения: день, неделя, месяц и год к дате конца отчёта."""
    import calendar
    from datetime import timedelta

    anchor = report_to
    month_current_from = anchor.replace(day=1)
    previous_month_last = month_current_from - timedelta(days=1)
    previous_month_from = previous_month_last.replace(day=1)
    previous_month_day = min(anchor.day, previous_month_last.day)
    previous_month_to = previous_month_from.replace(day=previous_month_day)

    def _year_back(value: date) -> date:
        day = min(value.day, calendar.monthrange(value.year - 1, value.month)[1])
        return value.replace(year=value.year - 1, day=day)

    comparisons = [
        ("ДЕНЬ", anchor, anchor,
         anchor - timedelta(days=1), anchor - timedelta(days=1)),
        ("НЕДЕЛЯ", anchor - timedelta(days=6), anchor,
         anchor - timedelta(days=13), anchor - timedelta(days=7)),
        ("МЕСЯЦ", month_current_from, anchor,
         previous_month_from, previous_month_to),
        ("ГОД К ГОДУ", report_from, report_to,
         _year_back(report_from), _year_back(report_to)),
    ]
    discount_pids = calc.discount_product_ids(conn)

    def _label(a: date, b: date) -> str:
        return a.strftime("%d.%m.%Y") if a == b else f"{a:%d.%m.%Y} - {b:%d.%m.%Y}"

    def _delta(value: int, old: int) -> str:
        return "—" if not old else f"{(value - old) / abs(old) * 100:+.0f}%"

    for horizon, cur_from, cur_to, prev_from, prev_to in comparisons:
        cur = _period_metrics(conn, cur_from, cur_to, store_name, discount_pids)
        prev = _period_metrics(conn, prev_from, prev_to, store_name, discount_pids)
        cur_label = _label(cur_from, cur_to)
        prev_label = _label(prev_from, prev_to)

        pdf.add_page()
        pk.cover(pdf, f"СРАВНЕНИЕ  ·  {horizon}")
        pdf.set_x(pk._MARGIN)
        pdf.set_font("DejaVu", size=9)
        pdf.set_text_color(*pk.INK)
        pdf.multi_cell(
            pk._INNER_W, 5,
            f"Текущий: {cur_label}\nПредыдущий: {prev_label}\n"
            f"Расчёт привязан к дате окончания отчёта: {anchor:%d.%m.%Y}.",
            align="L", new_x="LMARGIN", new_y="NEXT",
        )
        pdf.ln(3)
        pk.kpi_row(pdf, [
            ("Выручка", _rub(cur["rev"]), "₽", _delta(cur["rev"], prev["rev"])),
            ("Вал. прибыль", _rub(cur["profit"]), "₽", _delta(cur["profit"], prev["profit"])),
            ("До списаний", _rub(cur["before"]), "₽", _delta(cur["before"], prev["before"])),
            ("После списаний", _rub(cur["result"]), "₽", _delta(cur["result"], prev["result"])),
        ])
        pdf.ln(4)
        pk.section_header(pdf, "Сравнение показателей")
        metric_defs = [
            ("Выручка", "rev", True),
            ("Валовая прибыль", "profit", True),
            ("Прибыль до списаний", "before", True),
            ("Прибыль после списаний", "result", True),
            ("Списания", "loss", True),
            ("Операционные расходы", "op_expenses", True),
            ("Чеки", "checks", False),
            ("Средний чек", "avg_check", True),
        ]
        table_rows = []
        for title, key, money in metric_defs:
            value, old = int(cur[key] or 0), int(prev[key] or 0)
            value_s = _rub(value) + " ₽" if money else f"{value:,}".replace(",", " ")
            old_s = _rub(old) + " ₽" if money else f"{old:,}".replace(",", " ")
            diff = value - old
            diff_s = (_rub(diff) + " ₽" if money
                      else f"{diff:+,}".replace(",", " "))
            table_rows.append([title, value_s, old_s, diff_s, _delta(value, old)])
        pk.table(
            pdf,
            headers=["Показатель", "Текущий", "Предыдущий", "Разница", "Изм., %"],
            rows=table_rows,
            col_widths=[54, 36, 36, 28, 20],
            aligns=["L", "R", "R", "R", "R"],
            font_size=8.0,
        )
        pdf.ln(3)
        pdf.set_x(pk._MARGIN)
        pdf.set_font("DejaVu", size=7.5)
        pdf.set_text_color(*pk.SAGE)
        pdf.multi_cell(
            pk._INNER_W, 4,
            "Разница = текущий - предыдущий. Плюс означает рост, минус - снижение.",
            align="L", new_x="LMARGIN", new_y="NEXT",
        )
        pdf.set_text_color(*pk.INK)
def build_sales_pdf(
    conn, d_from: date, d_to: date, store_name: str | None = None,
    include_management_sections: bool = False,
    client=None,
) -> bytes:
    """PDF-отчёт по продажам с тремя уровнями прибыли. Возвращает bytes."""
    days = (d_to - d_from).days + 1
    period_str = (
        d_from.strftime("%d.%m.%Y") if d_from == d_to
        else f"{d_from.strftime('%d.%m.%Y')} – {d_to.strftime('%d.%m.%Y')}"
    )
    store_label = store_name or "Все склады"

    # -- Данные ----------------------------------------------------------------
    discount_pids = calc.discount_product_ids(conn)
    store_id_f = _store_id_for(conn, store_name)
    pdata = _sales_purchase_data(conn, d_from, d_to, store_id_f,
                                 discount_pids=discount_pids)
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
    from .report_inventory import inventory_loss_doc_ids
    inventory_loss_ids = inventory_loss_doc_ids(conn, d_from, d_to)
    losses = _get_losses_by_store(
        conn, d_from, d_to,
        discount_pids=discount_pids,
        inventory_doc_ids=inventory_loss_ids,
    )
    cashflow_writeoffs = get_cashflow_writeoffs(conn, d_from, d_to)
    base_writeoff_store = getattr(
        config, "BASE_CASHFLOW_WRITEOFF_STORE", "База Воровского 107/1",
    )
    known_store_names = {str(row[1]) for row in stores}
    for writeoff_project, writeoff_kop in cashflow_writeoffs["by_project"].items():
        target_store = (
            writeoff_project
            if writeoff_project in known_store_names
            else base_writeoff_store
        )
        losses[target_store] = losses.get(target_store, 0) + int(writeoff_kop or 0)
    losses["__total__"] = losses.get("__total__", 0) + int(
        cashflow_writeoffs["total"] or 0
    )
    losses_total = losses.get("__total__", 0)
    all_loss_details = (
        _get_loss_details_by_store(
            conn, d_from, d_to, discount_pids=discount_pids,
        )
        if include_management_sections else {}
    )
    ordinary_loss_details: dict[str, list[tuple]] = {}
    inventory_loss_details: dict[str, list[tuple]] = {}
    for detail_store, detail_rows in all_loss_details.items():
        for row in detail_rows:
            target = (
                inventory_loss_details
                if row[2] in inventory_loss_ids
                else ordinary_loss_details
            )
            target.setdefault(detail_store, []).append(row)
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

    _biz = [r for r in stores if r[2] in config.PROFIT_CHANNELS]
    grand_rev      = sum(r[3] for r in _biz)
    grand_cost_raw = sum(by_store.get(r[0], {}).get("pc_raw", 0) for r in _biz)
    discount_total = sum(by_store.get(r[0], {}).get("discount", 0) for r in _biz)
    grand_cost     = sum(by_store.get(r[0], {}).get("pc", 0) for r in _biz)
    grand_prof     = grand_rev - grand_cost
    grand_net      = grand_prof - exp["total"]
    grand_after    = grand_net - losses_total

    # -- Отдельный топ-40 по каждому складу -----------------------------------
    top_by_store: list[tuple[str, list[tuple]]] = []
    with conn.cursor() as cur:
        for sid, sn, _channel, rev_s, _checks in _biz:
            if not rev_s:
                continue
            cur.execute(f"""
                SELECT spd.product_name, SUM(spd.sell_qty), SUM(spd.revenue_kop),
                       SUM({_PCOST}),
                       {_UNCOV}
                FROM sales_by_product_day spd {_ASOF}
                WHERE spd.day BETWEEN %s AND %s
                  AND spd.store_id = %s
                  AND spd.assortment_id IN (
                      SELECT DISTINCT product_id FROM stock_snapshot
                      WHERE folder_path LIKE %s
                  )
                GROUP BY spd.product_name
                ORDER BY SUM(spd.revenue_kop) DESC LIMIT 40
            """, [sorted(discount_pids), d_from, d_to, sid, "Ассортимент/%"])
            store_top = cur.fetchall()
            if store_top:
                top_by_store.append((sn, store_top))

    # -- Строим PDF ------------------------------------------------------------
    pdf = pk.HermesPDF(
        section_title="Продажи",
        period=period_str,
        store=store_label,
    )
    pdf.add_page()

    pk.cover(pdf, f"Продажи ({days} дн.)")

    # KPI: 4 плашки — выручка / вал.прибыль / до списаний / чистая после списаний
    pk.kpi_row(pdf, [
        ("Выручка",        _rub(grand_rev),   "₽"),
        ("Вал.прибыль",    _rub(grand_prof),  "₽", _pct(grand_prof, grand_rev) + "%"),
        ("До списаний",    _rub(grand_net),   "₽", _pct(grand_net, grand_rev)  + "%"),
        ("Чист. / спис.",  _rub(grand_after), "₽", _pct(grand_after, grand_rev) + "%"),
    ])

    # -- Каскад P&L ------------------------------------------------------------
    pk.section_header(pdf, "Отчёт о прибылях и убытках")
    cascade_rows: list = [
        ("Выручка", grand_rev, "income"),
        ("Закупочная стоимость до скидки", grand_cost_raw, "deduct"),
    ]
    if discount_total > 0:
        cascade_rows.append(
            ("Скидка ООО «Поставщик» 7%", discount_total, "credit")
        )
    cascade_rows += [
        (None, None, None),
        ("= ВАЛОВАЯ ПРИБЫЛЬ", grand_prof, "subtotal",
         _pct(grand_prof, grand_rev) + "%"),
        ("Операционные расходы", exp["total"], "deduct"),
        (None, None, None),
        ("= ПРИБЫЛЬ ДО СПИСАНИЙ", grand_net, "subtotal",
         _pct(grand_net, grand_rev) + "%"),
        ("Списания (порча)", losses_total, "deduct"),
        (None, None, None),
        ("= ЧИСТАЯ ПРИБЫЛЬ ПОСЛЕ СПИСАНИЙ", grand_after, "subtotal",
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
        f"У остальных {est_pct}% закупка = выручка × 0.6. "
        "До списаний = валовая − операционные расходы."
    )
    if discount_total > 0:
        cov_note += (
            f" Скидка ООО «Поставщик» 7% учтена в закупке (−{_rub(discount_total)} ₽)."
        )
    pdf.multi_cell(pk._INNER_W, 4, cov_note, align="L")
    pdf.set_text_color(*pk.INK)
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

    # Полная цепочка: выручка -> валовая -> до списаний -> после списаний.
    hdrs = ["Склад / Канал", "Выручка", "Вал. приб.", "До спис.", "После спис."]
    cws  = [58, 28, 30, 29, 29]
    alns = ["L", "R", "R", "R", "R"]

    mixed = set(config.MIXED_CHANNEL_STORES or [])
    chan_rows: list[list[str]] = []
    chan_styles: list[str | None] = []
    mixed_in_table: list[str] = []

    for channel in config.PROFIT_CHANNELS:
        chan = [r for r in stores if r[2] == channel]
        c_rev = sum(r[3] for r in chan)
        if c_rev == 0:
            continue
        c_gross = sum(_gross(r[0], r[3]) for r in chan)
        c_net   = sum(_net(r[0], r[1], r[3]) for r in chan)
        c_after = sum(_net(r[0], r[1], r[3]) - losses.get(r[1], 0) for r in chan)

        for sid, sn, _, rev_s, _chk in chan:
            if rev_s == 0:
                continue
            gp  = _gross(sid, rev_s)
            np_ = _net(sid, sn, rev_s)
            ap_ = np_ - losses.get(sn, 0)
            marker = " †" if sn in mixed else ""
            if sn in mixed:
                mixed_in_table.append(sn)
            sn_short = _trunc(sn, 22)
            chan_rows.append([
                f"{channel.upper()}  {sn_short}{marker}",
                _rub(rev_s) + " ₽",
                _rub_pct(gp, rev_s),
                _rub_pct(np_, rev_s),
                _rub_pct(ap_, rev_s),
            ])
            chan_styles.append(None)
            ball_rev = balls_by_store.get(sn, 0)
            if ball_rev > 0 and not store_name:
                ball_lbl = ("в т.ч. шары (Коля)"
                            if sn in _KOLA_BALL_STORES
                            else "в т.ч. шары (совместное)")
                chan_rows.append([f"  {ball_lbl}", _rub(ball_rev) + " ₽", "", "", ""])
                chan_styles.append("detail")

        # Подытог канала полезен, только когда он объединяет несколько складов.
        # Для единственной строки «ОПТ База» он полностью дублировал цифры.
        active_channel_rows = [r for r in chan if r[3] != 0]
        if len(active_channel_rows) > 1:
            chan_rows.append([
                f"  Итого {channel}",
                _rub(c_rev)   + " ₽",
                _rub_pct(c_gross, c_rev),
                _rub_pct(c_net, c_rev),
                _rub_pct(c_after, c_rev),
            ])
            chan_styles.append(None)

    chan_rows.append([
        "ИТОГО",
        _rub(grand_rev)  + " ₽",
        _rub_pct(grand_prof, grand_rev),
        _rub_pct(grand_net, grand_rev),
        _rub_pct(grand_after, grand_rev),
    ])
    chan_styles.append(None)

    pk.table(pdf, headers=hdrs, rows=chan_rows, col_widths=cws, aligns=alns,
             font_size=7.6, row_styles=chan_styles)

    # Сноски под таблицей
    pdf.set_x(pk._MARGIN)
    pdf.set_font("DejaVu", size=7.5)
    pdf.set_text_color(*pk.SAGE)
    footnotes = [
        "«До списаний»: прямые расходы склада + доля общих расходов пропорционально выручке — оценка.",
        "«После списаний» = прибыль до списаний - списания по точке.",
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
            grand_prof=grand_prof,
            exp_total=exp["total"],
            losses_total=losses_total,
            kola_balls_kop=kola_balls_kop,
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
        # Заголовок и таблица должны начинаться на одной странице.
        # Заголовок + шапка + 4 склада + итог занимают около 55 мм. Не оставляем
        # одну итоговую строку сиротой на следующей почти пустой странице.
        if pdf.get_y() + 58 > pdf.page_break_trigger:
            pdf.add_page()
        pk.section_header(pdf, "Обычные списания (порча) и прибыль по складам")
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
            headers=["Склад", "Порча", "До порчи", "После порчи"],
            rows=loss_tbl_rows,
            col_widths=[76, 28, 35, 35],
            aligns=["L", "R", "R", "R"],
            font_size=7.8,
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

    # Полная детализация нужна в управленческом «Отчёте за период»:
    # отдельный лист каждого отдела, без лимита строк и без исключения БАЗЫ.
    if ordinary_loss_details:
        _append_loss_details_by_store(
            pdf, ordinary_loss_details, reuse_current_page=True,
        )
    _append_writeoff_clients(pdf, cashflow_writeoffs["by_agent"])
    if inventory_loss_details:
        _append_loss_details_by_store(
            pdf, inventory_loss_details, inventory=True,
        )

    # -- Топ товаров: отдельный лист A4 для каждого склада ---------------------
    for top_store_name, top_rev in top_by_store:
        pdf.add_page()
        pdf.set_x(pk._MARGIN)
        # Названия складов длинные. Разделяем заголовок и склад на две строки,
        # чтобы текст не уезжал за левое поле на A4.
        pk.cover(pdf, f"ТОП-{len(top_rev)} ТОВАРОВ")
        pdf.set_x(pk._MARGIN)
        pdf.set_font("DejaVu", size=7.2)
        pdf.set_text_color(*pk.SAGE)
        pdf.cell(
            pk._INNER_W, 4, top_store_name,
            new_x="LMARGIN", new_y="NEXT",
        )
        pdf.ln(2)
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
            max_rows=40,
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

    if include_management_sections:
        from .report_cashflow import build_expenses_report
        from .report_move import build_move_report
        from .report_clients import (
            build_clients_report, get_churn_clients, get_top_clients,
        )
        from .report_audit import build_audit_report
        from .report_inventory import build_inventory_report

        _append_text_section(
            pdf, "Расходы",
            build_expenses_report(conn, d_from, d_to, store_name, max_items=12),
        )
        _append_text_section(
            pdf, "Перемещения",
            build_move_report(conn, d_from, d_to, store_name, max_docs=10),
        )
        _append_text_section(
            pdf, "Клиенты",
            build_clients_report(
                conn, d_from, d_to, store_name,
                include_top=False, include_churn=False,
            ),
        )
        base_store = "База Воровского 107/1"
        _append_top_clients(
            pdf,
            get_top_clients(conn, d_from, d_to, base_store, limit=40),
            base_store,
        )
        _append_churn_clients(
            pdf,
            get_churn_clients(
                conn, limit=None, store_name=base_store, inactive_days=10,
                min_avg_check_kop=1_000_000,
            ),
        )
        _append_text_section(
            pdf, "Инвентаризации по точкам",
            build_inventory_report(conn, d_from, d_to, client=client),
        )
        _append_text_section(
            pdf, "Изменения заказов, приёмок и платежей",
            build_audit_report(client, d_from, d_to),
        )
        current_metrics = {
            "rev": grand_rev,
            "profit": grand_prof,
            "before": grand_net,
            "result": grand_after,
            "loss": losses_total,
            "op_expenses": int(exp["total"] or 0),
            "checks": sum(int(r[4] or 0) for r in _biz),
        }
        current_metrics["avg_check"] = (
            grand_rev // current_metrics["checks"]
            if current_metrics["checks"] else 0
        )
        _append_period_comparisons(pdf, conn, d_from, d_to, store_name)

    return bytes(pdf.output())
