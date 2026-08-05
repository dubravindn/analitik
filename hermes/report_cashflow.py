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


def build_expenses_report(conn, d_from: date, d_to: date) -> str:
    """Подробный отчёт: каждый платёжный документ за период."""
    period_str = (
        d_from.strftime("%d.%m.%Y") if d_from == d_to
        else f"{d_from.strftime('%d.%m.%Y')} – {d_to.strftime('%d.%m.%Y')}"
    )
    lines: list[str] = []
    lines.append(f"💸 Расходы и платежи {period_str}")
    lines.append("(кассовые документы не привязаны к складу)")
    lines.append("")

    with conn.cursor() as cur:
        cur.execute("""
            SELECT direction, SUM(amount_kop), COUNT(*)
            FROM cashflow_event
            WHERE day BETWEEN %s AND %s
            GROUP BY direction
        """, (d_from, d_to))
        totals = {row[0]: (float(row[1]), int(row[2])) for row in cur.fetchall()}

    in_kop,  in_cnt  = totals.get("in",  (0.0, 0))
    out_kop, out_cnt = totals.get("out", (0.0, 0))
    bal = in_kop - out_kop
    sign = "+" if bal >= 0 else "-"

    if in_cnt + out_cnt == 0:
        lines.append("Данных о платежах за этот период нет.")
        return "\n".join(lines)

    lines.append(f"📈 Приход:  {_rub(in_kop)} ₽  ({in_cnt} опер.)")
    lines.append(f"📉 Расход:  {_rub(out_kop)} ₽  ({out_cnt} опер.)")
    lines.append(f"{'✅' if bal >= 0 else '🔴'} Баланс: {sign}{_rub(abs(bal))} ₽")
    lines.append("")

    # Исходящие (cashout + paymentout)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT moment, doc_type, agent_name, description, amount_kop
            FROM cashflow_event
            WHERE day BETWEEN %s AND %s AND direction = 'out'
            ORDER BY moment
        """, (d_from, d_to))
        out_docs = cur.fetchall()

    if out_docs:
        lines.append(f"── 📉 Исходящие платежи ({len(out_docs)}) ──")
        for moment, doc_type, agent, desc, amount in out_docs:
            dt = (moment.strftime("%d.%m %H:%M")
                  if hasattr(moment, "strftime") else str(moment)[:16])
            ch = "Касса" if doc_type == "cashout" else "Банк"
            row = f"  {dt} [{ch}] {_rub(float(amount))} ₽"
            if agent:
                row += f"  → {agent}"
            if desc:
                row += f"\n     {desc}"
            lines.append(row)
        lines.append("")

    # Приходные (cashin + paymentin)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT moment, doc_type, agent_name, description, amount_kop
            FROM cashflow_event
            WHERE day BETWEEN %s AND %s AND direction = 'in'
            ORDER BY moment
        """, (d_from, d_to))
        in_docs = cur.fetchall()

    if in_docs:
        lines.append(f"── 📈 Приходные ордера ({len(in_docs)}) ──")
        for moment, doc_type, agent, desc, amount in in_docs:
            dt = (moment.strftime("%d.%m %H:%M")
                  if hasattr(moment, "strftime") else str(moment)[:16])
            ch = "Касса" if doc_type == "cashin" else "Банк"
            row = f"  {dt} [{ch}] {_rub(float(amount))} ₽"
            if agent:
                row += f"  <- {agent}"
            if desc:
                row += f"\n     {desc}"
            lines.append(row)

    return "\n".join(lines)
