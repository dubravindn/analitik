"""Отчёт «ДДС — Движение денежных средств»: приход/расход за период."""
from __future__ import annotations

from datetime import date


def _rub(kop: float) -> str:
    return f"{kop / 100:,.0f}".replace(",", " ")


_DOC_TYPE_LABEL = {
    "cashin":      "Касса (приход)",
    "cashout":     "Касса (расход)",
    "paymentin":   "Банк (приход)",
    "paymentout":  "Банк (расход)",
}


def build_cashflow_report(conn, d_from: date, d_to: date) -> str:
    period_str = (
        d_from.strftime("%d.%m.%Y")
        if d_from == d_to
        else f"{d_from.strftime('%d.%m.%Y')} – {d_to.strftime('%d.%m.%Y')}"
    )

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT direction, doc_type,
                   COUNT(*) AS docs,
                   SUM(amount_kop) AS kop
            FROM cashflow_event
            WHERE day BETWEEN %s AND %s
            GROUP BY direction, doc_type
            ORDER BY direction DESC, doc_type
            """,
            (d_from, d_to),
        )
        rows = cur.fetchall()

    lines: list[str] = []
    lines.append(f"💰 ДДС за {period_str}")
    lines.append("")

    if not rows:
        lines.append("Данных о движении денег за этот период нет.")
        return "\n".join(lines)

    in_total = sum(r[3] for r in rows if r[0] == "in")
    out_total = sum(r[3] for r in rows if r[0] == "out")
    balance = in_total - out_total

    lines.append(f"📈 Приход: {_rub(in_total)} ₽")
    for _, doc_type, docs, kop in [r for r in rows if r[0] == "in"]:
        lines.append(f"   {_DOC_TYPE_LABEL.get(doc_type, doc_type)}: {docs} опер. · {_rub(kop)} ₽")

    lines.append("")
    lines.append(f"📉 Расход: {_rub(out_total)} ₽")
    for _, doc_type, docs, kop in [r for r in rows if r[0] == "out"]:
        lines.append(f"   {_DOC_TYPE_LABEL.get(doc_type, doc_type)}: {docs} опер. · {_rub(kop)} ₽")

    lines.append("")
    sign = "+" if balance >= 0 else "−"
    lines.append(
        f"{'✅' if balance >= 0 else '🔴'} Баланс: {sign}{_rub(abs(balance))} ₽"
    )

    # Топ-5 контрагентов по притоку
    lines.append("")
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COALESCE(agent_name, 'Не указан'), SUM(amount_kop) AS kop
            FROM cashflow_event
            WHERE day BETWEEN %s AND %s AND direction = 'in'
              AND agent_name IS NOT NULL AND agent_name != ''
            GROUP BY agent_name
            ORDER BY SUM(amount_kop) DESC
            LIMIT 5
            """,
            (d_from, d_to),
        )
        top_in = cur.fetchall()

    if top_in:
        lines.append("🏆 Топ плательщиков:")
        for name, kop in top_in:
            lines.append(f"  {name}: {_rub(kop)} ₽")

    # Топ-5 по оттоку
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COALESCE(agent_name, 'Не указан'), SUM(amount_kop) AS kop
            FROM cashflow_event
            WHERE day BETWEEN %s AND %s AND direction = 'out'
              AND agent_name IS NOT NULL AND agent_name != ''
            GROUP BY agent_name
            ORDER BY SUM(amount_kop) DESC
            LIMIT 5
            """,
            (d_from, d_to),
        )
        top_out = cur.fetchall()

    if top_out:
        lines.append("")
        lines.append("💸 Топ получателей:")
        for name, kop in top_out:
            lines.append(f"  {name}: {_rub(kop)} ₽")

    return "\n".join(lines)


def build_expenses_report(conn, d_from: date, d_to: date, store_name: str | None = None) -> str:
    """Расходы: только исходящие платежи (cashout + paymentout) со статьёй расходов."""
    period_str = (
        d_from.strftime("%d.%m.%Y") if d_from == d_to
        else f"{d_from.strftime('%d.%m.%Y')} – {d_to.strftime('%d.%m.%Y')}"
    )
    lines: list[str] = []
    lines.append(f"💸 Расходы {period_str}")
    lines.append("(кассовые и банковские исходящие документы)")
    lines.append("")

    if store_name:
        _proj_filter = "AND project_name = %s"
        _extra: tuple = (store_name,)
    else:
        _proj_filter = "AND project_name IS NOT NULL AND project_name != ''"
        _extra = ()

    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT SUM(amount_kop), COUNT(*)
            FROM cashflow_event
            WHERE day BETWEEN %s AND %s AND direction = 'out'
            {_proj_filter}
        """, (d_from, d_to) + _extra)
        row = cur.fetchone()
        out_kop = float(row[0] or 0)
        out_cnt = int(row[1] or 0)

    if out_cnt == 0:
        lines.append("Исходящих платежей за этот период нет.")
        return "\n".join(lines)

    lines.append(f"📉 Итого расходов: {_rub(out_kop)} ₽  ({out_cnt} документов)")
    lines.append("")

    # Разбивка по статьям расходов
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT COALESCE(NULLIF(expense_item_name, ''), 'Без статьи'),
                   SUM(amount_kop), COUNT(*)
            FROM cashflow_event
            WHERE day BETWEEN %s AND %s AND direction = 'out'
            {_proj_filter}
            GROUP BY 1
            ORDER BY 2 DESC
        """, (d_from, d_to) + _extra)
        by_item = cur.fetchall()

    if len(by_item) > 1:
        lines.append("── По статьям расходов ──")
        for item_name, kop, cnt in by_item:
            lines.append(f"  {item_name}: {_rub(float(kop))} ₽ ({cnt} опер.)")
        lines.append("")

    # Разбивка по проектам (склад/подразделение)
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT project_name,
                   SUM(amount_kop), COUNT(*)
            FROM cashflow_event
            WHERE day BETWEEN %s AND %s AND direction = 'out'
            {_proj_filter}
            GROUP BY 1
            ORDER BY 2 DESC
        """, (d_from, d_to) + _extra)
        by_project = cur.fetchall()

    if len(by_project) > 1:
        lines.append("── По проектам (складам) ──")
        for proj_name, kop, cnt in by_project:
            lines.append(f"  📍 {proj_name}: {_rub(float(kop))} ₽ ({cnt} опер.)")
        lines.append("")

    # Каждый документ
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT moment, doc_type, agent_name, description,
                   expense_item_name, project_name, amount_kop
            FROM cashflow_event
            WHERE day BETWEEN %s AND %s AND direction = 'out'
            {_proj_filter}
            ORDER BY moment
        """, (d_from, d_to) + _extra)
        out_docs = cur.fetchall()

    lines.append(f"── 📋 Документы ({len(out_docs)}) ──")
    for moment, doc_type, agent, desc, expense_item, project, amount in out_docs:
        dt = (moment.strftime("%d.%m %H:%M")
              if hasattr(moment, "strftime") else str(moment)[:16])
        ch = "Касса" if doc_type == "cashout" else "Банк"
        line = f"  {dt} [{ch}] {_rub(float(amount))} ₽"
        if agent:
            line += f"  → {agent}"
        details = []
        if expense_item:
            details.append(f"Статья: {expense_item}")
        if project:
            details.append(f"Проект: {project}")
        if desc:
            details.append(desc)
        if details:
            line += "\n     " + " | ".join(details)
        lines.append(line)

    return "\n".join(lines)
