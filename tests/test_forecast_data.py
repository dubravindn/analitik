"""
Тесты для hermes/forecast_data.py — чистые функции без подключения к БД.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

import pytest

from hermes.forecast_data import (
    DayRecord,
    ForecastFeatures,
    _sales_window,
    _v2_balance,
    _wts,
    build_forecast_features,
)


# ── Вспомогательные фикстуры ─────────────────────────────────────────

def _day(evidence="POSITIVE_STOCK_OBSERVED", sales=10.0, balance_ok=True,
         balance_err=0.0, flags=(), stock=100.0, anomaly=False, date_=None):
    return DayRecord(
        date=date_ or date(2026, 8, 7),
        product_id="p1",
        product_name="Тест 25шт",
        store_id="s1",
        store_name="Магазин",
        sales_qty=sales,
        stock_qty=stock,
        available_qty=stock,
        reserve_qty=0.0,
        supply_qty=0.0,
        move_in_qty=0.0,
        move_out_qty=0.0,
        loss_qty=0.0,
        enter_qty=0.0,
        snapshot_present=(evidence != "NO_SNAPSHOT_ROW"),
        snapshot_volume_anomaly=anomaly,
        balance_ok=balance_ok,
        balance_abs_error=balance_err,
        data_quality_flags=flags,
        availability_evidence=evidence,
    )


# ── DayRecord — базовая конструкция ──────────────────────────────────

class TestDayRecord:
    def test_positive_stock(self):
        r = _day("POSITIVE_STOCK_OBSERVED", sales=25)
        assert r.availability_evidence == "POSITIVE_STOCK_OBSERVED"
        assert r.snapshot_present is True
        assert r.sales_qty == 25

    def test_no_snapshot(self):
        r = _day("NO_SNAPSHOT_ROW", sales=0, balance_ok=None, balance_err=None,
                 stock=None, flags=("NO_SNAPSHOT",))
        assert r.availability_evidence == "NO_SNAPSHOT_ROW"
        assert r.snapshot_present is False
        assert r.stock_qty is None
        assert r.balance_ok is None

    def test_snapshot_unreliable(self):
        r = _day("SNAPSHOT_UNRELIABLE", anomaly=True,
                 flags=("SNAPSHOT_VOLUME_ANOMALY",))
        assert r.availability_evidence == "SNAPSHOT_UNRELIABLE"
        assert r.snapshot_volume_anomaly is True
        assert "SNAPSHOT_VOLUME_ANOMALY" in r.data_quality_flags

    def test_conflicting_evidence(self):
        r = _day("CONFLICTING_EVIDENCE", sales=15, stock=None,
                 flags=("NO_SNAPSHOT", "SALES_WITHOUT_SNAPSHOT"))
        assert r.availability_evidence == "CONFLICTING_EVIDENCE"
        assert "SALES_WITHOUT_SNAPSHOT" in r.data_quality_flags

    def test_frozen(self):
        r = _day()
        with pytest.raises((AttributeError, TypeError)):
            r.sales_qty = 99  # type: ignore[misc]

    def test_balance_mismatch_flag(self):
        r = _day(balance_ok=False, balance_err=30.0, flags=("BALANCE_MISMATCH",))
        assert r.balance_ok is False
        assert r.balance_abs_error == 30.0
        assert "BALANCE_MISMATCH" in r.data_quality_flags


# ── _wts — сумма движений в окне ────────────────────────────────────

class TestWts:
    def _ts(self, date_str: str, hour: int = 12):
        from datetime import datetime, timezone
        return datetime(int(date_str[:4]), int(date_str[5:7]), int(date_str[8:]),
                        hour, 0, 0, tzinfo=timezone.utc)

    def test_all_in_window(self):
        t0 = self._ts("2026-08-06", 5)
        t1 = self._ts("2026-08-07", 5)
        items = [(self._ts("2026-08-06", 12), 100.0),
                 (self._ts("2026-08-06", 15), 50.0)]
        assert _wts(items, t0, t1) == 150.0

    def test_before_window_excluded(self):
        t0 = self._ts("2026-08-06", 5)
        t1 = self._ts("2026-08-07", 5)
        items = [(self._ts("2026-08-05", 20), 200.0),
                 (self._ts("2026-08-06", 12), 30.0)]
        assert _wts(items, t0, t1) == 30.0

    def test_after_window_excluded(self):
        t0 = self._ts("2026-08-06", 5)
        t1 = self._ts("2026-08-07", 5)
        items = [(self._ts("2026-08-07", 9), 100.0)]
        assert _wts(items, t0, t1) == 0.0

    def test_boundary_inclusive_at_t1(self):
        t0 = self._ts("2026-08-06", 5)
        t1 = self._ts("2026-08-07", 5)
        items = [(self._ts("2026-08-07", 5), 40.0)]  # ровно T1
        assert _wts(items, t0, t1) == 40.0

    def test_boundary_exclusive_at_t0(self):
        t0 = self._ts("2026-08-06", 5)
        t1 = self._ts("2026-08-07", 5)
        items = [(self._ts("2026-08-06", 5), 40.0)]  # ровно T0 — НЕ в окне
        assert _wts(items, t0, t1) == 0.0

    def test_empty(self):
        t0 = self._ts("2026-08-06", 5)
        t1 = self._ts("2026-08-07", 5)
        assert _wts([], t0, t1) == 0.0


# ── _sales_window — V2 назначение продаж ────────────────────────────

class TestSalesWindow:
    def _make_sales(self, pairs):
        d = defaultdict(float)
        for pid, sid, day_str, qty in pairs:
            d[(pid, sid, date.fromisoformat(day_str))] = qty
        return d

    def test_1day_window_includes_d0_excludes_d1(self):
        # d0=Aug6, d1=Aug7 → включить Aug6, исключить Aug7
        sales = self._make_sales([
            ("p", "s", "2026-08-06", 80.0),
            ("p", "s", "2026-08-07", 50.0),
        ])
        result = _sales_window(sales, "p", "s", date(2026, 8, 6), date(2026, 8, 7))
        assert result == 80.0

    def test_2day_window(self):
        # d0=Aug6, d1=Aug8 → включить Aug6, Aug7; исключить Aug8
        sales = self._make_sales([
            ("p", "s", "2026-08-06", 20.0),
            ("p", "s", "2026-08-07", 30.0),
            ("p", "s", "2026-08-08", 99.0),
        ])
        result = _sales_window(sales, "p", "s", date(2026, 8, 6), date(2026, 8, 8))
        assert result == 50.0

    def test_no_sales_in_window(self):
        sales = self._make_sales([("p", "s", "2026-08-10", 100.0)])
        result = _sales_window(sales, "p", "s", date(2026, 8, 6), date(2026, 8, 7))
        assert result == 0.0

    def test_same_day_window_returns_zero(self):
        # d0==d1 → range пустой
        sales = self._make_sales([("p", "s", "2026-08-06", 50.0)])
        result = _sales_window(sales, "p", "s", date(2026, 8, 6), date(2026, 8, 6))
        assert result == 0.0


# ── _v2_balance ───────────────────────────────────────────────────────

class TestV2Balance:
    from datetime import datetime, timezone

    @staticmethod
    def _ts(date_str: str, hour: int = 5):
        from datetime import datetime, timezone
        return datetime(int(date_str[:4]), int(date_str[5:7]), int(date_str[8:]),
                        hour, 0, 0, tzinfo=timezone.utc)

    def _snap(self, qty: float, ts):
        return {"stock": qty, "avail": qty, "rsrv": 0.0, "ts": ts}

    def _sales(self, pairs):
        d = defaultdict(float)
        for pid, sid, day_str, qty in pairs:
            d[(pid, sid, date.fromisoformat(day_str))] = qty
        return d

    def test_perfect_balance_no_movements(self):
        # s0=100, s1=60, продажи Aug6=40 → balance closes
        t0 = self._ts("2026-08-06")
        t1 = self._ts("2026-08-07")
        s0 = self._snap(100.0, t0)
        s1 = self._snap(60.0, t1)
        sales = self._sales([("p", "s", "2026-08-06", 40.0)])
        ok, err = _v2_balance(s0, s1, "p", "s", date(2026, 8, 6), date(2026, 8, 7),
                              sales, {})
        assert ok is True
        assert err == 0.0

    def test_balance_with_supply(self):
        # s0=50, supply 100 в окне, s1=110, продажи=40 → 50+100-40=110 ✓
        t0 = self._ts("2026-08-06")
        t1 = self._ts("2026-08-07")
        supply_ts = self._ts("2026-08-06", 12)  # в окне
        s0 = self._snap(50.0, t0)
        s1 = self._snap(110.0, t1)
        sales = self._sales([("p", "s", "2026-08-06", 40.0)])
        mv_ts = {("p", "s"): {"supply": [(supply_ts, 100.0)]}}
        ok, err = _v2_balance(s0, s1, "p", "s", date(2026, 8, 6), date(2026, 8, 7),
                              sales, mv_ts)
        assert ok is True
        assert err == 0.0

    def test_balance_mismatch(self):
        # s0=100, s1=120, нет движений — ошибка 20
        t0 = self._ts("2026-08-06")
        t1 = self._ts("2026-08-07")
        s0 = self._snap(100.0, t0)
        s1 = self._snap(120.0, t1)
        sales = self._sales([])
        ok, err = _v2_balance(s0, s1, "p", "s", date(2026, 8, 6), date(2026, 8, 7),
                              sales, {})
        assert ok is False
        assert err == 20.0

    def test_tolerance_half_unit(self):
        # ошибка ровно 0.5 — граничный случай, должен быть balance_ok=True
        t0 = self._ts("2026-08-06")
        t1 = self._ts("2026-08-07")
        s0 = self._snap(100.0, t0)
        s1 = self._snap(99.5, t1)
        sales = self._sales([])
        ok, err = _v2_balance(s0, s1, "p", "s", date(2026, 8, 6), date(2026, 8, 7),
                              sales, {})
        assert ok is True


# ── build_forecast_features ──────────────────────────────────────────

class TestBuildForecastFeatures:
    def _make_records(self, evidences_and_sales):
        recs = []
        for i, (ev, sales) in enumerate(evidences_and_sales):
            recs.append(_day(ev, sales=sales, date_=date(2026, 8, 1) + timedelta(i)))
        return recs

    def test_empty_input(self):
        result = build_forecast_features([])
        assert result == []

    def test_single_product_store(self):
        recs = self._make_records([
            ("POSITIVE_STOCK_OBSERVED", 10.0),
            ("POSITIVE_STOCK_OBSERVED", 20.0),
            ("POSITIVE_STOCK_OBSERVED", 30.0),
            ("NO_SNAPSHOT_ROW", 0.0),
        ])
        features = build_forecast_features(recs)
        assert len(features) == 1
        f = features[0]
        assert f.positive_stock_days == 3
        assert f.no_snapshot_days == 1
        assert f.calendar_days == 4
        assert f.confirmed_sales_total == pytest.approx(60.0)
        assert f.median_daily_sales == pytest.approx(20.0)
        assert f.mean_daily_sales == pytest.approx(20.0)

    def test_median_vs_mean_differ(self):
        # 10, 10, 10, 100 → median=10, mean=32.5
        recs = self._make_records([
            ("POSITIVE_STOCK_OBSERVED", 10.0),
            ("POSITIVE_STOCK_OBSERVED", 10.0),
            ("POSITIVE_STOCK_OBSERVED", 10.0),
            ("POSITIVE_STOCK_OBSERVED", 100.0),
        ])
        f = build_forecast_features(recs)[0]
        assert f.median_daily_sales == pytest.approx(10.0)
        assert f.mean_daily_sales == pytest.approx(32.5)

    def test_only_one_confirmed_day_gives_none_stats(self):
        recs = self._make_records([("POSITIVE_STOCK_OBSERVED", 25.0)])
        f = build_forecast_features(recs)[0]
        assert f.median_daily_sales is None
        assert f.mean_daily_sales == pytest.approx(25.0)

    def test_limited_history_flag(self):
        recs = self._make_records([
            ("POSITIVE_STOCK_OBSERVED", 10.0),
            ("NO_SNAPSHOT_ROW", 0.0),
        ])
        f = build_forecast_features(recs)[0]
        assert "LIMITED_HISTORY" in f.data_quality_flags

    def test_no_limited_history_when_enough_days(self):
        recs = self._make_records([("POSITIVE_STOCK_OBSERVED", 10.0)] * 7)
        f = build_forecast_features(recs)[0]
        assert "LIMITED_HISTORY" not in f.data_quality_flags

    def test_two_stores_separate_features(self):
        def _day2(sid, sales, date_):
            return DayRecord(
                date=date_, product_id="p1", product_name="Test",
                store_id=sid, store_name=sid,
                sales_qty=sales, stock_qty=100.0, available_qty=100.0, reserve_qty=0.0,
                supply_qty=0.0, move_in_qty=0.0, move_out_qty=0.0,
                loss_qty=0.0, enter_qty=0.0,
                snapshot_present=True, snapshot_volume_anomaly=False,
                balance_ok=None, balance_abs_error=None,
                data_quality_flags=(),
                availability_evidence="POSITIVE_STOCK_OBSERVED",
            )
        recs = [
            _day2("s1", 10.0, date(2026, 8, 1)),
            _day2("s1", 20.0, date(2026, 8, 2)),
            _day2("s1", 30.0, date(2026, 8, 3)),
            _day2("s2", 5.0, date(2026, 8, 1)),
            _day2("s2", 15.0, date(2026, 8, 2)),
            _day2("s2", 25.0, date(2026, 8, 3)),
        ]
        features = build_forecast_features(recs)
        assert len(features) == 2
        by_sid = {f.store_id: f for f in features}
        assert by_sid["s1"].median_daily_sales == pytest.approx(20.0)
        assert by_sid["s2"].median_daily_sales == pytest.approx(15.0)

    def test_all_period_sales_total_includes_all_days(self):
        recs = self._make_records([
            ("POSITIVE_STOCK_OBSERVED", 10.0),
            ("NO_SNAPSHOT_ROW", 5.0),     # не в confirmed, но в all_period
            ("SNAPSHOT_UNRELIABLE", 3.0), # аналогично
        ])
        f = build_forecast_features(recs)[0]
        assert f.all_period_sales_total == pytest.approx(18.0)
        assert f.confirmed_sales_total == pytest.approx(10.0)

    def test_balance_ok_ratio(self):
        recs = [
            _day(balance_ok=True,  balance_err=0.0),
            _day(balance_ok=True,  balance_err=0.0,  date_=date(2026, 8, 8)),
            _day(balance_ok=False, balance_err=25.0, date_=date(2026, 8, 9),
                 flags=("BALANCE_MISMATCH",)),
        ]
        f = build_forecast_features(recs)[0]
        assert f.balance_ok_ratio == pytest.approx(2 / 3, rel=1e-3)
