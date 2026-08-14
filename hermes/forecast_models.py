"""
Общие типы и константы для production forecast-движка.

Используется во всех forecast_*.py модулях.
calc_forecast.py не трогать — этот файл независим от него.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum


# ── Режимы прогноза ───────────────────────────────────────────────────────────

class ForecastMode(str, Enum):
    NORMAL    = "NORMAL"
    MARCH_8   = "MARCH_8"
    VALENTINE = "VALENTINE"
    NEW_YEAR  = "NEW_YEAR"


HOLIDAY_WINDOWS: list[tuple[tuple[int, int], tuple[int, int], ForecastMode]] = [
    ((2, 11), (2, 17), ForecastMode.VALENTINE),
    ((3, 1),  (3, 10), ForecastMode.MARCH_8),
    ((12, 25),(12, 31), ForecastMode.NEW_YEAR),
    ((1, 1),  (1, 10), ForecastMode.NEW_YEAR),
]

def forecast_mode_for(d: date) -> ForecastMode:
    for (m0, d0), (m1, d1), mode in HOLIDAY_WINDOWS:
        if date(d.year, m0, d0) <= d <= date(d.year, m1, d1):
            return mode
    return ForecastMode.NORMAL


# ── Каналы ────────────────────────────────────────────────────────────────────

class Channel(str, Enum):
    RETAIL = "розница"
    BASE   = "опт"


# ── Источник прогноза ─────────────────────────────────────────────────────────

class DemandSource(str, Enum):
    KNOWN_CO   = "known_co"    # уже оформленный CustomerOrder
    PREORDER   = "preorder"    # предзаказ / ПРЕДОПЛАТА
    STAT       = "stat"        # статистический остаток
    HYBRID     = "hybrid"      # known_co + preorder + stat


# ── Флаги качества данных ─────────────────────────────────────────────────────

class DataFlag(str, Enum):
    NO_SALES_HISTORY   = "NO_SALES_HISTORY"
    BELOW_OPER_START   = "BELOW_OPER_START"
    NO_STOCK_DATA      = "NO_STOCK_DATA"
    LARGE_ORDER_CO     = "LARGE_ORDER_CO"
    PREORDER_PRESENT   = "PREORDER_PRESENT"
    MARCH8_MODE        = "MARCH8_MODE"
    STAT_FALLBACK      = "STAT_FALLBACK"
    HOLIDAY_MODE       = "HOLIDAY_MODE"
    DOUBLE_COUNT_GUARD = "DOUBLE_COUNT_GUARD"


# ── Центральная структура результата ─────────────────────────────────────────

@dataclass
class ForecastResult:
    product_id:   str
    product_name: str
    store_id:     str
    store_name:   str
    channel:      Channel

    # Режим прогноза
    forecast_mode:    ForecastMode
    demand_source:    DemandSource
    model_name:       str            # 'mean_cal_14' | 'mean_cal_28' | 'hybrid'

    # Горизонт
    forecast_from:         date
    forecast_to:           date
    forecast_horizon_days: int
    cutoff_date:           date      # дата принятия решения о закупке

    # Декомпозиция спроса
    statistical_demand:  float = 0.0  # stat baseline (штук)
    known_order_demand:  float = 0.0  # CO на cutoff (штук)
    preorder_demand:     float = 0.0  # ПРЕДОПЛАТА / праздничные предзаказы (штук)
    expected_demand:     float = 0.0  # итого = stat_residual + known_co + preorder

    # Остаток
    available_stock: float | None = None
    stock_all:       float | None = None
    reserve_qty:     float | None = None

    # Поступления
    incoming_qty:     float | None = None  # None = UNKNOWN, не 0
    transfer_in_qty:  float = 0.0
    transfer_out_qty: float = 0.0

    # Заказ
    raw_order_qty:     float = 0.0
    recommended_order_qty: float = 0.0    # после округления до упаковки

    # Упаковка
    pack_size: int = 1

    # Стоимость (копейки)
    buy_price_kop:  float = 0.0
    order_cost_kop: float = 0.0

    # Флаги и объяснение
    data_quality_flags:   tuple[DataFlag, ...] = field(default_factory=tuple)
    recommendation_reason: str = ""

    def explain(self) -> str:
        """Краткое объяснение числа для explainability."""
        lines = [
            f"{self.product_name}  [{self.store_name}]",
            f"Режим: {self.forecast_mode.value}  Модель: {self.model_name}",
            "",
            f"  Статистический спрос : {self.statistical_demand:.0f}",
            f"  Известные CO         : {self.known_order_demand:.0f}",
            f"  Предзаказы           : {self.preorder_demand:.0f}",
            f"  Ожидаемый спрос      : {self.expected_demand:.0f}",
            "",
            f"  Доступный остаток    : {self.available_stock if self.available_stock is not None else 'N/A'}",
            f"  Подтверждённое пост. : {self.incoming_qty if self.incoming_qty is not None else 'UNKNOWN'}",
            "",
            f"  К заказу (raw)       : {self.raw_order_qty:.0f}",
            f"  К заказу (упаковки)  : {self.recommended_order_qty:.0f}",
            "",
            f"  Причина: {self.recommendation_reason}",
        ]
        if self.data_quality_flags:
            lines.append(f"  Флаги: {', '.join(f.value for f in self.data_quality_flags)}")
        return "\n".join(lines)
