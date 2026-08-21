from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pytest

from hermes.ai_analyst import (
    AIValidationError,
    render_telegram_answer,
    validate_question_response,
    validate_response,
)
from hermes.ai_question import parse_question_period
from hermes.analysis_payload import (
    _month_start_back,
    build_forecast_analysis_payload,
    combine_daily_payload,
)
from hermes.anomaly_rules import evaluate_payload


def _fact(fid, category, value=1, **details):
    return {
        "id": fid, "category": category, "label": fid, "value": value,
        "unit": "count", "store": None, "period": None,
        "evidence": f"{fid}: {value}", "details": details,
    }


def _payload(facts, report_type="current_state"):
    return {
        "report_id": "report-1", "report_type": report_type,
        "period": {"from": "2026-08-15", "to": "2026-08-15"},
        "facts": facts,
    }


def test_rules_flag_negative_stock_and_client_churn():
    payload = _payload([
        _fact("stock.neg", "stock_issue", -2, physical=1, reserve=3),
        _fact("client.one", "client_churn", 12, avg_check_kop=1_500_000),
    ])
    result = evaluate_payload(payload)
    ids = {item["id"] for item in result["signals"]}
    assert "negative_available" in ids
    assert "base_client_churn" in ids


def test_rules_include_stale_stock():
    payload = _payload([_fact("stale.1", "stale_stock", 8, days_on_stock=12)])
    ids = {item["id"] for item in evaluate_payload(payload)["signals"]}
    assert "stale_stock" in ids


def test_rules_flag_inventory_shortage_and_missing_weekly_check():
    payload = _payload([
        _fact("inventory.1", "inventory_adjustment", -25_000),
        _fact("inventory.missing", "inventory_missing", 0),
    ], report_type="period")
    ids = {item["id"] for item in evaluate_payload(payload)["signals"]}
    assert "inventory_shortage" in ids
    assert "inventory_missing" in ids


def test_rules_prioritize_bad_inventory_input_over_false_shortage():
    fact = _fact("inventory.bad", "inventory_adjustment", -3_200_000_000)
    fact["details"]["quantity_anomalies"] = [{"product": "Лента", "qty": 499_972}]
    ids = {item["id"] for item in evaluate_payload(_payload([fact]))["signals"]}
    assert "inventory_input_anomaly" in ids
    assert "inventory_shortage" not in ids


def test_rules_flag_uncovered_customer_order():
    payload = _payload([{
        **_fact("forecast.row.1", "forecast_product", 0),
        "details": {
            "known_order_demand": 20, "statistical_demand": 5,
            "available": 0, "recommended_order": 0, "flags": [],
        },
    }], report_type="forecast")
    ids = {item["id"] for item in evaluate_payload(payload)["signals"]}
    assert "uncovered_customer_orders" in ids
    assert "forecast_order_discrepancy" in ids


def test_validator_requires_real_fact_ids_and_rebuilds_evidence():
    payload = _payload([_fact("stock.neg", "stock_issue", -2)])
    rules = evaluate_payload(payload)
    response = {
        "status": "ok", "report_id": "report-1", "report_type": "current_state",
        "findings": [{
            "severity": "critical", "title": "Проверить резерв",
            "fact_ids": ["stock.neg"], "evidence": "модель придумала",
            "why_it_matters": "Доступный остаток отрицательный.",
            "action": "Сверить физический остаток и резерв.",
        }],
        "positive_changes": [], "data_warnings": [],
    }
    result = validate_response(response, payload, rules)
    assert result["findings"][0]["evidence"] == "stock.neg: -2"


def test_validator_rejects_unknown_fact_id():
    payload = _payload([_fact("stock.neg", "stock_issue", -2)])
    response = {
        "status": "ok", "report_id": "report-1", "report_type": "current_state",
        "findings": [{
            "severity": "critical", "title": "Проверить резерв",
            "fact_ids": ["invented"], "evidence": "",
            "why_it_matters": "Ошибка.", "action": "Проверить.",
        }],
        "positive_changes": [], "data_warnings": [],
    }
    with pytest.raises(AIValidationError):
        validate_response(response, payload, evaluate_payload(payload))


@dataclass
class _Forecast:
    product_id: str = "p1"
    product_name: str = "Роза тест"
    store_name: str = "База"
    known_order_demand: float = 25
    statistical_demand: float = 10
    statistical_residual: float = 0
    expected_demand: float = 25
    stock_all: float = 5
    reserve_qty: float = 2
    available_stock: float = 3
    raw_order_qty: float = 22
    recommended_order_qty: float = 25
    pack_size: int = 25
    data_quality_flags: tuple = ()
    model_name: str = "hybrid"
    demand_source: str = "hybrid"


def test_forecast_payload_preserves_new_engine_decomposition():
    payload = build_forecast_analysis_payload(
        [_Forecast()], date(2026, 8, 17), date(2026, 8, 23),
    )
    row = next(f for f in payload["facts"] if f["category"] == "forecast_product")
    assert row["details"]["known_order_demand"] == 25
    assert row["details"]["available"] == 3
    assert row["details"]["recommended_order"] == 25


def test_daily_payload_prefixes_fact_ids():
    period = {
        "schema_version": 1, "report_type": "period", "report_id": "p",
        "period": {"from": "2026-08-15", "to": "2026-08-15", "label": "15.08"},
        "facts": [_fact("period.rev", "financial", 10)],
    }
    state = {
        "schema_version": 1, "report_type": "current_state", "report_id": "s",
        "period": {"from": "2026-08-16", "to": "2026-08-16", "label": "16.08"},
        "facts": [_fact("stock.neg", "stock_issue", -1)],
    }
    daily = combine_daily_payload(period, state)
    assert {f["id"] for f in daily["facts"]} == {
        "daily.period.period.rev", "daily.state.stock.neg",
    }


def test_month_start_back_crosses_year_boundary():
    assert _month_start_back(date(2026, 1, 16), 1) == date(2025, 12, 1)
    assert _month_start_back(date(2026, 8, 16), 12) == date(2025, 8, 1)


def test_question_periods_in_russian_and_follow_up_context():
    today = date(2026, 8, 20)
    assert parse_question_period("что было вчера?", today) == (
        date(2026, 8, 19), date(2026, 8, 19),
    )
    assert parse_question_period("за прошлую неделю", today) == (
        date(2026, 8, 10), date(2026, 8, 16),
    )
    assert parse_question_period("за июль 2026", today) == (
        date(2026, 7, 1), date(2026, 7, 31),
    )
    assert parse_question_period(
        "а по БАЗЕ?", today, (date(2026, 7, 1), date(2026, 7, 31)),
    ) == (date(2026, 7, 1), date(2026, 7, 31))


def test_question_validator_rebuilds_evidence_and_rejects_invented_number():
    payload = {
        "report_id": "question-1", "report_type": "question",
        "period": {"from": "2026-08-01", "to": "2026-08-20"},
        "facts": [{
            **_fact("period.selected.rev", "financial", 1_500_000),
            "evidence": "Выручка: 15 000 ₽ · 01.08.2026–20.08.2026",
        }, {
            **_fact("period.selected.loss", "financial", 9_999_900),
            "evidence": "Списания: 99 999 ₽ · 01.08.2026–20.08.2026",
        }],
    }
    response = {
        "status": "ok", "report_id": "question-1", "report_type": "question",
        "answer": "Выручка составила 15 000 ₽.",
        "fact_ids": ["period.selected.rev"],
        "caveats": ["Период неполный. [period.selected.rev]"], "follow_up": "",
    }
    validated = validate_question_response(response, payload)
    assert validated["evidence"] == ["Выручка: 15 000 ₽ · 01.08.2026–20.08.2026"]
    assert validated["caveats"] == ["Период неполный."]
    bad = {**response, "answer": "Выручка составила 99 999 ₽."}
    with pytest.raises(AIValidationError):
        validate_question_response(bad, payload)


def test_question_renderer_is_compact_and_fact_backed():
    text = render_telegram_answer({"validated": {
        "answer": "Выручка выросла.", "evidence": ["Выручка: 15 000 ₽"],
        "caveats": [], "follow_up": "Сравнить по складам?",
    }})
    assert text.startswith("🧠 Ответ")
    assert "Подтверждение:" in text
    assert "Сравнить по складам?" in text
