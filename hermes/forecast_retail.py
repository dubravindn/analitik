"""
Retail forecast engine: mean_calendar_N для розницы (SKU × магазин).

Baseline: mean_cal_14 (подтверждён backtest v2, RETAIL NORMAL WAPE=0.658).
Параметризован через retail_window_days — не хардкодить в N местах.
"""
from __future__ import annotations

import math
from datetime import date, timedelta

import hermes.config as config
from hermes.forecast_data import (
    load_product_daily_sales_matrix,
    load_srezka_products,
    load_store_names,
    load_store_operational_start,
    rolling_mean,
)
from hermes.forecast_models import (
    Channel,
    DataFlag,
    DemandSource,
    ForecastMode,
    ForecastResult,
    forecast_mode_for,
)

# ── Параметры розничного движка ───────────────────────────────────────────────
# Изменять здесь (или через config), не в отдельных функциях.
RETAIL_WINDOW_DAYS: int = getattr(config, "RETAIL_WINDOW_DAYS", 14)


def build_retail_forecast(
    conn,
    store_id:     str,
    cutoff_date:  date,
    horizon_from: date,
    horizon_to:   date,
    window_days:  int | None = None,
) -> list[ForecastResult]:
    """
    Строит прогноз для одного розничного магазина.

    Алгоритм:
      statistical_demand = rolling_mean(window_days) × horizon_days
      expected_demand    = statistical_demand  (розница: только stat)

    Предоперационные нули исключаются.
    Возвращает по одному ForecastResult на каждый SKU с историей продаж.
    """
    w = window_days or RETAIL_WINDOW_DAYS
    horizon_days = (horizon_to - horizon_from).days + 1
    mode = forecast_mode_for(horizon_from)

    # Данные
    products   = load_srezka_products(conn)
    store_names = load_store_names(conn)
    store_name  = store_names.get(store_id, store_id)
    oper_start  = load_store_operational_start(conn, store_id)

    # История продаж за окно (+ небольшой запас для rolling)
    history_from = cutoff_date - timedelta(days=w + 7)
    sales_matrix = load_product_daily_sales_matrix(
        conn, store_id, history_from, cutoff_date - timedelta(days=1)
    )

    results: list[ForecastResult] = []

    for pid, info in products.items():
        daily = sales_matrix.get(pid, {})
        if not daily and oper_start and cutoff_date > oper_start:
            # Товар в ассортименте, но продаж за окно нет — нулевой прогноз
            flags = (DataFlag.NO_SALES_HISTORY,)
            reason = "нет продаж за последние {} дней".format(w)
        else:
            flags = ()
            reason = ""

        avg_daily = rolling_mean(daily, cutoff_date - timedelta(days=1), w, oper_start)

        if oper_start and cutoff_date <= oper_start:
            avg_daily = 0.0
            flags = (DataFlag.BELOW_OPER_START,)
            reason = "до operational_start магазина"

        stat_demand = avg_daily * horizon_days

        if mode in (ForecastMode.MARCH_8, ForecastMode.VALENTINE):
            flags = flags + (DataFlag.HOLIDAY_MODE,)

        results.append(ForecastResult(
            product_id=pid,
            product_name=info.product_name,
            store_id=store_id,
            store_name=store_name,
            channel=Channel.RETAIL,
            forecast_mode=mode,
            demand_source=DemandSource.STAT,
            model_name=f"mean_cal_{w}",
            forecast_from=horizon_from,
            forecast_to=horizon_to,
            forecast_horizon_days=horizon_days,
            cutoff_date=cutoff_date,
            statistical_demand=round(stat_demand, 1),
            known_order_demand=0.0,
            preorder_demand=0.0,
            expected_demand=round(stat_demand, 1),
            raw_order_qty=max(0.0, round(stat_demand, 1)),
            recommended_order_qty=math.ceil(stat_demand) if stat_demand > 0 else 0.0,
            pack_size=1,
            data_quality_flags=flags,
            recommendation_reason=reason or f"mean_cal_{w} × {horizon_days}d",
        ))

    return results
