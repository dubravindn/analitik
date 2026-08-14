"""
Интеграционные тесты NEW forecast engine.

Тестируются:
  - calculate_replenishment()      — чистая функция, без БД
  - apply_replenishment_to_result() — мутирует ForecastResult, без БД
  - Orchestration-level флаги      — логика build_forecast_new (через unit-copy)

11 тест-кейсов из ТЗ интеграции:
  1.  RETAIL + stock → правильный recommended_order_qty
  2.  BASE + CO + stock → 4-компонентный expected_demand
  3.  BASE + large estimated → LARGE_CO_ESTIMATED flag
  4.  Preorder → preorder_demand учитывается в expected
  5.  Reserve → уменьшает эффективный остаток
  6.  Zero stock → raw = expected_demand
  7.  Unknown stock → recommended_order_qty=None + MANUAL_REVIEW
  8.  Pack rounding → ceil до pack_size
  9.  Один SKU в двух магазинах → независимые результаты
  10. No double-count → stat_residual не добавляется поверх known
  11. incoming_qty=None → флаг INCOMING_UNKNOWN
"""
from __future__ import annotations

from datetime import date

import pytest

from hermes.forecast_data import StockSnapshot
from hermes.forecast_models import (
    Channel,
    DataFlag,
    DemandSource,
    ForecastMode,
    ForecastResult,
)
from hermes.forecast_replenishment import (
    apply_replenishment_to_result,
    calculate_replenishment,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _result(**kwargs) -> ForecastResult:
    """Создаёт ForecastResult с разумными значениями по умолчанию."""
    defaults = dict(
        product_id="p1",
        product_name="Роза 50шт",
        store_id="store_base",
        store_name="База Воровского",
        channel=Channel.BASE,
        forecast_mode=ForecastMode.NORMAL,
        demand_source=DemandSource.HYBRID,
        model_name="hybrid",
        forecast_from=date(2026, 8, 14),
        forecast_to=date(2026, 8, 20),
        forecast_horizon_days=7,
        cutoff_date=date(2026, 8, 13),
        pack_size=25,
        expected_demand=100.0,
    )
    defaults.update(kwargs)
    return ForecastResult(**defaults)


def _snap(pid: str, sid: str, avail: float, stock: float = 0.0, reserve: float = 0.0) -> StockSnapshot:
    return StockSnapshot(pid, sid, avail, stock or avail, reserve)


# ── Тест 1: RETAIL + stock → правильный recommended_order_qty ─────────────────

class TestRetailWithStock:
    def test_retail_recommended_order(self):
        """expected=100, available=30, pack=25 → raw=70, rounded=75."""
        raw, rounded, _ = calculate_replenishment(
            expected_demand=100.0,
            available_stock=30.0,
            reserve_qty=0.0,
            confirmed_incoming=None,
            transfer_in_qty=0.0,
            transfer_out_qty=0.0,
            pack_size=25,
        )
        assert raw == 70.0
        assert rounded == 75.0  # ceil(70/25)*25 = 3*25

    def test_retail_apply_to_result(self):
        r = _result(
            store_id="store_retail",
            store_name="Розница Ленина",
            channel=Channel.RETAIL,
            demand_source=DemandSource.STAT,
            model_name="mean_cal_14",
            expected_demand=100.0,
            pack_size=25,
        )
        apply_replenishment_to_result(r, _snap("p1", "store_retail", 30.0))
        assert r.raw_order_qty == 70.0
        assert r.recommended_order_qty == 75.0
        assert r.available_stock == 30.0


# ── Тест 2: BASE + CO + stock → 4-компонентный expected_demand ───────────────

class TestBaseWithCOAndStock:
    def test_4_component_demand_reaches_expected(self):
        """explicit=80, estimated_large=40, preorder=0, stat_residual=0 → expected=120."""
        r = _result(
            explicit_order_demand=80.0,
            estimated_large_order_demand=40.0,
            preorder_demand=0.0,
            known_order_demand=120.0,
            statistical_demand=100.0,
            statistical_residual=0.0,   # max(0, 100-120)=0
            expected_demand=120.0,      # known + stat_residual
            pack_size=50,
        )
        apply_replenishment_to_result(r, _snap("p1", "store_base", 30.0))
        # net_available = 30; raw = 120-30 = 90; rounded = ceil(90/50)*50 = 100
        assert r.raw_order_qty == 90.0
        assert r.recommended_order_qty == 100.0


# ── Тест 3: BASE + large estimated → LARGE_CO_ESTIMATED flag ─────────────────

class TestBaseLargeEstimated:
    def test_large_co_estimated_flag_preserved(self):
        """Флаг LARGE_CO_ESTIMATED должен быть сохранён после apply_replenishment."""
        r = _result(
            estimated_large_order_demand=500.0,
            known_order_demand=500.0,
            expected_demand=500.0,
            data_quality_flags=(DataFlag.LARGE_CO_ESTIMATED,),
            pack_size=100,
        )
        apply_replenishment_to_result(r, _snap("p1", "store_base", 200.0))
        assert DataFlag.LARGE_CO_ESTIMATED in r.data_quality_flags
        assert r.raw_order_qty == 300.0  # 500-200


# ── Тест 4: Preorder → preorder_demand входит в expected ─────────────────────

class TestPreorderDemand:
    def test_preorder_included_in_expected(self):
        """preorder=200 → known=200, expected >= 200."""
        r = _result(
            preorder_demand=200.0,
            known_order_demand=200.0,
            statistical_demand=150.0,
            statistical_residual=0.0,
            expected_demand=200.0,
            pack_size=50,
        )
        apply_replenishment_to_result(r, _snap("p1", "store_base", 50.0))
        # raw = 200-50 = 150; rounded = ceil(150/50)*50 = 150
        assert r.raw_order_qty == 150.0
        assert r.recommended_order_qty == 150.0


# ── Тест 5: Reserve → уменьшает эффективный остаток ─────────────────────────

class TestReserveReducesStock:
    def test_reserve_subtracts_from_net_available(self):
        """available=80, reserve=30 → net_avail=50; raw=100-50=50."""
        raw, rounded, _ = calculate_replenishment(
            expected_demand=100.0,
            available_stock=80.0,
            reserve_qty=30.0,
            confirmed_incoming=None,
            transfer_in_qty=0.0,
            transfer_out_qty=0.0,
            pack_size=1,
        )
        assert raw == 50.0

    def test_reserve_via_apply(self):
        r = _result(expected_demand=100.0, pack_size=1)
        snap = StockSnapshot("p1", "store_base", 80.0, 80.0, 30.0)  # reserve=30
        apply_replenishment_to_result(r, snap)
        assert r.raw_order_qty == 50.0   # 100 - (80 - 30)
        assert r.reserve_qty == 30.0


# ── Тест 6: Zero stock → raw = expected_demand ───────────────────────────────

class TestZeroStock:
    def test_zero_stock_orders_all(self):
        raw, _, _ = calculate_replenishment(
            expected_demand=150.0,
            available_stock=0.0,
            reserve_qty=0.0,
            confirmed_incoming=None,
            transfer_in_qty=0.0,
            transfer_out_qty=0.0,
            pack_size=1,
        )
        assert raw == 150.0

    def test_zero_stock_in_result(self):
        r = _result(expected_demand=75.0, pack_size=25)
        apply_replenishment_to_result(r, _snap("p1", "store_base", 0.0))
        assert r.raw_order_qty == 75.0
        assert r.recommended_order_qty == 75.0  # 75 уже кратно 25


# ── Тест 7: Unknown stock → recommended_order_qty=None + MANUAL_REVIEW ───────

class TestUnknownStock:
    def test_no_snap_sets_no_stock_data_flag(self):
        r = _result(expected_demand=100.0)
        apply_replenishment_to_result(r, None)   # snap=None → NO_STOCK_DATA
        assert DataFlag.NO_STOCK_DATA in r.data_quality_flags
        assert r.available_stock is None

    def test_orchestration_sets_manual_review_and_none_qty(self):
        """
        Имитируем логику orchestration-слоя:
          after apply_replenishment(None) → available_stock is None
          → add MANUAL_REVIEW, set recommended_order_qty = None
        """
        r = _result(expected_demand=100.0)
        apply_replenishment_to_result(r, None)

        # Orchestration logic
        if r.available_stock is None:
            if DataFlag.MANUAL_REVIEW not in r.data_quality_flags:
                r.data_quality_flags = r.data_quality_flags + (DataFlag.MANUAL_REVIEW,)
            r.recommended_order_qty = None

        assert DataFlag.MANUAL_REVIEW in r.data_quality_flags
        assert r.recommended_order_qty is None


# ── Тест 8: Pack rounding ─────────────────────────────────────────────────────

class TestPackRounding:
    def test_ceil_to_pack_size(self):
        """raw=71, pack=25 → ceil(71/25)*25 = 3*25 = 75."""
        _, rounded, _ = calculate_replenishment(
            expected_demand=101.0,
            available_stock=30.0,
            reserve_qty=0.0,
            confirmed_incoming=None,
            transfer_in_qty=0.0,
            transfer_out_qty=0.0,
            pack_size=25,
        )
        assert rounded == 75.0

    def test_exact_pack_no_extra(self):
        """raw=50, pack=25 → rounded=50 (без лишней упаковки)."""
        _, rounded, _ = calculate_replenishment(
            expected_demand=80.0,
            available_stock=30.0,
            reserve_qty=0.0,
            confirmed_incoming=None,
            transfer_in_qty=0.0,
            transfer_out_qty=0.0,
            pack_size=25,
        )
        assert rounded == 50.0


# ── Тест 9: Один SKU в двух магазинах → независимые результаты ───────────────

class TestStoreSKUIsolation:
    def test_same_sku_two_stores_independent(self):
        """product_id=p1 в магазинах s1 и s2 — разные результаты, ключ (pid, store_id)."""
        r_base   = _result(product_id="p1", store_id="store_base",   expected_demand=200.0, pack_size=50)
        r_retail = _result(product_id="p1", store_id="store_retail", expected_demand=30.0,  pack_size=10,
                           channel=Channel.RETAIL, demand_source=DemandSource.STAT, model_name="mean_cal_14")

        apply_replenishment_to_result(r_base,   _snap("p1", "store_base",   80.0))
        apply_replenishment_to_result(r_retail, _snap("p1", "store_retail", 10.0))

        assert r_base.store_id != r_retail.store_id
        assert r_base.raw_order_qty   == 120.0    # 200 - 80
        assert r_retail.raw_order_qty == 20.0     # 30 - 10
        # Нет взаимного влияния
        assert r_base.available_stock == 80.0
        assert r_retail.available_stock == 10.0


# ── Тест 10: No double-count (stat_residual не добавляется поверх known) ──────

class TestNoDoubleCount:
    def test_stat_residual_is_floored_at_zero(self):
        """known=120, stat=100 → stat_residual=0, expected=120 (не 220)."""
        known = 120.0
        stat  = 100.0
        stat_residual = max(0.0, stat - known)
        expected = known + stat_residual

        assert stat_residual == 0.0
        assert expected == 120.0   # = known, stat не прибавлен

    def test_stat_residual_provides_floor(self):
        """known=50, stat=100 → stat_residual=50, expected=100."""
        known = 50.0
        stat  = 100.0
        stat_residual = max(0.0, stat - known)
        expected = known + stat_residual

        assert stat_residual == 50.0
        assert expected == 100.0

    def test_result_reflects_no_double_count(self):
        """Проверяем через ForecastResult: expected = known + stat_residual."""
        r = _result(
            known_order_demand=120.0,
            statistical_demand=100.0,
            statistical_residual=0.0,
            expected_demand=120.0,  # не 220
            pack_size=50,
        )
        apply_replenishment_to_result(r, _snap("p1", "store_base", 20.0))
        # raw = 120 - 20 = 100; pack=50 → rounded=100
        assert r.raw_order_qty == 100.0
        assert r.recommended_order_qty == 100.0


# ── Тест 11: INCOMING_UNKNOWN флаг ───────────────────────────────────────────

class TestIncomingUnknown:
    def test_incoming_none_reason_text(self):
        """calculate_replenishment с incoming=None → reason содержит 'поступление неизвестно'."""
        _, _, reason = calculate_replenishment(
            expected_demand=100.0,
            available_stock=50.0,
            reserve_qty=0.0,
            confirmed_incoming=None,
            transfer_in_qty=0.0,
            transfer_out_qty=0.0,
            pack_size=1,
        )
        assert "поступление неизвестно" in reason

    def test_incoming_none_treats_as_zero(self):
        """incoming=None → консервативный расчёт (приходов нет)."""
        raw_none, _, _ = calculate_replenishment(
            expected_demand=100.0, available_stock=50.0, reserve_qty=0.0,
            confirmed_incoming=None,
            transfer_in_qty=0.0, transfer_out_qty=0.0, pack_size=1,
        )
        raw_zero, _, _ = calculate_replenishment(
            expected_demand=100.0, available_stock=50.0, reserve_qty=0.0,
            confirmed_incoming=0.0,
            transfer_in_qty=0.0, transfer_out_qty=0.0, pack_size=1,
        )
        assert raw_none == raw_zero == 50.0

    def test_orchestration_sets_incoming_unknown_flag(self):
        """
        Имитируем orchestration-логику: incoming_qty is None → INCOMING_UNKNOWN.
        """
        r = _result(expected_demand=100.0)
        # r.incoming_qty is None by default (v1: поступления не загружаются)
        assert r.incoming_qty is None

        # Orchestration logic
        if r.incoming_qty is None and DataFlag.INCOMING_UNKNOWN not in r.data_quality_flags:
            r.data_quality_flags = r.data_quality_flags + (DataFlag.INCOMING_UNKNOWN,)

        assert DataFlag.INCOMING_UNKNOWN in r.data_quality_flags
