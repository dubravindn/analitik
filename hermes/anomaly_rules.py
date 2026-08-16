"""Детерминированные сигналы, которые AI обязан учитывать.

Здесь нет генерации текста для владельца: правила лишь выделяют проверяемые
факты. Финальный приоритет и короткое объяснение формирует AI, но не может
игнорировать критические сигналы или придумывать причину.
"""
from __future__ import annotations

import statistics
from typing import Any


def _signal(
    signal_id: str, severity: str, title: str, fact_ids: list[str], reason: str,
) -> dict[str, Any]:
    return {
        "id": signal_id,
        "severity": severity,
        "title": title,
        "fact_ids": fact_ids,
        "reason": reason,
    }


def evaluate_payload(payload: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    facts = payload.get("facts") or []
    by_id = {fact.get("id"): fact for fact in facts if fact.get("id")}
    signals: list[dict[str, Any]] = []
    positives: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    negative = [f for f in facts if f.get("category") == "stock_issue"]
    if negative:
        signals.append(_signal(
            "negative_available", "critical", "Резерв превышает физический остаток",
            [f["id"] for f in negative[:5]],
            "Доступно меньше нуля; требуется проверить остаток и резерв.",
        ))

    zero_stock = [f for f in facts if f.get("category") == "zero_stock"]
    if zero_stock:
        signals.append(_signal(
            "zero_stock", "warning", "Товары с нулевым или отрицательным доступным остатком",
            [f["id"] for f in zero_stock[:5]],
            "Позиции входят в утверждённые группы контроля заказа.",
        ))

    audit = [f for f in facts if f.get("category") == "document_audit"]
    if audit:
        signals.append(_signal(
            "document_audit", "critical", "Есть изменённые или удалённые документы",
            [f["id"] for f in audit[:5]],
            "Документы изменялись после создания или были удалены; номера сохранены в фактах.",
        ))

    churn = [f for f in facts if f.get("category") == "client_churn"]
    if churn:
        signals.append(_signal(
            "base_client_churn", "warning", "Клиенты БАЗЫ с риском оттока",
            [f["id"] for f in churn[:5]],
            "Нет заказа более 10 дней при среднем чеке не ниже 10 000 ₽.",
        ))

    forecast_rows = [f for f in facts if f.get("category") == "forecast_product"]
    uncovered: list[dict] = []
    discrepancy: list[dict] = []
    manual: list[dict] = []
    for fact in forecast_rows:
        d = fact.get("details") or {}
        known = float(d.get("known_order_demand") or 0)
        stat = float(d.get("statistical_demand") or 0)
        available = d.get("available")
        recommended = d.get("recommended_order")
        flags = set(d.get("flags") or [])
        if known > 0 and (available is None or known > float(available or 0)):
            if recommended is None or float(recommended or 0) <= 0:
                uncovered.append(fact)
        if known > 0 and stat > 0 and (known >= stat * 2 or stat >= known * 2):
            discrepancy.append(fact)
        if "MANUAL_REVIEW" in flags or recommended is None:
            manual.append(fact)
    if uncovered:
        signals.append(_signal(
            "uncovered_customer_orders", "critical", "Заказы клиентов не покрыты остатком и закупкой",
            [f["id"] for f in uncovered[:5]],
            "Спрос из заказов выше доступного остатка, но итог к заказу пустой или нулевой.",
        ))
    if discrepancy:
        signals.append(_signal(
            "forecast_order_discrepancy", "warning", "Заказы клиентов сильно расходятся со статистикой",
            [f["id"] for f in discrepancy[:5]],
            "Один из компонентов спроса как минимум вдвое выше другого; нужна проверка контекста.",
        ))
    if manual:
        warnings.append(_signal(
            "forecast_manual_review", "data", "Прогноз требует ручной проверки",
            [f["id"] for f in manual[:5]],
            "NEW engine вернул MANUAL_REVIEW или не смог дать итог к заказу.",
        ))

    # Аномалия выручки: сравнение только с тем же днём недели за 8 прошлых недель.
    current = by_id.get("compare.day.rev.current")
    history = [
        f for fid, f in by_id.items()
        if str(fid).startswith("history.same_weekday.") and str(fid).endswith(".revenue")
    ]
    if current and len(history) >= 4:
        values = [float(f.get("value") or 0) for f in history]
        median = statistics.median(values)
        deviations = [abs(v - median) for v in values]
        mad = statistics.median(deviations)
        value = float(current.get("value") or 0)
        relative = abs(value - median) / abs(median) if median else 0
        unusual = relative >= 0.20 and (mad == 0 or abs(value - median) > 3 * mad)
        fact_ids = [current["id"]] + [f["id"] for f in history]
        if unusual and value < median:
            signals.append(_signal(
                "revenue_below_normal", "warning", "Выручка ниже обычного для этого дня недели",
                fact_ids, "Сравнение с медианой того же дня недели; отклонение выше 3 MAD и 20%.",
            ))
        elif unusual and value > median:
            positives.append(_signal(
                "revenue_above_normal", "positive", "Выручка выше обычного для этого дня недели",
                fact_ids, "Сравнение с медианой того же дня недели; отклонение выше 3 MAD и 20%.",
            ))

    for fact in facts:
        if fact.get("category") == "data_quality":
            warnings.append(_signal(
                f"data_{fact['id']}", "data", fact.get("label", "Проблема данных"),
                [fact["id"]], "Источник данных сообщил об ошибке или неполноте.",
            ))

    rank = {"critical": 0, "warning": 1, "info": 2}
    signals.sort(key=lambda s: rank.get(s["severity"], 9))
    return {"signals": signals, "positive_changes": positives, "data_warnings": warnings}
