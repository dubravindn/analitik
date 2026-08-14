"""
Replenishment engine: expected_demand → recommended_order_qty.

Принимает готовые данные, не вычисляет прогноз.
Отдельная функция — чтобы заказ можно было пересчитать
без повторного запуска forecast.
"""
from __future__ import annotations

import math


def calculate_replenishment(
    expected_demand:    float,
    available_stock:    float | None,
    reserve_qty:        float,
    confirmed_incoming: float | None,
    transfer_in_qty:    float,
    transfer_out_qty:   float,
    pack_size:          int,
    safety_stock:       float = 0.0,  # v1: всегда 0, отдельная модель позже
) -> tuple[float, float, str]:
    """
    Вычисляет к заказу.

    Параметры:
      expected_demand     — ожидаемый спрос (из forecast engine, штук)
      available_stock     — доступный остаток (None = UNKNOWN)
      reserve_qty         — резерв (вычитается из stock)
      confirmed_incoming  — подтверждённое поступление (None = UNKNOWN, не 0)
      transfer_in_qty     — входящие перемещения
      transfer_out_qty    — исходящие перемещения
      pack_size           — размер упаковки для округления
      safety_stock        — буфер (v1 = 0; безопасный запас отдельно позже)

    Возвращает:
      (raw_order_qty, rounded_order_qty, reason)

    Цепочка:
      target_stock   = expected_demand + safety_stock
      net_available  = available_stock + confirmed_incoming + transfer_in - transfer_out - reserve
      raw_order      = max(0, target_stock - net_available)
      rounded_order  = ceil(raw_order / pack_size) * pack_size
    """
    target_stock = expected_demand + safety_stock

    # Если остаток неизвестен — не вычитаем (консервативный сценарий: заказать весь target)
    stock_contribution = available_stock if available_stock is not None else 0.0
    incoming = confirmed_incoming if confirmed_incoming is not None else 0.0

    net_available = (
        stock_contribution
        + incoming
        + transfer_in_qty
        - transfer_out_qty
        - reserve_qty
    )

    raw_order = max(0.0, target_stock - net_available)

    # Округление до упаковки вверх
    if pack_size > 1 and raw_order > 0:
        rounded_order = math.ceil(raw_order / pack_size) * pack_size
    else:
        rounded_order = math.ceil(raw_order)

    # Объяснение
    parts: list[str] = []
    if available_stock is None:
        parts.append("остаток неизвестен")
    if confirmed_incoming is None:
        parts.append("поступление неизвестно")
    if safety_stock > 0:
        parts.append(f"safety={safety_stock:.0f}")
    parts.append(f"target={target_stock:.0f}, net_avail={net_available:.0f}")
    reason = "; ".join(parts)

    return raw_order, float(rounded_order), reason


def apply_replenishment_to_result(result, stock_snapshot) -> None:
    """
    Обновляет ForecastResult остатком и пересчитывает заказ.
    Мутирует result in-place (удобно для shadow mode).
    """
    if stock_snapshot:
        result.available_stock = stock_snapshot.available_stock
        result.stock_all       = stock_snapshot.stock_all
        result.reserve_qty     = stock_snapshot.reserve_qty
    else:
        result.data_quality_flags = result.data_quality_flags + (
            __import__("hermes.forecast_models", fromlist=["DataFlag"]).DataFlag.NO_STOCK_DATA,
        )

    raw, rounded, reason = calculate_replenishment(
        expected_demand=result.expected_demand,
        available_stock=result.available_stock,
        reserve_qty=result.reserve_qty or 0.0,
        confirmed_incoming=result.incoming_qty,
        transfer_in_qty=result.transfer_in_qty,
        transfer_out_qty=result.transfer_out_qty,
        pack_size=result.pack_size,
    )
    result.raw_order_qty           = raw
    result.recommended_order_qty   = rounded
    result.recommendation_reason   = (
        (result.recommendation_reason + " | " if result.recommendation_reason else "")
        + reason
    )
