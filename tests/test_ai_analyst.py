from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pytest

from hermes.ai_analyst import AIValidationError, validate_response
from hermes.analysis_payload import build_forecast_analysis_payload, combine_daily_payload
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
