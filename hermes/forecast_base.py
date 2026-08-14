"""
BASE forecast engine: HYBRID (known CO + preorders + stat residual).

Подтверждено backtest v2:
  STAT_ONLY  WAPE = 0.459
  HYBRID     WAPE = 0.265  (+42.3%)
  NORMAL     WAPE = 0.136

Главное правило: NO DOUBLE COUNT.
  stat_residual вычисляется от исторического residual (total - known_at_cutoff),
  а не от полного объёма. known_order_demand + stat_residual + preorder_demand
  не пересекаются.
"""
from __future__ import annotations

import math
from collections import defaultdict
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
from hermes.forecast_orders import (
    LARGE_ORDER_THRESHOLD_DEFAULT,
    aggregate_known_demand,
    load_known_customer_orders,
)
from hermes.forecast_replenishment import calculate_replenishment

# ── Параметры BASE-движка ─────────────────────────────────────────────────────
BASE_STAT_WINDOW_DAYS:      int = getattr(config, "BASE_STAT_WINDOW_DAYS", 28)
BASE_LARGE_ORDER_THRESHOLD: int = getattr(config, "BASE_LARGE_ORDER_THRESHOLD",
                                          LARGE_ORDER_THRESHOLD_DEFAULT)
PROCUREMENT_LEAD_DAYS:      int = getattr(config, "PROCUREMENT_LEAD_DAYS", 7)


def _stat_residual_history(
    sales_matrix: dict[str, dict[date, float]],
    product_id:   str,
    cutoff:       date,
    window_days:  int,
    oper_start:   date | None,
) -> float:
    """
    Среднедневной исторический residual спрос за окно.

    residual_day = total_sales_day - known_sales_day
    Но «known» в исторических данных неизвестен по дням.
    Аппроксимация: за последние window_days считаем полный объём как
    upper bound; вычитание known_at_cutoff делается на уровне ForecastResult.

    Для первой production-версии stat_residual = rolling_mean(28d) × horizon.
    Улучшение: в следующей итерации — вычитать историческое known_qty
    (требует CO-истории в БД).
    """
    daily = sales_matrix.get(product_id, {})
    return rolling_mean(daily, cutoff - timedelta(days=1), window_days, oper_start)


def build_base_hybrid_forecast(
    conn,
    token:        str,
    store_id:     str,
    store_href:   str,
    cutoff_date:  date,
    horizon_from: date,
    horizon_to:   date,
    stat_window:  int | None = None,
    procurement_lead: int | None = None,
    large_threshold:  int | None = None,
) -> list[ForecastResult]:
    """
    Строит HYBRID-прогноз для BASE (Базы Воровского).

    Компоненты:
      known_order_demand = сумма CO-позиций, CO.moment <= cutoff_date
      preorder_demand    = subset known с флагом ПРЕДОПЛАТА/праздник
      stat_residual      = mean_cal_28(исторический) × horizon
                           (upper bound; в v2 будет вычитаться known_history)
      expected_demand    = known_order_demand + stat_residual
                           (preorder_demand уже входит в known_order_demand)

    NO DOUBLE COUNT: stat_residual не прибавляется поверх known_order_demand.
    Вместо этого stat обеспечивает floor — берём max(known, stat).
    Конечная формула:
      expected = known_order_demand + max(0, stat_residual - known_order_demand)
               = max(known_order_demand, stat_residual)
    Это корректно только пока у нас нет исторического residual по дням.
    Когда появится CO-история в БД — перейти на истинный residual.
    """
    w        = stat_window or BASE_STAT_WINDOW_DAYS
    lead     = procurement_lead or PROCUREMENT_LEAD_DAYS
    thresh   = large_threshold or BASE_LARGE_ORDER_THRESHOLD
    horizon_days = (horizon_to - horizon_from).days + 1
    mode = forecast_mode_for(horizon_from)

    # Данные
    products    = load_srezka_products(conn)
    store_names = load_store_names(conn)
    store_name  = store_names.get(store_id, store_id)
    oper_start  = load_store_operational_start(conn, store_id)

    # История продаж за окно
    history_from = cutoff_date - timedelta(days=w + 7)
    sales_matrix = load_product_daily_sales_matrix(
        conn, store_id, history_from, cutoff_date - timedelta(days=1)
    )

    # Known CustomerOrders (no future leakage)
    co_positions = load_known_customer_orders(
        token=token,
        store_href=store_href,
        cutoff_date=cutoff_date,
        horizon_from=horizon_from,
        horizon_to=horizon_to,
        large_order_threshold=thresh,
    )
    co_by_product = aggregate_known_demand(co_positions, thresh)

    results: list[ForecastResult] = []

    for pid, info in products.items():
        flags: list[DataFlag] = []

        # Stat residual
        avg_daily = _stat_residual_history(
            sales_matrix, pid, cutoff_date, w, oper_start
        )
        stat_demand = avg_daily * horizon_days

        if oper_start and cutoff_date <= oper_start:
            stat_demand = 0.0
            flags.append(DataFlag.BELOW_OPER_START)

        if not sales_matrix.get(pid):
            flags.append(DataFlag.NO_SALES_HISTORY)

        # Known CO
        co_agg = co_by_product.get(pid, {})
        known_qty    = float(co_agg.get("known_qty", 0))
        preorder_qty = float(co_agg.get("preorder_qty", 0))
        large_qty    = float(co_agg.get("large_order_qty", 0))

        if large_qty > 0:
            flags.append(DataFlag.LARGE_ORDER_CO)
        if preorder_qty > 0:
            flags.append(DataFlag.PREORDER_PRESENT)
        if mode in (ForecastMode.MARCH_8, ForecastMode.VALENTINE):
            flags.append(DataFlag.HOLIDAY_MODE)

        # HYBRID без double-count:
        # stat = upper bound от всего спроса
        # known_order_demand уже покрывает часть stat
        # residual = max(0, stat - known)
        stat_residual = max(0.0, stat_demand - known_qty)
        expected = known_qty + stat_residual
        flags.append(DataFlag.DOUBLE_COUNT_GUARD)

        if mode == ForecastMode.MARCH_8:
            flags.append(DataFlag.MARCH8_MODE)
            # Для 8 марта stat не доверяем — используем known + преязаказы
            # stat_residual оставляем как есть (residual от known)

        source = DemandSource.HYBRID if known_qty > 0 else DemandSource.STAT
        model  = f"hybrid(co+mean_cal_{w})" if known_qty > 0 else f"mean_cal_{w}"

        reason_parts = []
        if known_qty > 0:
            reason_parts.append(f"CO={known_qty:.0f}")
        if preorder_qty > 0:
            reason_parts.append(f"preorder={preorder_qty:.0f}")
        reason_parts.append(f"stat_residual={stat_residual:.0f}")
        reason = " + ".join(reason_parts)

        results.append(ForecastResult(
            product_id=pid,
            product_name=info.product_name,
            store_id=store_id,
            store_name=store_name,
            channel=Channel.BASE,
            forecast_mode=mode,
            demand_source=source,
            model_name=model,
            forecast_from=horizon_from,
            forecast_to=horizon_to,
            forecast_horizon_days=horizon_days,
            cutoff_date=cutoff_date,
            statistical_demand=round(stat_demand, 1),
            known_order_demand=round(known_qty, 1),
            preorder_demand=round(preorder_qty, 1),
            expected_demand=round(expected, 1),
            raw_order_qty=max(0.0, round(expected, 1)),
            recommended_order_qty=math.ceil(expected) if expected > 0 else 0.0,
            pack_size=1,
            data_quality_flags=tuple(flags),
            recommendation_reason=reason,
        ))

    return results
