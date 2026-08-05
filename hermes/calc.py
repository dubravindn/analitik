"""Чистые расчётные функции — считают деньги. Покрыты тестами (tests/test_calc.py).

Здесь НЕТ обращений к сети или БД: на вход числа, на выходе числа. Это позволяет
проверять формулы на известных ответах и гарантировать воспроизводимость.

Деньги внутри системы — в КОПЕЙКАХ (целые). В рубли переводим только для показа.
"""
from __future__ import annotations


def kop_to_rub(kop: int) -> float:
    """Копейки → рубли."""
    return kop / 100


def gross_profit(revenue_kop: int, cost_kop: int) -> int:
    """Грязная прибыль = Выручка − Себестоимость (в копейках)."""
    return revenue_kop - cost_kop


def gross_margin_pct(revenue_kop: int, cost_kop: int) -> float:
    """% грязной прибыли = Грязная прибыль / Выручка × 100.

    Если выручка ноль — процент не определён, возвращаем 0.0 (а не деление на ноль).
    """
    if revenue_kop == 0:
        return 0.0
    return gross_profit(revenue_kop, cost_kop) / revenue_kop * 100


def avg_check(revenue_kop: int, checks: int) -> int:
    """Средний чек = Выручка / Количество чеков (в копейках, округление к ближайшему).

    Нет чеков — нет среднего, возвращаем 0.
    """
    if checks == 0:
        return 0
    return round(revenue_kop / checks)


def delta_pct(current: float, previous: float) -> float | None:
    """Изменение в % относительно прошлого периода.

    Прошлое значение ноль → сравнение не определено, возвращаем None (в отчёте покажем «—»).
    """
    if previous == 0:
        return None
    return (current - previous) / previous * 100


# --- Ценообразование (контроль прайса), правила владельца ---
COEFF_TRANSFER = 1.10   # Цена по переводу/карте = Наличка × 1,10
COEFF_RETAIL = 1.95     # Розничная цена = Наличка × 1,95


def expected_transfer_price(cash_price: float) -> float:
    return round(cash_price * COEFF_TRANSFER, 2)


def expected_retail_price(cash_price: float) -> float:
    return round(cash_price * COEFF_RETAIL, 2)
