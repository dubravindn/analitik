"""Тесты расчётных формул на известных ответах.

Числа взяты из реальной сверки за 2026-08-04 (склад «Киров, Ленина 102А»):
выручка 35 931 ₽, себестоимость 9 258 ₽, 14 чеков.
"""
from hermes import calc


def test_gross_profit():
    # 35 931,00 ₽ − 9 258,00 ₽ = 26 673,00 ₽  (в копейках)
    assert calc.gross_profit(3_593_100, 925_800) == 2_667_300


def test_gross_margin_pct():
    m = calc.gross_margin_pct(3_593_100, 925_800)
    assert round(m, 1) == 74.2


def test_gross_margin_zero_revenue():
    # деления на ноль быть не должно
    assert calc.gross_margin_pct(0, 0) == 0.0


def test_avg_check():
    # 35 931,00 ₽ / 14 чеков = 2 566,50 ₽ → 256 650 копеек
    assert calc.avg_check(3_593_100, 14) == 256_650


def test_avg_check_no_checks():
    assert calc.avg_check(100_000, 0) == 0


def test_kop_to_rub():
    assert calc.kop_to_rub(3_593_100) == 35931.0


def test_delta_pct():
    assert calc.delta_pct(120, 100) == 20.0
    assert calc.delta_pct(80, 100) == -20.0


def test_delta_pct_zero_base():
    assert calc.delta_pct(50, 0) is None


def test_pricing_rules():
    # Наличка 100 ₽ → перевод 110 ₽, розница 195 ₽
    assert calc.expected_transfer_price(100) == 110.0
    assert calc.expected_retail_price(100) == 195.0
