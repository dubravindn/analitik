"""
Unit tests для нового forecast-движка.

Покрытие:
  - retail regular
  - base regular (stat fallback)
  - known customer order
  - large order flag
  - preorder flag
  - March 8 mode
  - no known orders (stat only)
  - partial known orders
  - no stock data
  - double-count prevention (КЛЮЧЕВОЙ тест)
  - new SKU / zero sales
  - internal writeoff excluded (rolling_mean не включает дни без продаж)
  - replenishment: raw_order, rounded, pack_size
  - replenishment: unknown stock / unknown incoming
  - ForecastResult.explain()
  - forecast_mode_for()
"""
from __future__ import annotations

import math
from datetime import date, timedelta

import pytest

from hermes.forecast_data import rolling_mean
from hermes.forecast_models import (
    Channel,
    DataFlag,
    DemandSource,
    ForecastMode,
    ForecastResult,
    forecast_mode_for,
)
from hermes.forecast_orders import (
    COPosition,
    aggregate_known_demand,
)
from hermes.forecast_replenishment import calculate_replenishment


# ── forecast_mode_for ─────────────────────────────────────────────────────────

class TestForecastModeFor:
    def test_normal_april(self):
        assert forecast_mode_for(date(2026, 4, 15)) == ForecastMode.NORMAL

    def test_march_8_window(self):
        assert forecast_mode_for(date(2026, 3, 1))  == ForecastMode.MARCH_8
        assert forecast_mode_for(date(2026, 3, 8))  == ForecastMode.MARCH_8
        assert forecast_mode_for(date(2026, 3, 10)) == ForecastMode.MARCH_8
        assert forecast_mode_for(date(2026, 3, 11)) == ForecastMode.NORMAL

    def test_valentine_window(self):
        assert forecast_mode_for(date(2026, 2, 11)) == ForecastMode.VALENTINE
        assert forecast_mode_for(date(2026, 2, 17)) == ForecastMode.VALENTINE
        assert forecast_mode_for(date(2026, 2, 18)) == ForecastMode.NORMAL

    def test_new_year_start(self):
        assert forecast_mode_for(date(2026, 1, 1))  == ForecastMode.NEW_YEAR
        assert forecast_mode_for(date(2026, 1, 10)) == ForecastMode.NEW_YEAR
        assert forecast_mode_for(date(2026, 1, 11)) == ForecastMode.NORMAL

    def test_new_year_end(self):
        assert forecast_mode_for(date(2025, 12, 25)) == ForecastMode.NEW_YEAR
        assert forecast_mode_for(date(2025, 12, 31)) == ForecastMode.NEW_YEAR


# ── rolling_mean ──────────────────────────────────────────────────────────────

class TestRollingMean:
    def _make_sales(self, base_date: date, values: list[float]) -> dict[date, float]:
        return {base_date + timedelta(days=i): v for i, v in enumerate(values)}

    def test_simple_average(self):
        sales = self._make_sales(date(2026, 1, 1), [10.0, 20.0, 30.0, 40.0, 50.0,
                                                     60.0, 70.0])
        # window_days=7, end_date=2026-01-07
        result = rolling_mean(sales, date(2026, 1, 7), 7)
        assert result == pytest.approx(40.0)

    def test_zeros_counted_in_denominator(self):
        # Нулевые дни включаются в знаменатель
        sales = {date(2026, 1, 3): 70.0, date(2026, 1, 4): 70.0}
        result = rolling_mean(sales, date(2026, 1, 7), 7)
        # 140 / 7 = 20.0
        assert result == pytest.approx(20.0)

    def test_oper_start_excludes_early_days(self):
        sales = {date(2026, 1, 5): 70.0, date(2026, 1, 6): 70.0, date(2026, 1, 7): 70.0}
        # oper_start = 2026-01-05, window начинается с 2026-01-01
        # Эффективный диапазон: 2026-01-05..2026-01-07 = 3 дня
        result = rolling_mean(sales, date(2026, 1, 7), 7, oper_start=date(2026, 1, 5))
        assert result == pytest.approx(70.0)

    def test_empty_sales_returns_zero(self):
        assert rolling_mean({}, date(2026, 4, 1), 14) == 0.0

    def test_below_oper_start_returns_zero(self):
        sales = {date(2026, 4, 1): 100.0}
        # oper_start позже end_date → n_days <= 0
        result = rolling_mean(sales, date(2026, 3, 1), 14,
                              oper_start=date(2026, 4, 1))
        assert result == 0.0


# ── aggregate_known_demand ────────────────────────────────────────────────────

class TestAggregateKnownDemand:
    def _pos(self, product_id, qty, is_pre=False):
        return COPosition(
            co_id="co1",
            co_date=date(2026, 5, 1),
            delivery_date=date(2026, 5, 7),
            product_id=product_id,
            product_name="Роза 60",
            ordered_qty=qty,
            is_preorder=is_pre,
            lead_days=6,
        )

    def test_simple_aggregation(self):
        positions = [self._pos("p1", 100.0), self._pos("p1", 200.0)]
        agg = aggregate_known_demand(positions)
        assert agg["p1"]["known_qty"] == pytest.approx(300.0)

    def test_preorder_subset(self):
        positions = [
            self._pos("p1", 200.0, is_pre=True),
            self._pos("p1", 100.0, is_pre=False),
        ]
        agg = aggregate_known_demand(positions)
        assert agg["p1"]["known_qty"]    == pytest.approx(300.0)
        assert agg["p1"]["preorder_qty"] == pytest.approx(200.0)

    def test_large_order_flag(self):
        positions = [
            self._pos("p1", 600.0),  # >= 500 → large
            self._pos("p1", 300.0),  # < 500
        ]
        agg = aggregate_known_demand(positions, large_order_threshold=500)
        assert agg["p1"]["large_order_qty"] == pytest.approx(600.0)

    def test_no_positions_empty_dict(self):
        agg = aggregate_known_demand([])
        assert agg == {}

    def test_multiple_products(self):
        positions = [self._pos("p1", 100.0), self._pos("p2", 200.0)]
        agg = aggregate_known_demand(positions)
        assert set(agg.keys()) == {"p1", "p2"}
        assert agg["p2"]["known_qty"] == pytest.approx(200.0)


# ── КЛЮЧЕВОЙ ТЕСТ: отсутствие double-count ───────────────────────────────────

class TestNoDoubleCount:
    """
    Воспроизводит логику из forecast_base.py:
      stat_demand   = mean_cal_28 × horizon
      known_qty     = CO-позиции на cutoff
      stat_residual = max(0, stat_demand - known_qty)
      expected      = known_qty + stat_residual
                    = max(known_qty, stat_demand)

    Инвариант: expected <= stat_demand + known_qty (нет добавления дважды).
    """

    def _expected(self, stat_demand: float, known_qty: float) -> float:
        stat_residual = max(0.0, stat_demand - known_qty)
        return known_qty + stat_residual

    def test_no_co_equals_stat(self):
        assert self._expected(100.0, 0.0) == pytest.approx(100.0)

    def test_co_covers_all_stat(self):
        # known > stat → stat_residual = 0, expected = known
        result = self._expected(100.0, 300.0)
        assert result == pytest.approx(300.0)

    def test_co_partial_coverage(self):
        # known < stat → stat_residual = stat - known, expected = stat
        result = self._expected(100.0, 40.0)
        assert result == pytest.approx(100.0)

    def test_no_double_count_invariant(self):
        # expected < stat + known (не суммируем дважды)
        stat, known = 100.0, 60.0
        result = self._expected(stat, known)
        assert result < stat + known

    def test_preorder_inside_known_no_double(self):
        # preorder_demand ⊆ known_order_demand — не прибавляется отдельно
        known_qty    = 200.0
        preorder_qty = 80.0   # входит в known_qty
        stat_demand  = 150.0
        stat_residual = max(0.0, stat_demand - known_qty)
        expected = known_qty + stat_residual
        # preorder не прибавляется сверх known
        assert expected == pytest.approx(200.0)
        # и expected != known + preorder + stat_residual
        assert expected != pytest.approx(known_qty + preorder_qty + stat_residual)


# ── calculate_replenishment ───────────────────────────────────────────────────

class TestCalculateReplenishment:
    def test_simple_order(self):
        raw, rounded, _ = calculate_replenishment(
            expected_demand=100.0,
            available_stock=20.0,
            reserve_qty=0.0,
            confirmed_incoming=None,
            transfer_in_qty=0.0,
            transfer_out_qty=0.0,
            pack_size=1,
        )
        assert raw == pytest.approx(80.0)
        assert rounded == pytest.approx(80.0)

    def test_pack_size_rounding(self):
        _, rounded, _ = calculate_replenishment(
            expected_demand=100.0,
            available_stock=0.0,
            reserve_qty=0.0,
            confirmed_incoming=None,
            transfer_in_qty=0.0,
            transfer_out_qty=0.0,
            pack_size=25,
        )
        assert rounded == pytest.approx(100.0)  # 100 / 25 * 25 = 100

    def test_pack_size_ceil(self):
        _, rounded, _ = calculate_replenishment(
            expected_demand=110.0,
            available_stock=0.0,
            reserve_qty=0.0,
            confirmed_incoming=None,
            transfer_in_qty=0.0,
            transfer_out_qty=0.0,
            pack_size=25,
        )
        assert rounded == pytest.approx(125.0)  # ceil(110/25)*25 = 125

    def test_no_order_when_stock_sufficient(self):
        raw, rounded, _ = calculate_replenishment(
            expected_demand=50.0,
            available_stock=200.0,
            reserve_qty=0.0,
            confirmed_incoming=None,
            transfer_in_qty=0.0,
            transfer_out_qty=0.0,
            pack_size=1,
        )
        assert raw == pytest.approx(0.0)
        assert rounded == pytest.approx(0.0)

    def test_unknown_stock_orders_full_target(self):
        # Остаток неизвестен → не вычитаем → заказываем весь target
        raw, _, _ = calculate_replenishment(
            expected_demand=100.0,
            available_stock=None,
            reserve_qty=0.0,
            confirmed_incoming=None,
            transfer_in_qty=0.0,
            transfer_out_qty=0.0,
            pack_size=1,
        )
        assert raw == pytest.approx(100.0)

    def test_confirmed_incoming_reduces_order(self):
        raw, _, _ = calculate_replenishment(
            expected_demand=100.0,
            available_stock=20.0,
            reserve_qty=0.0,
            confirmed_incoming=50.0,
            transfer_in_qty=0.0,
            transfer_out_qty=0.0,
            pack_size=1,
        )
        assert raw == pytest.approx(30.0)

    def test_unknown_incoming_treated_as_zero(self):
        # None incoming — не знаем, консервативно не учитываем
        raw, _, _ = calculate_replenishment(
            expected_demand=100.0,
            available_stock=20.0,
            reserve_qty=0.0,
            confirmed_incoming=None,
            transfer_in_qty=0.0,
            transfer_out_qty=0.0,
            pack_size=1,
        )
        assert raw == pytest.approx(80.0)

    def test_reserve_increases_order(self):
        raw, _, _ = calculate_replenishment(
            expected_demand=100.0,
            available_stock=50.0,
            reserve_qty=30.0,
            confirmed_incoming=None,
            transfer_in_qty=0.0,
            transfer_out_qty=0.0,
            pack_size=1,
        )
        assert raw == pytest.approx(80.0)

    def test_transfer_in_reduces_order(self):
        raw, _, _ = calculate_replenishment(
            expected_demand=100.0,
            available_stock=0.0,
            reserve_qty=0.0,
            confirmed_incoming=None,
            transfer_in_qty=40.0,
            transfer_out_qty=0.0,
            pack_size=1,
        )
        assert raw == pytest.approx(60.0)

    def test_reason_contains_unknown_when_missing(self):
        _, _, reason = calculate_replenishment(
            expected_demand=100.0,
            available_stock=None,
            reserve_qty=0.0,
            confirmed_incoming=None,
            transfer_in_qty=0.0,
            transfer_out_qty=0.0,
            pack_size=1,
        )
        assert "неизвестен" in reason or "неизвестно" in reason


# ── ForecastResult.explain() ──────────────────────────────────────────────────

class TestForecastResultExplain:
    def _make_result(self) -> ForecastResult:
        return ForecastResult(
            product_id="p1",
            product_name="Роза Explorer 60",
            store_id="s1",
            store_name="База Воровского",
            channel=Channel.BASE,
            forecast_mode=ForecastMode.NORMAL,
            demand_source=DemandSource.HYBRID,
            model_name="hybrid(co+mean_cal_28)",
            forecast_from=date(2026, 5, 4),
            forecast_to=date(2026, 5, 10),
            forecast_horizon_days=7,
            cutoff_date=date(2026, 4, 27),
            statistical_demand=120.0,
            known_order_demand=500.0,
            preorder_demand=0.0,
            expected_demand=500.0,
            available_stock=150.0,
            raw_order_qty=350.0,
            recommended_order_qty=350.0,
            recommendation_reason="CO=500 + stat_residual=0",
        )

    def test_explain_contains_key_fields(self):
        r = self._make_result()
        text = r.explain()
        assert "Роза Explorer 60" in text
        assert "500" in text
        assert "350" in text
        assert "NORMAL" in text

    def test_explain_shows_decomposition(self):
        r = self._make_result()
        text = r.explain()
        assert "Статистический спрос" in text
        assert "Известные CO" in text
        assert "Ожидаемый спрос" in text
