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


# Суммы перемещений — в закупочных ценах из приёмок (методика E), на дату документа.
# Фолбэк на cost_kop МойСклад для позиций без приёмки.
_MV_JOIN = """
    FROM move_doc d
    JOIN move_item i ON i.doc_id = d.doc_id
    LEFT JOIN LATERAL (
        SELECT price_kop FROM purchase_price_asof p
        WHERE p.product_id = i.product_id AND p.priced_from <= d.day
        ORDER BY p.priced_from DESC LIMIT 1
    ) pp ON true
"""
# Закупочная стоимость позиции (фолбэк на total_kop МойСклад).
_MV_TOTAL = ("CASE WHEN pp.price_kop IS NOT NULL AND pp.price_kop > 0 "
             "THEN round(i.qty * pp.price_kop) ELSE i.total_kop END")
# Выручка-покрытие: total_kop только по покрытым приёмками позициям.
_MV_COVERED = "CASE WHEN pp.price_kop IS NOT NULL AND pp.price_kop > 0 THEN i.total_kop ELSE 0 END"

# I8: фильтр «Ассортимент» — в перемещениях учитываем только товар (не ленты,
# упаковку, услуги). По product_id через product_dim, как в списаниях.
_MV_ASSORT = ("i.product_id IN (SELECT product_id FROM product_dim "
              "WHERE folder_path LIKE 'Ассортимент/%%')")


def _two_price(cost_unit, nal_unit) -> str:
    """«закуп X ₽ · нал Y ₽» (прочерк, если цены нет)."""
    c = f"закуп {_rub(cost_unit)} ₽" if cost_unit else "закуп —"
    n = f"нал {_rub(nal_unit)} ₽" if nal_unit else "нал —"
    return f"{c} · {n}"


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
    lines.append("Суммы в закупочных ценах из карточки товара.")

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
                   COALESCE(SUM({_MV_TOTAL}), 0),
                   COALESCE(SUM({_MV_COVERED}), 0),
                   COALESCE(SUM(i.total_kop), 0)
            {_MV_JOIN}
            WHERE d.day BETWEEN %s AND %s {store_cond} AND {_MV_ASSORT}
        """, base_params)
        cnt, total_qty, total_kop, covered_ms, all_ms = cur.fetchone()

    if not cnt:
        lines.append("Данных о перемещениях за этот период нет.")
        return "\n".join(lines)

    coverage = (float(covered_ms) / float(all_ms) * 100) if all_ms else 0.0
    lines.append(f"По закупочным ценам: {coverage:.0f}% стоимости · МойСклад: {100 - coverage:.0f}% (нет закупочной в карточке)")
    lines.append(f"📋 Документов: {cnt} · Позиций: {_qty(float(total_qty))} ед. · Сумма: {_rub(float(total_kop))} ₽")
    lines.append("")

    if store_name:
        # Исходящие со склада
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT d.store_to_name,
                       COUNT(DISTINCT d.doc_id),
                       COALESCE(SUM(i.qty), 0),
                       COALESCE(SUM({_MV_TOTAL}), 0)
                {_MV_JOIN}
                WHERE d.day BETWEEN %s AND %s AND d.store_from_name = %s AND {_MV_ASSORT}
                GROUP BY d.store_to_name
                ORDER BY 4 DESC
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
            cur.execute(f"""
                SELECT d.store_from_name,
                       COUNT(DISTINCT d.doc_id),
                       COALESCE(SUM(i.qty), 0),
                       COALESCE(SUM({_MV_TOTAL}), 0)
                {_MV_JOIN}
                WHERE d.day BETWEEN %s AND %s AND d.store_to_name = %s AND {_MV_ASSORT}
                GROUP BY d.store_from_name
                ORDER BY 4 DESC
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
            cur.execute(f"""
                SELECT d.store_from_name, d.store_to_name,
                       COUNT(DISTINCT d.doc_id),
                       COALESCE(SUM(i.qty), 0),
                       COALESCE(SUM({_MV_TOTAL}), 0)
                {_MV_JOIN}
                WHERE d.day BETWEEN %s AND %s AND {_MV_ASSORT}
                GROUP BY d.store_from_name, d.store_to_name
                ORDER BY 5 DESC
            """, [d_from, d_to])
            flows = cur.fetchall()
        if flows:
            lines.append("── Потоки (откуда → куда) ──")
            for from_name, to_name, dcnt, sqty, skop in flows:
                lines.append(f"  {from_name} → {to_name}: {dcnt} докум. · {_qty(float(sqty))} ед. · {_rub(float(skop))} ₽")
            lines.append("")

    # J5: «Топ-10 перемещаемых товаров» удалён по решению владельца.
    # Остаются: шапка, потоки «откуда → куда», документы с позициями.

    # Все документы с позициями
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT d.doc_id, d.moment, d.day, d.store_from_name, d.store_to_name, d.description
            FROM move_doc d
            WHERE d.day BETWEEN %s AND %s {store_cond}
              AND EXISTS (SELECT 1 FROM move_item i WHERE i.doc_id = d.doc_id AND {_MV_ASSORT})
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
    any_star = False
    for doc_id, moment, doc_day, from_name, to_name, description in docs:
        moment_str = moment.strftime("%d.%m.%Y %H:%M") if hasattr(moment, "strftime") else str(moment)[:16]
        header = f"📅 {moment_str} · {from_name} → {to_name}"
        if description:
            header += f" · {description}"
        lines.append(header)

        # I8: две цены позиции — закупочная (фолбэк cost_kop) и наличная, на дату;
        # только товар «Ассортимент».
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT i.product_name, i.qty, i.cost_kop, i.total_kop,
                       pp.price_kop, np.price_kop
                FROM move_item i
                LEFT JOIN LATERAL (
                    SELECT price_kop FROM purchase_price_asof p
                    WHERE p.product_id = i.product_id AND p.priced_from <= %s
                    ORDER BY p.priced_from DESC LIMIT 1
                ) pp ON true
                LEFT JOIN LATERAL (
                    SELECT price_kop FROM nal_price_asof n
                    WHERE n.product_id = i.product_id AND n.priced_from <= %s
                    ORDER BY n.priced_from DESC LIMIT 1
                ) np ON true
                WHERE i.doc_id = %s AND {_MV_ASSORT}
                ORDER BY i.total_kop DESC
            """, [doc_day, doc_day, doc_id])
            positions = cur.fetchall()

        doc_total = 0.0
        for pname, qty, ms_cost, ms_total, purch_price, nal in positions:
            covered = purch_price is not None and purch_price > 0
            unit = int(purch_price) if covered else int(ms_cost or 0)
            pos_total = round(float(qty) * unit) if covered else float(ms_total)
            nal_unit = int(nal) if nal else 0
            doc_total += float(pos_total)
            star = "" if covered else " *"
            any_star = any_star or not covered
            lines.append(
                f"  • {pname}: {_qty(float(qty))} ед. · {_two_price(unit, nal_unit)} · "
                f"{_rub(float(pos_total))} ₽{star}"
            )
        lines.append(f"  Итого: {_qty(float(sum(p[1] for p in positions)))} ед. · {_rub(doc_total)} ₽")
        lines.append("")

    # total_qty/total_kop — из сводного запроса (не затирать переменной цикла!)
    lines.append(f"═══ ИТОГО: {_qty(float(total_qty))} ед. · {_rub(float(total_kop))} ₽ ═══")
    if any_star:
        lines.append("* себестоимость МойСклад — нет закупочной цены в карточке")
    return "\n".join(lines)
