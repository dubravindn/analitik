"""Единая закупочная цена для всего Hermes.

unit_cost() — единственная точка входа; фолбэк на себестоимость МойСклад убран везде.
"""
from __future__ import annotations


def unit_cost(
    card_price_kop: int | None,
    sale_price_kop: int,
) -> tuple[int, str]:
    """Закупочная цена единицы по единому правилу проекта.

    Returns (cost_per_unit_kop, source):
      'card'     — из карточки товара (точная цена приёмки)
      'estimate' — цена продажи × 0.6 (оценка: продажа − 40%)

    Категории «нет цены» нет: каждый товар получает cost.
    """
    if card_price_kop is not None and int(card_price_kop) > 0:
        return int(card_price_kop), "card"
    return round(int(sale_price_kop) * 0.6), "estimate"
