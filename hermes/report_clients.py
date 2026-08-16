"""Клиентская аналитика: топ клиентов + детектор оттока (churn)."""
from __future__ import annotations

from datetime import date

from . import config


_BASE_STORE = "База Воровского 107/1"


def _rub(kop: float) -> str:
    return f"{kop / 100:,.0f}".replace(",", " ")


def get_top_clients(
    conn, d_from: date, d_to: date,
    store_name: str = _BASE_STORE, limit: int = 40,
) -> list[tuple]:
    """Топ идентифицированных клиентов выбранного склада по выручке."""
    excluded = list(config.RETAIL_PLACEHOLDER_AGENTS or [""])
    excluded += list(config.INTERNAL_AGENTS or [""])
    with conn.cursor() as cur:
        cur.execute("""
            SELECT agent_name,
                   COUNT(*) FILTER (WHERE doc_type = 'demand') AS orders,
                   COALESCE(SUM(sum_kop), 0) AS revenue
            FROM sales_doc
            WHERE day BETWEEN %s AND %s
              AND store_name = %s
              AND agent_id IS NOT NULL AND agent_id != ''
              AND NOT (agent_name = ANY(%s))
            GROUP BY agent_id, agent_name
            ORDER BY revenue DESC
            LIMIT %s
        """, (d_from, d_to, store_name, excluded, limit))
        return cur.fetchall()


def get_churn_clients(
    conn, limit: int | None = None, store_name: str = _BASE_STORE,
    inactive_days: int = 10, min_avg_check_kop: int = 1_000_000,
) -> list[tuple]:
    """Все клиенты БАЗЫ с паузой и средним чеком от заданной суммы."""
    excluded = list(config.RETAIL_PLACEHOLDER_AGENTS or [""])
    excluded += list(config.INTERNAL_AGENTS or [""])
    limit_sql = "LIMIT %s" if limit is not None else ""
    params = [store_name, excluded, inactive_days, min_avg_check_kop]
    if limit is not None:
        params.append(limit)
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT agent_name,
                   MAX(day) AS last_day,
                   CURRENT_DATE - MAX(day) AS days_since,
                   COUNT(*) AS orders,
                   COALESCE(SUM(sum_kop), 0) AS revenue,
                   COALESCE(AVG(sum_kop), 0) AS avg_check
            FROM sales_doc
            WHERE store_name = %s
              AND doc_type = 'demand'
              AND agent_id IS NOT NULL AND agent_id != ''
              AND NOT (agent_name = ANY(%s))
            GROUP BY agent_id, agent_name
            HAVING CURRENT_DATE - MAX(day) > %s
               AND AVG(sum_kop) >= %s
            ORDER BY days_since DESC, avg_check DESC, agent_name
            {limit_sql}
        """, params)
        return cur.fetchall()


def build_clients_report(
    conn, d_from: date, d_to: date, store_name: str | None = None,
    include_top: bool = True, top_limit: int = 20,
    include_churn: bool = True, churn_limit: int = 50,
) -> str:
    period_str = (
        d_from.strftime("%d.%m.%Y") if d_from == d_to
        else f"{d_from.strftime('%d.%m.%Y')} – {d_to.strftime('%d.%m.%Y')}"
    )
    store_label = f" · {store_name}" if store_name else " · Все склады"
    lines: list[str] = [f"👥 Клиенты {period_str}{store_label}", ""]

    sf = "AND store_name = %s" if store_name else ""
    p  = [d_from, d_to] + ([store_name] if store_name else [])

    # Сводка за период. Сумма — нетто (отгрузки минус возвраты); «заказы» —
    # только отгрузки (возврат не заказ).
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT COUNT(DISTINCT agent_id),
                   COUNT(*) FILTER (WHERE doc_type = 'demand'),
                   COALESCE(SUM(sum_kop), 0)
            FROM sales_doc
            WHERE day BETWEEN %s AND %s {sf}
              AND agent_id IS NOT NULL AND agent_id != ''
        """, p)
        row = cur.fetchone()

    if not row or not row[0]:
        lines.append("Данных по клиентам за этот период нет.")
        return "\n".join(lines)

    unique_clients = int(row[0])
    total_docs     = int(row[1])
    total_kop      = float(row[2])
    # «Сумма», а не «нетто»: возвраты оптовикам оформляются расходным ордером
    # (статья «Возврат»), документы salesreturn бизнесом фактически не
    # используются — вычитать нечего. См. ANALYTICS_FORMULAS.md.
    lines.append(
        f"📋 Клиентов: {unique_clients} · Заказов: {total_docs}"
        f" · Сумма: {_rub(total_kop)} ₽"
    )

    # Контроль покрытия: сумма по клиентам vs выручка из секции «Продажи»
    # (sales_by_store_day). Расхождение — на возвраты; должно быть видно сразу.
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT COALESCE(SUM(revenue_kop), 0)
            FROM sales_by_store_day
            WHERE day BETWEEN %s AND %s {sf}
        """, p)
        sec1_kop = float(cur.fetchone()[0] or 0)
    if sec1_kop > 0:
        pct = total_kop / sec1_kop * 100
        lines.append(
            f"📊 Покрытие: {_rub(total_kop)} ₽ из {_rub(sec1_kop)} ₽ выручки ({pct:.0f}%)"
        )
    lines.append("")

    placeholders = config.RETAIL_PLACEHOLDER_AGENTS or [""]
    internal = config.INTERNAL_AGENTS or [""]
    hints = config.INTERNAL_HINTS or []
    excluded = list(placeholders) + list(internal)

    # Топ клиентов: при общем отчёте только склад БАЗА.
    top_store = store_name or _BASE_STORE
    top = (
        get_top_clients(conn, d_from, d_to, top_store, limit=top_limit)
        if include_top else []
    )

    if top:
        lines.append(f"🏆 Топ-{len(top)} клиентов · {top_store}:")
        suspect = False
        for i, (name, orders, rev) in enumerate(top, 1):
            avg = float(rev) / orders if orders else 0
            gear = " ⚙" if any(h in (name or "") for h in hints) else ""
            if gear:
                suspect = True
            lines.append(
                f"  {i:2}. {name}{gear}\n"
                f"      {orders} зак. · {_rub(float(rev))} ₽ · ср.чек {_rub(avg)} ₽"
            )
        if suspect:
            lines.append("  ⚙ похоже на внутренний контрагент — проверить (не исключён)")
        lines.append("")

    # Две отдельные строки: розница без идентификации vs внутренние операции.
    def _sum_for(names):
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT COALESCE(SUM(sum_kop), 0),
                       COUNT(*) FILTER (WHERE doc_type = 'demand')
                FROM sales_doc
                WHERE day BETWEEN %s AND %s {sf} AND agent_name = ANY(%s)
            """, p + [names])
            return cur.fetchone()

    ph_kop, ph_docs = _sum_for(placeholders)
    if ph_kop and float(ph_kop) != 0:
        lines.append(f"🏪 Розничные продажи без идентификации клиента: "
                     f"{_rub(float(ph_kop))} ₽ ({int(ph_docs)} док)")
    in_kop, in_docs = _sum_for(internal)
    if in_kop and float(in_kop) != 0:
        lines.append(f"⚙ Внутренние операции (свои точки, ИП): "
                     f"{_rub(float(in_kop))} ₽ ({int(in_docs)} док)")
    if (ph_kop and float(ph_kop) != 0) or (in_kop and float(in_kop) != 0):
        lines.append("")

    if include_churn:
        churned = get_churn_clients(
            conn, limit=None, store_name=_BASE_STORE, inactive_days=10,
            min_avg_check_kop=1_000_000,
        )
        lines.append("")
        lines.append("⚠️ Возможный отток · БАЗА:")
        lines.append(
            "Все клиенты без заказа больше 10 дней со средним чеком от 10 000 ₽."
        )
        if churned:
            for name, last_day, days_since, orders, revenue, avg_check in churned:
                last_str = last_day.strftime("%d.%m.%Y") if hasattr(last_day, "strftime") else str(last_day)
                lines.append(
                    f"  • {name}: посл. заказ {last_str}"
                    f" ({int(days_since)} дн. назад, {int(orders)} заказов,"
                    f" всего {_rub(float(revenue))} ₽,"
                    f" ср. чек {_rub(float(avg_check))} ₽)"
                )
        else:
            lines.append("✅ Отставших оптовиков не обнаружено")

    return "\n".join(lines)
