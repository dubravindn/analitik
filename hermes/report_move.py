"""Отчёт «Перемещения между складами»: движение товара со склада на склад.

Канал «ресторан» (СОБРАНИЕ) работает через перемещения — прибыль СОБРАНИЯ
считается по отгрузкам, а поступление товара видно здесь.
"""
from __future__ import annotations

from datetime import date


def _rub(kop: float) -> str:
    return f"{kop / 100:,.0f}".replace(",", " ")


def _qty(q: float) -> str:
    if q == int(q):
        return f"{int(q):,}".replace(",", " ")
    return f"{q:,.1f}".replace(",", " ")


def build_move_report(conn, d_from: date, d_to: date, store_name: str | None = None,
                      max_docs: int | None = 50) -> str:
    """Полный отчёт по перемещениям: сводка по складам + документы с позициями.

    Без склада — общая картина потоков между всеми складами.
    С выбранным складом — раздельно «исходящие со склада» и «входящие на склад».
    max_docs ограничивает число выводимых документов (последние N) — защита от
    лимита Telegram 4096. В PDF передаём max_docs=None (показать все).
    """
    period_str = (
        d_from.strftime("%d.%m.%Y") if d_from == d_to
        else f"{d_from.strftime('%d.%m.%Y')} – {d_to.strftime('%d.%m.%Y')}"
    )
    store_label = f" · {store_name}" if store_name else " · Все склады"
    lines: list[str] = []
    lines.append(f"🔄 Перемещения {period_str}{store_label}")
    lines.append("")

    # Сводка (учитываем документ, если он касается выбранного склада как источник ИЛИ приёмник)
    store_cond = ""
    base_params: list = [d_from, d_to]
    if store_name:
        store_cond = "AND (d.store_from_name = %s OR d.store_to_name = %s)"
        base_params += [store_name, store_name]

    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT COUNT(DISTINCT d.doc_id),
                   COALESCE(SUM(i.qty), 0),
                   COALESCE(SUM(i.total_kop), 0)
            FROM move_doc d
            JOIN move_item i ON i.doc_id = d.doc_id
            WHERE d.day BETWEEN %s AND %s {store_cond}
        """, base_params)
        cnt, total_qty, total_kop = cur.fetchone()

    if not cnt:
        lines.append("Данных о перемещениях за этот период нет.")
        return "\n".join(lines)

    lines.append(f"📋 Документов: {cnt} · Позиций: {_qty(float(total_qty))} ед. · Сумма: {_rub(float(total_kop))} ₽")
    lines.append("")

    if store_name:
        # Исходящие со склада
        with conn.cursor() as cur:
            cur.execute("""
                SELECT d.store_to_name,
                       COUNT(DISTINCT d.doc_id),
                       COALESCE(SUM(i.qty), 0),
                       COALESCE(SUM(i.total_kop), 0)
                FROM move_doc d
                JOIN move_item i ON i.doc_id = d.doc_id
                WHERE d.day BETWEEN %s AND %s AND d.store_from_name = %s
                GROUP BY d.store_to_name
                ORDER BY SUM(i.total_kop) DESC
            """, [d_from, d_to, store_name])
            outgoing = cur.fetchall()
        if outgoing:
            out_kop = sum(float(r[3]) for r in outgoing)
            lines.append(f"📤 Исходящие со склада: {_rub(out_kop)} ₽")
            for to_name, dcnt, sqty, skop in outgoing:
                lines.append(f"  → {to_name}: {dcnt} докум. · {_qty(float(sqty))} ед. · {_rub(float(skop))} ₽")
            lines.append("")

        # Входящие на склад
        with conn.cursor() as cur:
            cur.execute("""
                SELECT d.store_from_name,
                       COUNT(DISTINCT d.doc_id),
                       COALESCE(SUM(i.qty), 0),
                       COALESCE(SUM(i.total_kop), 0)
                FROM move_doc d
                JOIN move_item i ON i.doc_id = d.doc_id
                WHERE d.day BETWEEN %s AND %s AND d.store_to_name = %s
                GROUP BY d.store_from_name
                ORDER BY SUM(i.total_kop) DESC
            """, [d_from, d_to, store_name])
            incoming = cur.fetchall()
        if incoming:
            in_kop = sum(float(r[3]) for r in incoming)
            lines.append(f"📥 Входящие на склад: {_rub(in_kop)} ₽")
            for from_name, dcnt, sqty, skop in incoming:
                lines.append(f"  ← {from_name}: {dcnt} докум. · {_qty(float(sqty))} ед. · {_rub(float(skop))} ₽")
            lines.append("")
    else:
        # Потоки между складами: источник → приёмник
        with conn.cursor() as cur:
            cur.execute("""
                SELECT d.store_from_name, d.store_to_name,
                       COUNT(DISTINCT d.doc_id),
                       COALESCE(SUM(i.qty), 0),
                       COALESCE(SUM(i.total_kop), 0)
                FROM move_doc d
                JOIN move_item i ON i.doc_id = d.doc_id
                WHERE d.day BETWEEN %s AND %s
                GROUP BY d.store_from_name, d.store_to_name
                ORDER BY SUM(i.total_kop) DESC
            """, [d_from, d_to])
            flows = cur.fetchall()
        if flows:
            lines.append("── Потоки (откуда → куда) ──")
            for from_name, to_name, dcnt, sqty, skop in flows:
                lines.append(f"  {from_name} → {to_name}: {dcnt} докум. · {_qty(float(sqty))} ед. · {_rub(float(skop))} ₽")
            lines.append("")

    # Топ-10 перемещаемых товаров
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT i.product_name, SUM(i.qty), SUM(i.total_kop)
            FROM move_doc d
            JOIN move_item i ON i.doc_id = d.doc_id
            WHERE d.day BETWEEN %s AND %s {store_cond}
            GROUP BY i.product_name
            ORDER BY SUM(i.total_kop) DESC
            LIMIT 10
        """, base_params)
        top = cur.fetchall()

    if top:
        lines.append("🏆 Топ-10 перемещаемых товаров:")
        for i, (name, qty, kop) in enumerate(top, 1):
            lines.append(f"  {i:2}. {name}: {_qty(float(qty))} ед. · {_rub(float(kop))} ₽")
        lines.append("")

    # Все документы с позициями
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT d.doc_id, d.moment, d.store_from_name, d.store_to_name, d.description
            FROM move_doc d
            WHERE d.day BETWEEN %s AND %s {store_cond}
            ORDER BY d.moment
        """, base_params)
        docs = cur.fetchall()

    total_docs = len(docs)
    if max_docs is not None and total_docs > max_docs:
        docs = docs[-max_docs:]   # последние N (самые свежие)
        lines.append(f"── Документы: показаны {max_docs} из {total_docs} "
                     f"(полный список — в PDF) ──")
    else:
        lines.append(f"── Все документы ({total_docs}) ──")
    lines.append("")
    for doc_id, moment, from_name, to_name, description in docs:
        moment_str = moment.strftime("%d.%m.%Y %H:%M") if hasattr(moment, "strftime") else str(moment)[:16]
        header = f"📅 {moment_str} · {from_name} → {to_name}"
        if description:
            header += f" · {description}"
        lines.append(header)

        with conn.cursor() as cur:
            cur.execute("""
                SELECT product_name, qty, cost_kop, total_kop
                FROM move_item
                WHERE doc_id = %s
                ORDER BY total_kop DESC
            """, [doc_id])
            positions = cur.fetchall()

        doc_total = 0.0
        for pname, qty, pos_cost_kop, pos_total_kop in positions:
            doc_total += float(pos_total_kop)
            cost_str = f" × {_rub(pos_cost_kop)} ₽/ед." if pos_cost_kop else ""
            lines.append(
                f"  • {pname}: {_qty(float(qty))} ед.{cost_str} = {_rub(float(pos_total_kop))} ₽"
            )
        lines.append(f"  Итого: {_qty(float(sum(p[1] for p in positions)))} ед. · {_rub(doc_total)} ₽")
        lines.append("")

    # total_qty/total_kop — из сводного запроса (не затирать переменной цикла!)
    lines.append(f"═══ ИТОГО: {_qty(float(total_qty))} ед. · {_rub(float(total_kop))} ₽ ═══")
    return "\n".join(lines)
