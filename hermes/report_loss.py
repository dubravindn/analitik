"""Отчёт «Аналитик списаний»: все документы с полным раскрытием позиций."""
from __future__ import annotations

from datetime import date

from . import config


def _rub(kop: float) -> str:
    return f"{kop / 100:,.0f}".replace(",", " ")


def _qty(q: float) -> str:
    if q == int(q):
        return f"{int(q):,}".replace(",", " ")
    return f"{q:,.1f}".replace(",", " ")


# Стоимость списаний — по закупочной цене из карточки товара (блок G), на дату
# документа. Фолбэк на cost_kop МойСклад для позиций без цены.
_LS_JOIN = """
    FROM loss_doc d
    JOIN loss_item i ON i.doc_id = d.doc_id
    LEFT JOIN LATERAL (
        SELECT price_kop FROM purchase_price_asof p
        WHERE p.product_id = i.product_id AND p.priced_from <= d.day
        ORDER BY p.priced_from DESC LIMIT 1
    ) pp ON true
"""
_LS_TOTAL = ("CASE WHEN pp.price_kop IS NOT NULL AND pp.price_kop > 0 "
             "THEN round(i.qty * pp.price_kop) ELSE i.total_kop END")

# I4: вторая цена — оптовая «Наличка» из карточки на дату документа. Где нет
# наличной цены — вклад в наличный итог 0 (в строке показываем «нал —»).
_LS_NAL = """
    LEFT JOIN LATERAL (
        SELECT price_kop FROM nal_price_asof n
        WHERE n.product_id = i.product_id AND n.priced_from <= d.day
        ORDER BY n.priced_from DESC LIMIT 1
    ) np ON true
"""
_LS_NAL_TOTAL = ("CASE WHEN np.price_kop IS NOT NULL AND np.price_kop > 0 "
                 "THEN round(i.qty * np.price_kop) ELSE 0 END")


def _two_price(cost_unit, nal_unit) -> str:
    """«закуп X ₽ · нал Y ₽» (прочерк, если цены нет)."""
    c = f"закуп {_rub(cost_unit)} ₽" if cost_unit else "закуп —"
    n = f"нал {_rub(nal_unit)} ₽" if nal_unit else "нал —"
    return f"{c} · {n}"


# Оприходования (enter) — «+»-сторона инвентаризации. Та же методика цены, что и
# у списаний (закупочная из карточки на дату, фолбэк на цену документа).
_EN_JOIN = """
    FROM enter_doc d
    JOIN enter_item i ON i.doc_id = d.doc_id
    LEFT JOIN LATERAL (
        SELECT price_kop FROM purchase_price_asof p
        WHERE p.product_id = i.product_id AND p.priced_from <= d.day
        ORDER BY p.priced_from DESC LIMIT 1
    ) pp ON true
"""
_EN_TOTAL = ("CASE WHEN pp.price_kop IS NOT NULL AND pp.price_kop > 0 "
             "THEN round(i.qty * pp.price_kop) ELSE i.total_kop END")

_ASSORT = ("i.product_id IN (SELECT product_id FROM product_dim "
           "WHERE folder_path LIKE 'Ассортимент/%%')")


_ENTER_MISSING = None  # sentinel: enter_doc не существует в БД


def _enter_summary(conn, d_from, d_to, adj):
    """Сводка оприходований Базы (Ассортимент), той же методикой цены.

    Возвращает (cnt, qty, kop) или _ENTER_MISSING, если таблица ещё не создана.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT COUNT(DISTINCT d.doc_id), COALESCE(SUM(i.qty), 0),
                       COALESCE(SUM({_EN_TOTAL}), 0)
                {_EN_JOIN}
                WHERE d.day BETWEEN %s AND %s AND {_ASSORT} AND d.store_name = ANY(%s)
            """, [d_from, d_to, adj])
            return cur.fetchone()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return _ENTER_MISSING


def _inventory_block(conn, lines, d_from, d_to, adj, adj_qty, adj_kop,
                     ent_cnt, ent_qty, ent_kop):
    """J2: полный блок инвентаризации Базы — списания и оприходования в обе
    стороны, документы с позициями, итоговая корректировка. Сводки по обеим
    сторонам уже посчитаны в основном отчёте (не пересчитываем)."""
    lines.append("── 📋 ИНВЕНТАРИЗАЦИЯ (База) — корректировки учёта, НЕ потери ──")
    lines.append(
        f"Списано: {_qty(float(adj_qty))} ед. · {_rub(float(adj_kop))} ₽ · "
        f"Оприходовано: {_qty(float(ent_qty))} ед. · {_rub(float(ent_kop))} ₽"
    )
    net = float(adj_kop) - float(ent_kop)   # >0 → учётный остаток был завышен (недостача)
    sign = "−" if net > 0 else ("+" if net < 0 else "")
    lines.append(f"Итог корректировки: {sign}{_rub(abs(net))} ₽")
    if not ent_cnt:
        lines.append("ℹ️ Оприходований за период нет "
                     "(если инвентаризация давала «плюс» — проверь sync-enter).")
    lines.append("")

    # Документы обеих сторон, по времени. sign_char печатаем перед суммой позиции.
    def _emit_docs(join_sql, total_expr, table, item_table, kind_label, pos_sign):
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT d.doc_id, d.moment, d.day, d.description
                FROM {table} d
                WHERE d.day BETWEEN %s AND %s AND d.store_name = ANY(%s)
                  AND EXISTS (SELECT 1 FROM {item_table} i
                              WHERE i.doc_id = d.doc_id AND {_ASSORT})
                ORDER BY d.moment
            """, [d_from, d_to, adj])
            docs = cur.fetchall()
        for doc_id, moment, doc_day, description in docs:
            moment_str = (moment.strftime("%d.%m.%Y %H:%M")
                          if hasattr(moment, "strftime") else str(moment)[:16])
            header = f"📅 {moment_str} · {kind_label}"
            if description:
                header += f" · {description}"
            lines.append(header)
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT i.product_name, i.qty, i.cost_kop, i.total_kop, pp.price_kop
                    FROM {item_table} i
                    LEFT JOIN LATERAL (
                        SELECT price_kop FROM purchase_price_asof p
                        WHERE p.product_id = i.product_id AND p.priced_from <= %s
                        ORDER BY p.priced_from DESC LIMIT 1
                    ) pp ON true
                    WHERE i.doc_id = %s AND {_ASSORT}
                    ORDER BY i.total_kop DESC
                """, [doc_day, doc_id])
                positions = cur.fetchall()
            doc_total = 0.0
            for pname, qty, ms_cost, ms_total, purch in positions:
                covered = purch is not None and purch > 0
                unit = int(purch) if covered else int(ms_cost or 0)
                pos_total = round(float(qty) * unit) if covered else float(ms_total)
                doc_total += float(pos_total)
                cost_str = f" × {_rub(unit)} ₽/ед." if unit else ""
                lines.append(
                    f"  • {pname}: {_qty(float(qty))} ед.{cost_str} = "
                    f"{pos_sign}{_rub(float(pos_total))} ₽"
                )
            lines.append(f"  Итого: {_qty(float(sum(p[1] for p in positions)))} ед. · "
                         f"{pos_sign}{_rub(doc_total)} ₽")
            lines.append("")

    _emit_docs(_LS_JOIN, _LS_TOTAL, "loss_doc", "loss_item", "Списание", "−")
    _emit_docs(_EN_JOIN, _EN_TOTAL, "enter_doc", "enter_item", "Оприходование", "+")


def build_loss_report(conn, d_from: date, d_to: date, store_name: str | None = None,
                       max_docs: int | None = 50) -> str:
    """Полный отчёт по списаниям: каждый документ + все позиции.

    max_docs ограничивает число выводимых документов (последние N) — защита от
    лимита Telegram 4096. В PDF передаём max_docs=None (показать все).
    """
    period_str = (
        d_from.strftime("%d.%m.%Y") if d_from == d_to
        else f"{d_from.strftime('%d.%m.%Y')} – {d_to.strftime('%d.%m.%Y')}"
    )
    store_label = f" · {store_name}" if store_name else " · Все склады"
    lines: list[str] = []
    lines.append(f"🗑 Списания {period_str}{store_label}")
    lines.append("Стоимость — по закупочным ценам из карточки товара.")
    lines.append("")

    sf = "AND d.store_name = %s" if store_name else ""
    p  = [d_from, d_to] + ([store_name] if store_name else [])
    adj = config.ADJUSTMENT_STORES or [""]

    # Три категории (H2): порча (розница), корректировки учёта (База), возвраты.
    def _loss_sum(cond, extra):
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT COUNT(DISTINCT d.doc_id), COALESCE(SUM(i.qty), 0),
                       COALESCE(SUM({_LS_TOTAL}), 0)
                {_LS_JOIN}
                WHERE d.day BETWEEN %s AND %s AND i.product_id IN (SELECT product_id FROM product_dim WHERE folder_path LIKE 'Ассортимент/%%') {sf} {cond}
            """, p + extra)
            return cur.fetchone()

    spoil_cnt, spoil_qty, spoil_kop = _loss_sum("AND NOT (d.store_name = ANY(%s))", [adj])
    adj_cnt, adj_qty, adj_kop = _loss_sum("AND (d.store_name = ANY(%s))", [adj])

    # Возвраты клиентам — из расходов (cashflow), статья «Возврат».
    with conn.cursor() as cur:
        cur.execute("""
            SELECT COUNT(*), COALESCE(SUM(amount_kop), 0)
            FROM cashflow_event
            WHERE day BETWEEN %s AND %s AND direction = 'out'
              AND expense_item_name ILIKE '%%возврат%%'
        """, [d_from, d_to])
        ret_cnt, ret_kop = cur.fetchone()

    if not spoil_cnt and not adj_cnt and not ret_kop:
        lines.append("Данных о списаниях за этот период нет.")
        return "\n".join(lines)

    lines.append(f"🌸 Порча (розница): {_rub(float(spoil_kop))} ₽ · "
                 f"{_qty(float(spoil_qty))} ед. · {spoil_cnt} докум.")
    lines.append(f"📋 Корректировки инвентаризации (База): {_rub(float(adj_kop))} ₽ · "
                 f"{adj_cnt} докум. — учёт, НЕ потери")
    if ret_kop:
        lines.append(f"💸 Возвраты клиентам: {_rub(float(ret_kop))} ₽ · {int(ret_cnt)} опер. "
                     f"(те же операции в разделе «Расходы»)")
    lines.append("")

    # J2: полный блок инвентаризации Базы (списания + оприходования в обе стороны).
    # Показываем, когда склад не выбран или выбрана сама База.
    show_inventory = (not store_name) or (store_name in adj)
    _ent = _enter_summary(conn, d_from, d_to, adj)
    if _ent is _ENTER_MISSING:
        ent_cnt, ent_qty, ent_kop = 0, 0, 0
        if show_inventory:
            lines.append("ℹ️ Оприходования: данные не загружены — запустите sync-enter "
                         "(или python -m hermes migrate).")
            lines.append("")
    else:
        ent_cnt, ent_qty, ent_kop = _ent
    if show_inventory and (adj_cnt or ent_cnt) and _ent is not _ENTER_MISSING:
        _inventory_block(conn, lines, d_from, d_to, adj,
                         adj_qty, adj_kop, ent_cnt, ent_qty, ent_kop)

    # Порчу (розницу) детализируем, когда склад не выбран или выбран НЕ База.
    show_spoilage = (not store_name) or (store_name not in adj)
    if not show_spoilage:
        return "\n".join(lines).rstrip()

    if not spoil_cnt:
        lines.append("Порчи (списаний на рознице) за период нет.")
        return "\n".join(lines).rstrip()

    # Детализация ниже — только ПОРЧА (розница); корректировки Базы не смешиваем.
    lines.append("── 🌸 ПОРЧА (розница) ──")
    sf = sf + " AND NOT (d.store_name = ANY(%s))"
    p  = p + [adj]
    cnt, total_qty, total_kop = spoil_cnt, spoil_qty, spoil_kop

    # I4: наличный итог порчи (по оптовой цене «Наличка», где известна).
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT COALESCE(SUM({_LS_NAL_TOTAL}), 0)
            {_LS_JOIN} {_LS_NAL}
            WHERE d.day BETWEEN %s AND %s AND i.product_id IN (SELECT product_id FROM product_dim WHERE folder_path LIKE 'Ассортимент/%%') {sf}
        """, p)
        total_nal_kop = float(cur.fetchone()[0] or 0)

    # I9: покрытие закупочными ценами из карточки (как в продажах/остатках/перемещениях).
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT COALESCE(SUM(CASE WHEN pp.price_kop IS NOT NULL AND pp.price_kop > 0
                                     THEN round(i.qty * pp.price_kop) ELSE 0 END), 0),
                   COALESCE(SUM({_LS_TOTAL}), 0)
            {_LS_JOIN}
            WHERE d.day BETWEEN %s AND %s AND i.product_id IN (SELECT product_id FROM product_dim WHERE folder_path LIKE 'Ассортимент/%%') {sf}
        """, p)
        _cov_row = cur.fetchone()
    _cov_kop = float(_cov_row[0] or 0) if _cov_row else 0.0
    _all_kop = float(_cov_row[1] or 0) if _cov_row else 0.0
    if _all_kop:
        cov = _cov_kop / _all_kop * 100
        lines.append(f"По закупочным ценам: {cov:.0f}% стоимости · "
                     f"МойСклад: {100 - cov:.0f}% (нет закупочной в карточке)")
        # K2: симметричная строка покрытия «Наличка»
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT COUNT(*) FILTER (WHERE np.price_kop IS NOT NULL AND np.price_kop > 0),
                       COUNT(*),
                       COALESCE(SUM(CASE WHEN np.price_kop IS NOT NULL AND np.price_kop > 0
                                        THEN {_LS_TOTAL} ELSE 0 END), 0),
                       COALESCE(SUM({_LS_TOTAL}), 0)
                {_LS_JOIN} {_LS_NAL}
                WHERE d.day BETWEEN %s AND %s
                  AND i.product_id IN (SELECT product_id FROM product_dim WHERE folder_path LIKE 'Ассортимент/%%') {sf}
            """, p)
            _nal_r = cur.fetchone() or (0, 0, 0, 0)
        _nal_pos = int(_nal_r[0] or 0)
        _nal_kop2 = float(_nal_r[2] or 0)
        _nal_all2 = float(_nal_r[3] or 0)
        if int(_nal_r[1] or 0):
            _pct = _nal_kop2 / _nal_all2 * 100 if _nal_all2 else 0
            _pfx = "⚠️ " if _pct < 90 else ""
            lines.append(f"{_pfx}Наличная цена известна для {_nal_pos} из {int(_nal_r[1])} поз. ({_pct:.0f}% суммы)")
        lines.append("")

    # Разбивка по складам
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT d.store_name,
                   COUNT(DISTINCT d.doc_id),
                   COALESCE(SUM(i.qty), 0),
                   COALESCE(SUM({_LS_TOTAL}), 0)
            {_LS_JOIN}
            WHERE d.day BETWEEN %s AND %s AND i.product_id IN (SELECT product_id FROM product_dim WHERE folder_path LIKE 'Ассортимент/%%') {sf}
            GROUP BY d.store_name
            ORDER BY SUM({_LS_TOTAL}) DESC
        """, p)
        by_store = cur.fetchall()

    if not store_name and len(by_store) > 1:
        lines.append("── По складам ──")
        for sn, dcnt, sqty, skop in by_store:
            lines.append(f"  📍 {sn}: {dcnt} докум. · {_qty(float(sqty))} ед. · {_rub(float(skop))} ₽")
        lines.append("  ⚠️ Это место списания, а не оценка работы точки: "
                     "часть порчи перемещена с Базы и списана на рознице.")
        lines.append("")

    # I4: разбивка «По проектам» убрана (владельцу не нужна).

    # Топ-10 товаров по сумме
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT i.product_name, SUM(i.qty), SUM({_LS_TOTAL})
            {_LS_JOIN}
            WHERE d.day BETWEEN %s AND %s AND i.product_id IN (SELECT product_id FROM product_dim WHERE folder_path LIKE 'Ассортимент/%%') {sf}
            GROUP BY i.product_name
            ORDER BY SUM({_LS_TOTAL}) DESC
            LIMIT 10
        """, p)
        top = cur.fetchall()

    if top:
        lines.append(f"🏆 Топ-{len(top)} по сумме списания:")
        for i, (name, qty, kop) in enumerate(top, 1):
            lines.append(f"  {i:2}. {name}: {_qty(float(qty))} ед. · {_rub(float(kop))} ₽")
        lines.append("")

    # Все документы с позициями
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT d.doc_id, d.moment, d.day, d.store_name, d.description, d.project_name
            FROM loss_doc d
            WHERE d.day BETWEEN %s AND %s {sf}
              AND EXISTS (SELECT 1 FROM loss_item i WHERE i.doc_id = d.doc_id AND i.product_id IN (SELECT product_id FROM product_dim WHERE folder_path LIKE 'Ассортимент/%%'))
            ORDER BY d.moment
        """, p)
        docs = cur.fetchall()

    total_docs = len(docs)
    if max_docs is not None and total_docs > max_docs:
        hidden_docs = docs[:total_docs - max_docs]   # старые (скрываем)
        docs        = docs[total_docs - max_docs:]   # последние N
        # Быстрая оценка стоимости скрытых документов по total_kop (МойСклад-cost)
        hidden_ids  = tuple(d[0] for d in hidden_docs)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(SUM(total_kop), 0) FROM loss_item WHERE doc_id = ANY(%s)",
                [list(hidden_ids)],
            )
            hidden_kop = float(cur.fetchone()[0] or 0)
        lines.append(
            f"── Документы: показаны {max_docs} из {total_docs} "
            f"(и ещё {total_docs - max_docs} докум. · {_rub(hidden_kop)} ₽ — полный список в PDF) ──"
        )
    else:
        lines.append(f"── Все документы ({total_docs}) ──")
    lines.append("")
    for doc_id, moment, doc_day, sn, description, project_name in docs:
        moment_str = moment.strftime("%d.%m.%Y %H:%M") if hasattr(moment, "strftime") else str(moment)[:16]
        header = f"📅 {moment_str}"
        if not store_name:
            header += f" · {sn}"
        if project_name:
            header += f" · [{project_name}]"
        if description:
            header += f" · {description}"
        lines.append(header)

        # I4: две цены позиции — закупочная (фолбэк cost_kop) и наличная, на дату.
        with conn.cursor() as cur:
            cur.execute("""
                SELECT i.product_name, i.qty, i.cost_kop, i.total_kop,
                       pp.price_kop, np.price_kop
                FROM loss_item i
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
                WHERE i.doc_id = %s AND i.product_id IN (SELECT product_id FROM product_dim WHERE folder_path LIKE 'Ассортимент/%%')
                ORDER BY i.total_kop DESC
            """, [doc_day, doc_day, doc_id])
            positions = cur.fetchall()

        doc_total = 0.0
        doc_nal = 0.0
        for pname, qty, ms_cost, ms_total, purch, nal in positions:
            covered = purch is not None and purch > 0
            unit = int(purch) if covered else int(ms_cost or 0)
            pos_total = round(float(qty) * unit) if covered else float(ms_total)
            nal_unit = int(nal) if nal else 0
            doc_total += float(pos_total)
            doc_nal += float(qty) * nal_unit
            lines.append(
                f"  • {pname}: {_qty(float(qty))} ед. · {_two_price(unit, nal_unit)} · "
                f"{_rub(float(pos_total))} ₽"
            )
        lines.append(
            f"  Итого: {_qty(float(sum(p[1] for p in positions)))} ед. · "
            f"закуп {_rub(doc_total)} ₽ · нал {_rub(doc_nal)} ₽"
        )
        lines.append("")

    # Порча (розница) — из сводного запроса (не затирать переменной цикла!)
    lines.append(f"═══ ИТОГО ПОРЧА (розница): {_qty(float(total_qty))} ед. · "
                 f"закуп {_rub(float(total_kop))} ₽ · нал {_rub(total_nal_kop)} ₽ ═══")
    return "\n".join(lines)
