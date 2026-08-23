"""Структурированные факты для независимого AI-аналитика.

Модуль намеренно не читает PDF и не пересчитывает бизнес-формулы. Финансовые
показатели берутся из того же ``_pdf_summary``, который использует основной
отчёт, остатки — из помощников текущего PDF, прогноз — из готовых результатов
NEW engine.
"""
from __future__ import annotations

import calendar
import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterable

from . import config


_BASE_STORE = "База Воровского 107/1"
_METRICS = (
    ("rev", "Выручка", "kop"),
    ("profit", "Валовая прибыль", "kop"),
    ("before", "Прибыль до списаний", "kop"),
    ("op_expenses", "Операционные расходы", "kop"),
    ("loss", "Списания", "kop"),
    ("result", "Прибыль после списаний", "kop"),
    ("checks", "Чеки", "count"),
    ("avg_check", "Средний чек", "kop"),
)

_MONTH_HISTORY_METRICS = (
    ("rev", "Выручка", "kop"),
    ("profit", "Валовая прибыль", "kop"),
    ("before", "Прибыль до списаний", "kop"),
    ("result", "Прибыль после списаний", "kop"),
    ("checks", "Чеки", "count"),
    ("avg_check", "Средний чек", "kop"),
)


def _plain(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if hasattr(value, "value"):
        return value.value
    return value


def _fmt(value: Any, unit: str) -> str:
    if value is None:
        return "нет данных"
    if unit == "kop":
        return f"{float(value) / 100:,.0f} ₽".replace(",", " ")
    if unit in {"qty", "count", "days"}:
        number = float(value)
        return f"{number:,.1f}".replace(",", " ") if number % 1 else str(int(number))
    if unit == "pct":
        return f"{float(value):+.1f}%"
    return str(value)


def _fact(
    fact_id: str,
    category: str,
    label: str,
    value: Any,
    unit: str,
    *,
    store: str | None = None,
    period: str | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    evidence = f"{label}: {_fmt(value, unit)}"
    if store:
        evidence += f" · {store}"
    if period:
        evidence += f" · {period}"
    return {
        "id": fact_id,
        "category": category,
        "label": label,
        "value": _plain(value),
        "unit": unit,
        "store": store,
        "period": period,
        "evidence": evidence,
        "details": {k: _plain(v) for k, v in (details or {}).items()},
    }


def _year_back(value: date) -> date:
    day = min(value.day, calendar.monthrange(value.year - 1, value.month)[1])
    return value.replace(year=value.year - 1, day=day)


def _month_start_back(value: date, months: int) -> date:
    """Первый день месяца на ``months`` месяцев раньше ``value``."""
    absolute = value.year * 12 + value.month - 1 - months
    return date(absolute // 12, absolute % 12 + 1, 1)


def _comparison_windows(as_of: date) -> list[tuple[str, date, date, date, date]]:
    month_start = as_of.replace(day=1)
    prev_month_end = month_start - timedelta(days=1)
    prev_month_start = prev_month_end.replace(day=1)
    prev_month_to = prev_month_start.replace(day=min(as_of.day, prev_month_end.day))
    return [
        ("day", as_of, as_of, as_of - timedelta(days=1), as_of - timedelta(days=1)),
        ("week", as_of - timedelta(days=6), as_of,
         as_of - timedelta(days=13), as_of - timedelta(days=7)),
        ("month", month_start, as_of, prev_month_start, prev_month_to),
        ("year", month_start, as_of, _year_back(month_start), _year_back(as_of)),
    ]


def _delta_pct(current: Any, previous: Any) -> float | None:
    try:
        previous_f = float(previous)
        if previous_f == 0:
            return None
        return (float(current) - previous_f) / abs(previous_f) * 100
    except (TypeError, ValueError):
        return None


def _payload(report_type: str, report_id: str, period: dict[str, str], facts: list[dict]) -> dict:
    body = {
        "schema_version": 1,
        "report_type": report_type,
        "report_id": report_id,
        "period": period,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "facts": facts,
    }
    canonical = json.dumps(body, ensure_ascii=False, sort_keys=True, default=str)
    body["payload_hash"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return body


def build_period_analysis_payload(conn, d_from: date, d_to: date, client=None) -> dict:
    """Факты управленческого отчёта и четыре понятных сравнения."""
    from . import calc
    from .report_sales_pdf import _period_metrics
    from .report_clients import get_churn_clients
    from .report_cashflow import get_cashflow_writeoffs

    facts: list[dict[str, Any]] = []
    discount_pids = calc.discount_product_ids(conn)

    def summary(a: date, b: date, store: str | None = None) -> dict:
        # Ровно та же функция и та же скидка поставщика, что в принятом PDF.
        return _period_metrics(conn, a, b, store, discount_pids)

    selected = summary(d_from, d_to)
    selected_label = f"{d_from:%d.%m.%Y}–{d_to:%d.%m.%Y}"
    for key, label, unit in _METRICS:
        facts.append(_fact(
            f"period.selected.{key}", "financial", label,
            selected.get(key, 0), unit, period=selected_label,
        ))

    # Денежные списания БАЗЫ: ИИ получает контрагента, количество документов и
    # сумму, а не видит их как безымянные операционные расходы.
    for index, (agent, docs, amount, avg) in enumerate(
        get_cashflow_writeoffs(conn, d_from, d_to)["by_agent"][:35], 1
    ):
        facts.append(_fact(
            f"period.writeoff_agent.{index}", "loss",
            f"Списание у контрагента: {agent}", amount, "kop",
            store="База Воровского 107/1", period=selected_label,
            details={"documents": docs, "average_kop": avg, "source": "cashflow"},
        ))

    # Сравнения день/неделя/месяц/год используют одинаковую методику отчёта.
    for window, cur_from, cur_to, prev_from, prev_to in _comparison_windows(d_to):
        current = summary(cur_from, cur_to)
        previous = summary(prev_from, prev_to)
        cur_label = f"{cur_from:%d.%m.%Y}–{cur_to:%d.%m.%Y}"
        prev_label = f"{prev_from:%d.%m.%Y}–{prev_to:%d.%m.%Y}"
        for key, label, unit in _METRICS:
            cur_value = current.get(key, 0)
            prev_value = previous.get(key, 0)
            facts.append(_fact(
                f"compare.{window}.{key}.current", "comparison", label,
                cur_value, unit, period=cur_label,
                details={"window": window, "side": "current"},
            ))
            facts.append(_fact(
                f"compare.{window}.{key}.previous", "comparison", label,
                prev_value, unit, period=prev_label,
                details={"window": window, "side": "previous"},
            ))
            delta = _delta_pct(cur_value, prev_value)
            if delta is not None:
                facts.append(_fact(
                    f"compare.{window}.{key}.delta_pct", "comparison",
                    f"Изменение: {label}", round(delta, 2), "pct",
                    period=f"{cur_label} к {prev_label}",
                    details={"window": window},
                ))

    # История того же дня недели: база для поиска аномалии, а не произвольный %.
    with conn.cursor() as cur:
        cur.execute("""
            SELECT day, COALESCE(SUM(revenue_kop), 0), COALESCE(SUM(checks), 0)
            FROM sales_by_store_day
            WHERE day < %s AND day >= %s
              AND EXTRACT(ISODOW FROM day) = EXTRACT(ISODOW FROM %s::date)
            GROUP BY day ORDER BY day DESC LIMIT 8
        """, (d_to, d_to - timedelta(days=70), d_to))
        same_weekday = cur.fetchall()
    for idx, (day_value, revenue, checks) in enumerate(same_weekday, 1):
        facts.append(_fact(
            f"history.same_weekday.{idx}.revenue", "history", "Выручка в тот же день недели",
            revenue, "kop", period=day_value.isoformat(),
        ))
        facts.append(_fact(
            f"history.same_weekday.{idx}.checks", "history", "Чеки в тот же день недели",
            checks, "count", period=day_value.isoformat(),
        ))

    # Текущий месяц сравнивается с тем же числом каждого из 12 прошлых
    # месяцев. Неполные локальные месяцы не используются для выводов: иначе
    # отсутствие синхронизации выглядело бы как реальное падение бизнеса.
    for offset in range(1, 13):
        hist_from = _month_start_back(d_to, offset)
        hist_last_day = calendar.monthrange(hist_from.year, hist_from.month)[1]
        hist_to = hist_from.replace(day=min(d_to.day, hist_last_day))
        expected_days = (hist_to - hist_from).days + 1
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(DISTINCT day)
                FROM sales_by_store_day
                WHERE day BETWEEN %s AND %s
                """,
                (hist_from, hist_to),
            )
            covered_days = int((cur.fetchone() or (0,))[0])

        hist_label = f"{hist_from:%d.%m.%Y}–{hist_to:%d.%m.%Y}"
        complete = covered_days >= expected_days
        facts.append(_fact(
            f"history.month_{offset:02d}.coverage", "data_quality",
            "Покрытие исторического месяца", covered_days, "days",
            period=hist_label,
            details={
                "expected_days": expected_days,
                "complete": complete,
                "months_back": offset,
            },
        ))
        if not complete:
            continue

        # Полное покрытие — это статус источника, а не предупреждение.
        facts[-1]["category"] = "source_status"
        historical = summary(hist_from, hist_to)
        for key, label, unit in _MONTH_HISTORY_METRICS:
            facts.append(_fact(
                f"history.month_{offset:02d}.{key}", "history",
                f"{label} за сопоставимую часть месяца",
                historical.get(key, 0), unit, period=hist_label,
                details={"months_back": offset, "coverage_days": covered_days},
            ))

    with conn.cursor() as cur:
        cur.execute("""
            SELECT store_name
            FROM sales_by_store_day
            WHERE day BETWEEN %s AND %s
              AND channel = ANY(%s)
            GROUP BY store_name ORDER BY SUM(revenue_kop) DESC
        """, (d_from, d_to, list(config.PROFIT_CHANNELS)))
        store_names = [row[0] for row in cur.fetchall()]
    for idx, store_name in enumerate(store_names, 1):
        store_metrics = summary(d_from, d_to, store_name)
        for key, label, unit in (
            ("rev", "Выручка склада", "kop"),
            ("profit", "Валовая прибыль склада", "kop"),
            ("checks", "Чеки склада", "count"),
            ("avg_check", "Средний чек склада", "kop"),
        ):
            facts.append(_fact(
                f"store.{idx}.{key}", "store", label, store_metrics[key], unit,
                store=store_name, period=selected_label,
            ))
        margin = (
            float(store_metrics["profit"]) / float(store_metrics["rev"]) * 100
            if store_metrics["rev"] else 0
        )
        facts.append(_fact(
            f"store.{idx}.gross_margin", "store", "Валовая маржа склада",
            round(margin, 2), "pct", store=store_name, period=selected_label,
        ))

    for idx, row in enumerate(get_churn_clients(
        conn, limit=None, store_name=_BASE_STORE, inactive_days=10,
        min_avg_check_kop=1_000_000,
    ), 1):
        name, last_day, days_since, orders, revenue, avg_check = row
        facts.append(_fact(
            f"client.churn.{idx}", "client_churn", "Клиент возможного оттока",
            int(days_since), "days", store=_BASE_STORE,
            details={
                "client": name, "last_order": last_day, "orders": orders,
                "revenue_kop": revenue, "avg_check_kop": avg_check,
                "rule": "нет заказа >10 дней, средний чек >=10000 ₽",
            },
        ))

    # Инвентаризация — отдельный проверяемый блок: обе стороны корректировки,
    # документная дата и еженедельное покрытие каждой собственной точки.
    from .report_inventory import inventory_fact_rows
    inventory_rows = inventory_fact_rows(conn, d_from, d_to)
    session_idx = coverage_idx = 0
    for row in inventory_rows:
        if "weekly_latest" in row:
            coverage_idx += 1
            latest = row["weekly_latest"]
            category = "inventory_status" if latest else "inventory_missing"
            facts.append(_fact(
                f"inventory.coverage.{coverage_idx}", category,
                "Последняя инвентаризация за 7 дней" if latest else "Инвентаризация за 7 дней не найдена",
                latest or 0, "date" if latest else "count", store=row["store"],
                period=f"{row['weekly_from']:%d.%m.%Y}–{row['weekly_to']:%d.%m.%Y}",
                details={"completed": bool(latest)},
            ))
            continue
        session_idx += 1
        documents = [
            {
                "type": doc["kind"], "id": doc["doc_id"],
                "positions": doc["positions"], "qty": doc["qty"],
                "value_kop": doc["value_kop"],
            }
            for doc in row["documents"]
        ]
        facts.append(_fact(
            f"inventory.session.{session_idx}", "inventory_adjustment",
            "Итог инвентаризации (оприходовано минус списано)",
            row["net_kop"], "kop", store=row["store"], period=row["day"].isoformat(),
            details={
                "loss_kop": row["loss_kop"], "enter_kop": row["enter_kop"],
                "loss_qty": row["loss_qty"], "enter_qty": row["enter_qty"],
                "missing_prices": row["missing_prices"], "documents": documents,
                "quantity_anomalies": row["quantity_anomalies"],
                "assessment": row["assessment"], "check": row["check"],
                "stated_reasons": row["descriptions"],
            },
        ))

    if client is not None:
        try:
            from .report_audit import build_audit_report
            audit = build_audit_report(client, d_from, d_to)
            audit_kind = "modified"
            current_event: dict[str, Any] | None = None

            def flush_audit_event() -> None:
                nonlocal current_event
                if not current_event:
                    return
                event_idx = current_event["idx"]
                document = current_event["document"]
                changes = current_event["changes"]
                evidence = document
                if changes:
                    evidence += "; " + "; ".join(changes)
                fact = _fact(
                    f"audit.{current_event['kind']}.{event_idx}",
                    "document_audit",
                    "Удалённый документ"
                    if current_event["kind"] == "deleted"
                    else "Изменённый документ",
                    1, "count", period=selected_label,
                    details={
                        "document": document,
                        "changes": changes,
                        "analysis_required": (
                            "Объяснить, что изменили, оценить риск, "
                            "назвать возможную причину только как гипотезу "
                            "и дать действие для проверки."
                        ),
                    },
                )
                fact["evidence"] = evidence
                facts.append(fact)
                current_event = None

            for idx, line in enumerate(audit.splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("🧾 ЗАКАЗЫ"):
                    flush_audit_event()
                    audit_kind = "sales_change"
                    continue
                if stripped.startswith("🗑 УДАЛЁННЫЕ"):
                    flush_audit_event()
                    audit_kind = "deleted"
                    continue
                if stripped.startswith("✏️ ИЗМЕНЁННЫЕ"):
                    flush_audit_event()
                    audit_kind = "modified"
                    continue
                if stripped.startswith("•"):
                    flush_audit_event()
                    current_event = {
                        "idx": idx, "kind": audit_kind,
                        "document": stripped.lstrip("• "), "changes": [],
                    }
                    continue
                if current_event and stripped.startswith("–"):
                    current_event["changes"].append(stripped.lstrip("– "))
            flush_audit_event()
        except Exception as exc:
            facts.append(_fact(
                "data.audit.unavailable", "data_quality", "Аудит документов недоступен",
                1, "count", period=selected_label, details={"error_type": type(exc).__name__},
            ))

    return _payload(
        "period", f"period-{d_from:%Y%m%d}-{d_to:%Y%m%d}",
        {"from": d_from.isoformat(), "to": d_to.isoformat(), "label": selected_label}, facts,
    )


def build_current_state_analysis_payload(conn, token: str | None = None) -> dict:
    """Факты PDF «Состояние на сегодня» без OCR и без новых формул."""
    from .report_current_state import (
        _STALE_DAYS, _last_receipts, _last_sales, _latest_day, _stock_rows,
        _stores_with_data, _zero_order_rows, _zero_order_rows_db,
    )

    snap_day = _latest_day(conn)
    facts: list[dict[str, Any]] = []
    last_sales = _last_sales(conn)
    last_receipts = _last_receipts(conn)
    stores = _stores_with_data(conn, snap_day)
    for store_idx, store_name in enumerate(stores, 1):
        rows = _stock_rows(conn, snap_day, store_name)
        total_physical = sum(float(r[3] or 0) for r in rows)
        total_reserve = sum(float(r[4] or 0) for r in rows)
        total_available = sum(float(r[5] or 0) for r in rows)
        for key, label, value in (
            ("sku", "Позиций СРЕЗКА", len(rows)),
            ("physical", "Физический остаток", total_physical),
            ("reserve", "Резерв", total_reserve),
            ("available", "Доступно без резерва", total_available),
        ):
            facts.append(_fact(
                f"stock.{store_idx}.{key}", "stock", label, value,
                "count" if key == "sku" else "qty", store=store_name,
                period=snap_day.isoformat(),
            ))
        for idx, row in enumerate(rows, 1):
            pid, name, group, physical, reserve, available = row[:6]
            if float(available or 0) < 0 or float(reserve or 0) > float(physical or 0):
                facts.append(_fact(
                    f"stock.{store_idx}.negative.{idx}", "stock_issue",
                    "Отрицательный доступный остаток", available, "qty",
                    store=store_name, period=snap_day.isoformat(),
                    details={
                        "product_id": pid, "product": name, "group": group,
                        "physical": physical, "reserve": reserve,
                    },
                ))
            if float(physical or 0) > 0:
                last_sale = last_sales.get(pid)
                days_idle = (snap_day - last_sale).days if last_sale else 9999
                if days_idle > _STALE_DAYS:
                    receipt = last_receipts.get(pid)
                    days_on_stock = (snap_day - receipt).days if receipt else None
                    facts.append(_fact(
                        f"stale.{store_idx}.{idx}", "stale_stock", "Дней без продаж",
                        days_idle if days_idle < 9999 else None, "days",
                        store=store_name, period=snap_day.isoformat(),
                        details={
                            "product_id": pid, "product": name, "group": group,
                            "physical": physical, "last_sale": last_sale,
                            "last_receipt": receipt, "days_on_stock": days_on_stock,
                            "threshold_days": _STALE_DAYS,
                        },
                    ))
        zero_rows = (
            _zero_order_rows(conn, token, snap_day, store_name)
            if token else _zero_order_rows_db(conn, snap_day, store_name)
        )
        for idx, row in enumerate(zero_rows, 1):
            pid, name, group, physical, reserve, available = row[:6]
            facts.append(_fact(
                f"zero.{store_idx}.{idx}", "zero_stock", "Ноль или минус к проверке заказа",
                available, "qty", store=store_name, period=snap_day.isoformat(),
                details={
                    "product_id": pid, "product": name, "group": group,
                    "physical": physical, "reserve": reserve,
                },
            ))

    # Тот же NEW engine и тот же горизонт, что в PDF «Состояние на сегодня».
    if token:
        try:
            from . import config
            from .calc_forecast import SREZKA_STORE_CONFIGS, build_forecast_new
            today = config.msk_today()
            monday = today - timedelta(days=today.weekday())
            horizon_from = monday + timedelta(days=7)
            horizon_to = horizon_from + timedelta(days=6)
            forecast_rows = build_forecast_new(
                conn, token, SREZKA_STORE_CONFIGS,
                cutoff_date=today, horizon_from=horizon_from, horizon_to=horizon_to,
            )
            forecast_payload = build_forecast_analysis_payload(
                [row for row in forecast_rows if row.store_name in stores],
                horizon_from, horizon_to,
            )
            for fact in forecast_payload["facts"]:
                cloned = dict(fact)
                cloned["id"] = f"current.{fact['id']}"
                facts.append(cloned)
        except Exception as exc:
            facts.append(_fact(
                "data.current_forecast.unavailable", "data_quality",
                "NEW-прогноз недоступен для AI", 1, "count",
                period=snap_day.isoformat(), details={"error_type": type(exc).__name__},
            ))
    return _payload(
        "current_state", f"current-state-{snap_day:%Y%m%d}",
        {"from": snap_day.isoformat(), "to": snap_day.isoformat(), "label": snap_day.strftime("%d.%m.%Y")},
        facts,
    )


def build_forecast_analysis_payload(
    results: Iterable[Any], horizon_from: date, horizon_to: date,
) -> dict:
    """Факты непосредственно из готовых строк NEW engine."""
    facts: list[dict[str, Any]] = []
    rows = list(results)
    order_qty = sum(float(r.recommended_order_qty or 0) for r in rows)
    facts.append(_fact("forecast.total.rows", "forecast", "Позиций в расчёте", len(rows), "count"))
    facts.append(_fact("forecast.total.order_qty", "forecast", "Всего к заказу", order_qty, "qty"))
    for idx, row in enumerate(rows, 1):
        flags = [getattr(flag, "value", str(flag)) for flag in (row.data_quality_flags or ())]
        details = {
            "product_id": row.product_id,
            "product": row.product_name,
            "known_order_demand": row.known_order_demand,
            "statistical_demand": row.statistical_demand,
            "statistical_residual": row.statistical_residual,
            "expected_demand": row.expected_demand,
            "physical": row.stock_all,
            "reserve": row.reserve_qty,
            "available": row.available_stock,
            "raw_order": row.raw_order_qty,
            "recommended_order": row.recommended_order_qty,
            "pack_size": row.pack_size,
            "flags": flags,
            "model": row.model_name,
            "demand_source": _plain(row.demand_source),
        }
        facts.append(_fact(
            f"forecast.row.{idx}", "forecast_product", "Прогноз позиции",
            row.recommended_order_qty, "qty", store=row.store_name,
            period=f"{horizon_from.isoformat()}–{horizon_to.isoformat()}", details=details,
        ))
    return _payload(
        "forecast", f"forecast-{horizon_from:%Y%m%d}-{horizon_to:%Y%m%d}",
        {"from": horizon_from.isoformat(), "to": horizon_to.isoformat(),
         "label": f"{horizon_from:%d.%m.%Y}–{horizon_to:%d.%m.%Y}"}, facts,
    )


def combine_daily_payload(period_payload: dict, state_payload: dict) -> dict:
    """Один ежедневный взгляд: вчерашний результат + сегодняшнее состояние."""
    facts = []
    for prefix, payload in (("daily.period", period_payload), ("daily.state", state_payload)):
        for fact in payload.get("facts", []):
            cloned = dict(fact)
            cloned["id"] = f"{prefix}.{fact['id']}"
            facts.append(cloned)
    period = {
        "from": period_payload["period"]["from"],
        "to": state_payload["period"]["to"],
        "label": f"Итоги {period_payload['period']['label']} и состояние {state_payload['period']['label']}",
    }
    return _payload("daily", f"daily-{period_payload['period']['to'].replace('-', '')}", period, facts)
