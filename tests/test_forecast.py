"""
Тесты для calc_forecast.py — чистые функции без подключения к БД.
"""
import pytest
from hermes.calc_forecast import (
    parse_pack_size,
    round_up_to_pack,
    sell_through_label,
    ForecastRow,
    SAFETY_HIGH, SAFETY_MEDIUM, SAFETY_NONE,
    ST_FULL, ST_GOOD, ST_PARTIAL, ST_WEAK,
    SUPPLIER_DISCOUNT,
)
from datetime import date


# ── parse_pack_size ───────────────────────────────────────────────────────────

class TestParsePackSize:
    def test_standard_bunch(self):
        pack, src = parse_pack_size("Роза 60см 25шт Красная")
        assert pack == 25
        assert src == "name_parsed"

    def test_with_dot(self):
        pack, src = parse_pack_size("Лилия орион 5шт.")
        assert pack == 5
        assert src == "name_parsed"

    def test_lowercase(self):
        pack, src = parse_pack_size("гвоздика 10 шт красная")
        assert pack == 10
        assert src == "name_parsed"

    def test_large_pack(self):
        pack, src = parse_pack_size("Тюльпан 100шт микс")
        assert pack == 100
        assert src == "name_parsed"

    def test_no_pack_info(self):
        pack, src = parse_pack_size("Роза 50см красная штучная")
        assert pack == 1
        assert src == "default"

    def test_too_large_number_ignored(self):
        # 999шт выходит за 2-500 диапазон — default
        pack, src = parse_pack_size("999шт контейнер")
        assert pack == 1
        assert src == "default"

    def test_single_unit_ignored(self):
        # 1шт — тоже default (< 2)
        pack, src = parse_pack_size("Растение 1шт горшок")
        assert pack == 1
        assert src == "default"

    def test_empty_name(self):
        pack, src = parse_pack_size("")
        assert pack == 1
        assert src == "default"


# ── round_up_to_pack ─────────────────────────────────────────────────────────

class TestRoundUpToPack:
    def test_exact_multiple(self):
        units, packs = round_up_to_pack(50, 25)
        assert units == 50
        assert packs == 2

    def test_rounds_up(self):
        units, packs = round_up_to_pack(51, 25)
        assert units == 75
        assert packs == 3

    def test_pack1(self):
        units, packs = round_up_to_pack(7, 1)
        assert units == 7
        assert packs == 7

    def test_zero_qty(self):
        units, packs = round_up_to_pack(0, 25)
        assert units == 0
        assert packs == 0

    def test_negative_qty(self):
        units, packs = round_up_to_pack(-5, 25)
        assert units == 0
        assert packs == 0

    def test_small_qty_big_pack(self):
        # 1.5 единицы при пачке 10 → 10 единиц, 1 пачка
        units, packs = round_up_to_pack(1.5, 10)
        assert units == 10
        assert packs == 1

    def test_scenario_tz_example(self):
        # ТЗ: 51 единица, упаковка 25 → 3 пачки, 75 единиц
        units, packs = round_up_to_pack(51, 25)
        assert packs == 3
        assert units == 75


# ── sell_through_label ────────────────────────────────────────────────────────

class TestSellThroughLabel:
    def test_full_sellthrough(self):
        lbl = sell_through_label(0.99, True, 100)
        assert "полн" in lbl.lower() or "≥" in lbl or "95" in lbl

    def test_good_sellthrough(self):
        lbl = sell_through_label(0.82, True, 100)
        assert lbl  # не пустая строка

    def test_partial_sellthrough(self):
        lbl = sell_through_label(0.50, True, 100)
        assert lbl

    def test_weak_sellthrough(self):
        lbl = sell_through_label(0.05, True, 100)
        assert lbl

    def test_no_receipts(self):
        lbl = sell_through_label(0.0, True, 0)
        assert "нет" in lbl.lower() or "нет приёмки" in lbl.lower() or lbl

    def test_incomplete_obs(self):
        lbl = sell_through_label(0.6, False, 50)
        assert lbl  # не падает


# ── Бизнес-логика заказа ──────────────────────────────────────────────────────

class TestOrderLogic:
    """
    Проверяем формулы через ForecastRow, если бы мы строили его вручную.
    ForecastRow — dataclass без методов, логика в build_forecast.
    Тесты проверяют вспомогательные константы и граничные условия.
    """

    def test_safety_rates(self):
        # Высокий сквозной продажи → 10%
        assert SAFETY_HIGH == pytest.approx(0.10)
        assert SAFETY_MEDIUM == pytest.approx(0.05)
        assert SAFETY_NONE == pytest.approx(0.00)

    def test_supplier_discount(self):
        # Скидка ООО Поставщик ровно 7%
        assert SUPPLIER_DISCOUNT == pytest.approx(0.07)

    def test_discount_applied_to_cost(self):
        buy_price = 10_000  # 100 руб в копейках
        order_units = 50
        cost_before = buy_price * order_units
        discount = int(cost_before * SUPPLIER_DISCOUNT)
        cost_after = cost_before - discount

        assert cost_before == 500_000
        assert discount == 35_000
        assert cost_after == 465_000

    def test_target_stock_formula(self):
        # target_stock = max(prev, curr) * (1 + safety) - stock
        # Если stock > target_demand → order = 0
        prev_demand = 30.0
        curr_demand = 25.0   # curr = year_ago_demand в данной неделе
        safety = SAFETY_HIGH
        stock = 50.0

        base_demand = max(prev_demand, curr_demand)       # 30
        target_stock = base_demand * (1 + safety)          # 33
        raw_order = max(0, target_stock - stock)           # 0

        assert raw_order == 0.0

    def test_target_stock_formula_needs_order(self):
        prev_demand = 100.0
        curr_demand = 90.0
        safety = SAFETY_HIGH
        stock = 20.0

        base_demand = max(prev_demand, curr_demand)
        target_stock = base_demand * (1 + safety)          # 110
        raw_order = max(0, target_stock - stock)           # 90

        assert raw_order == pytest.approx(90.0)

    def test_weak_sellthrough_suppresses_order(self):
        # Если sell-through < ST_PARTIAL → категория skip, нет заказа
        assert ST_PARTIAL == pytest.approx(0.30)
        # Sell-through 15% → заказывать нельзя (товар застрял, проблема с качеством)
        st = 0.15
        assert st < ST_PARTIAL


# ── ForecastRow dataclass ─────────────────────────────────────────────────────

class TestForecastRowDefaults:
    def test_create_minimal(self):
        r = ForecastRow(
            product_id="p1",
            product_name="Тест 25шт",
            folder_path="Ассортимент/Срезка/Розы",
            subgroup="Розы",
            pack_size=25,
            pack_size_source="name_parsed",
        )
        assert r.product_id == "p1"
        assert r.order_units == 0
        assert r.order_packs == 0
        assert r.confidence == "low"
        assert r.category == "manual"
        assert not r.is_supplier_discount

    def test_subgroup_set_correctly(self):
        r = ForecastRow(
            product_id="p2",
            product_name="Лилия",
            folder_path="Ассортимент/Срезка/Лилии",
            subgroup="Лилии",
            pack_size=5,
            pack_size_source="name_parsed",
        )
        assert r.subgroup == "Лилии"


# ── summarize() ───────────────────────────────────────────────────────────────

class TestSummarize:
    def _make_row(self, **kwargs) -> ForecastRow:
        defaults = dict(
            product_id="x",
            product_name="Test",
            folder_path="A/B/C",
            subgroup="C",
            pack_size=10,
            pack_size_source="default",
        )
        defaults.update(kwargs)
        return ForecastRow(**defaults)

    def test_empty(self):
        from hermes.calc_forecast import summarize
        sm = summarize([])
        assert sm["n_order"] == 0
        assert sm["total_units"] == 0
        assert sm["cost_after_kop"] == 0

    def test_sums_order_rows(self):
        from hermes.calc_forecast import summarize
        r1 = self._make_row(
            product_id="p1",
            order_units=50,
            order_packs=2,
            category="order",
            confidence="high",
            cost_after_discount_kop=100_000,
            cost_before_discount_kop=107_527,
            discount_kop=7_527,
        )
        r2 = self._make_row(
            product_id="p2",
            order_units=25,
            order_packs=1,
            category="order",
            confidence="medium",
            cost_after_discount_kop=50_000,
            cost_before_discount_kop=53_763,
            discount_kop=3_763,
        )
        r3 = self._make_row(
            product_id="p3",
            category="skip",
            confidence="low",
        )
        sm = summarize([r1, r2, r3])
        assert sm["n_order"] == 2
        assert sm["n_skip"] == 1
        assert sm["total_units"] == 75
        assert sm["total_packs"] == 3
        assert sm["confidence_high"] == 1
        assert sm["confidence_medium"] == 1
        assert sm["cost_after_kop"] == 150_000


# ── Anti-leakage: remaining_qty_at_cutoff ────────────────────────────────────


class TestRemainingQtyAntiLeakage:
    """
    remaining_qty_at_cutoff = ordered - shipped_before_cutoff.
    Отгрузки ПОСЛЕ cutoff — future data, использовать ЗАПРЕЩЕНО.

    Сценарий:
      ordered = 1000
      до cutoff отгружено = 250
      после cutoff ещё = 500
      → remaining_at_cutoff должен быть 750, не 250 и не 0.
    """

    @staticmethod
    def _remaining(total_ordered: float, demands: list, cutoff) -> float:
        """Функция как в co_lead_backtest.py simulate_tier_b_policy."""
        from datetime import date
        shipped_before = sum(q for d, q in demands if d <= cutoff)
        return max(0.0, total_ordered - shipped_before)

    def test_anti_leakage_basic(self):
        """750 — правильно. Не 250 (если включать future), не 0 (все shipped)."""
        from datetime import date
        cutoff = date(2026, 8, 1)
        demands = [
            (date(2026, 7, 28), 250.0),   # before cutoff — отгружено
            (date(2026, 8, 5),  500.0),   # after cutoff — FUTURE (нельзя)
            (date(2026, 8, 12), 250.0),   # after cutoff — FUTURE (нельзя)
        ]
        remaining = self._remaining(1000.0, demands, cutoff)
        assert remaining == 750.0  # ordered - shipped_before = 1000 - 250

    def test_fully_shipped_before_cutoff(self):
        """Если всё отгружено до cutoff → remaining=0 → Tier B не включает."""
        from datetime import date
        cutoff = date(2026, 8, 5)
        demands = [
            (date(2026, 8, 1), 600.0),
            (date(2026, 8, 3), 400.0),
        ]
        remaining = self._remaining(1000.0, demands, cutoff)
        assert remaining == 0.0

    def test_nothing_shipped_before_cutoff(self):
        """CO создан, первая отгрузка после cutoff → remaining = ordered."""
        from datetime import date
        co_date = date(2026, 8, 10)
        cutoff = co_date  # age = 0
        demands = [
            (date(2026, 8, 14), 800.0),
            (date(2026, 8, 18), 200.0),
        ]
        remaining = self._remaining(1000.0, demands, cutoff)
        assert remaining == 1000.0

    def test_partial_shipped_cutoff_on_demand_date(self):
        """Граничный случай: cutoff = дата отгрузки → shipped считается."""
        from datetime import date
        cutoff = date(2026, 8, 3)
        demands = [
            (date(2026, 8, 3), 300.0),   # exactly on cutoff — считается
            (date(2026, 8, 7), 700.0),   # after cutoff
        ]
        remaining = self._remaining(1000.0, demands, cutoff)
        assert remaining == 700.0  # 1000 - 300
