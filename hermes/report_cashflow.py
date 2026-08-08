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


def build_expenses_report(
    conn, d_from: date, d_to: date,
    store_name: str | None = None,
    max_items: int | None = None,
) -> str:
    """Расходы: только исходящие платежи (cashout + paymentout) со статьёй расходов."""
    period_str = (
        d_from.strftime("%d.%m.%Y") if d_from == d_to
        else f"{d_from.strftime('%d.%m.%Y')} – {d_to.strftime('%d.%m.%Y')}"
    )
    lines: list[str] = []
    store_label = store_name or "Все склады"
    lines.append(f"💸 Расходы {period_str} · {store_label}")
    lines.append("(кассовые и банковские исходящие документы)")
    lines.append("")

    # Фильтр по складу.
    #  • Конкретный склад → только его project_name.
    #  • Все склады → БЕЗ фильтра: показываем все расходы, в т.ч. без проекта,
    #    чтобы итог бился с ДДС. Расходы без проекта выделяем группой «Без проекта».
    if store_name:
        _proj_filter = "AND project_name = %s"
        _extra: tuple = (store_name,)
    else:
        _proj_filter = ""
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

    # I5: «По проектам (складам)» — только компактная сводка (≤5 строк).
    # Полное дерево строится по статьям, не по проектам.
    if not store_name:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT COALESCE(NULLIF(project_name, ''), '⚠️ Без проекта'),
                       SUM(amount_kop), COUNT(*),
                       (project_name IS NULL OR project_name = '') AS no_proj
                FROM cashflow_event
                WHERE day BETWEEN %s AND %s AND direction = 'out'
                {_proj_filter}
                GROUP BY 1, 4
                ORDER BY no_proj ASC, 2 DESC
            """, (d_from, d_to) + _extra)
            by_project = cur.fetchall()
        if len(by_project) > 1:
            lines.append("── По складам (сводка) ──")
            for proj_name, kop, cnt, _no in by_project[:5]:
                lines.append(f"  📍 {proj_name}: {_rub(float(kop))} ₽ ({cnt} опер.)")
            if len(by_project) > 5:
                rest_kop = sum(float(r[1]) for r in by_project[5:])
                lines.append(f"  … и ещё {len(by_project) - 5} складов · {_rub(rest_kop)} ₽")
            lines.append("")

    # I5: дерево «статья расходов → документы по убыванию суммы».
    # Один запрос, группировку и сортировку делаем в Python (без N+1).
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT moment, doc_type, agent_name, description,
                   COALESCE(NULLIF(expense_item_name, ''), 'Без статьи'),
                   project_name, amount_kop
            FROM cashflow_event
            WHERE day BETWEEN %s AND %s AND direction = 'out'
            {_proj_filter}
            ORDER BY amount_kop DESC
        """, (d_from, d_to) + _extra)
        out_docs = cur.fetchall()

    # Сгруппировать по статье, сохранив порядок «по убыванию суммы» внутри статьи
    # (запрос уже отсортирован по amount DESC).
    tree: dict[str, list] = {}
    item_total: dict[str, float] = {}
    for moment, doc_type, agent, desc, item, project, amount in out_docs:
        tree.setdefault(item, []).append((moment, doc_type, agent, desc, project, amount))
        item_total[item] = item_total.get(item, 0.0) + float(amount)

    sorted_items = sorted(item_total, key=lambda k: -item_total[k])

    if max_items is not None and len(sorted_items) > max_items:
        hidden_items = sorted_items[max_items:]
        hidden_kop   = sum(item_total[k] for k in hidden_items)
        sorted_items = sorted_items[:max_items]
    else:
        hidden_items, hidden_kop = [], 0.0

    lines.append("── 💸 По статьям (статья → документы) ──")
    for item in sorted_items:
        docs_item = tree[item]
        lines.append(f"▸ {item}: {_rub(item_total[item])} ₽ ({len(docs_item)} опер.)")
        for moment, doc_type, agent, desc, project, amount in docs_item:
            dt = (moment.strftime("%d.%m %H:%M")
                  if hasattr(moment, "strftime") else str(moment)[:16])
            ch = "Касса" if doc_type == "cashout" else "Банк"
            line = f"   • {dt} [{ch}] {_rub(float(amount))} ₽"
            if agent:
                line += f" → {agent}"
            tail = []
            if project:
                tail.append(f"склад: {project}")
            if desc:
                tail.append(desc)
            if tail:
                line += " · " + " | ".join(tail)
            lines.append(line)
        lines.append("")

    if hidden_items:
        lines.append(
            f"… и ещё {len(hidden_items)} статей · {_rub(hidden_kop)} ₽ (полный список в PDF)"
        )

    return "\n".join(lines).rstrip()


def get_owner_withdrawals(conn, d_from: date, d_to: date) -> int:
    """Изъятия собственника за период (копейки). Не входят ни в расходы, ни в прибыль."""
    from . import config
    owner = list(config.OWNER_EXPENSE_ITEMS or [])
    if not owner:
        return 0
    with conn.cursor() as cur:
        cur.execute("""
            SELECT COALESCE(SUM(amount_kop), 0)
            FROM cashflow_event
            WHERE day BETWEEN %s AND %s AND direction = 'out'
              AND expense_item_name = ANY(%s)
        """, (d_from, d_to, owner))
        return int(cur.fetchone()[0] or 0)


def get_operational_expenses(conn, d_from: date, d_to: date) -> dict:
    """Операционные расходы за период без изъятий собственника.

    Returns {"total": int (копейки), "by_project": {"склад": int, "__general__": int}}
    '__general__' — расходы без проекта (None / пустая строка).
    """
    from . import config
    owner = list(config.OWNER_EXPENSE_ITEMS or [])
    excl = ("AND (expense_item_name IS NULL OR NOT (expense_item_name = ANY(%s)))"
            if owner else "")
    params = (d_from, d_to) + ((owner,) if owner else ())

    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT COALESCE(NULLIF(project_name, ''), '__general__'),
                   SUM(amount_kop)
            FROM cashflow_event
            WHERE day BETWEEN %s AND %s AND direction = 'out'
              {excl}
            GROUP BY 1
        """, params)
        rows = cur.fetchall()

    by_project: dict[str, int] = {}
    total = 0
    for proj, kop in rows:
        v = int(kop or 0)
        by_project[proj] = v
        total += v
    return {"total": total, "by_project": by_project}
