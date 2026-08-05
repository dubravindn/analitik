"""Тесты кратности упаковки для позиционного прогноза (D2.2)."""
from hermes.report_forecast import _pkg_size


def test_flower_bunch_sizes():
    assert _pkg_size("Роза Фридом Эквадор 50 см. 25 шт.") == 25
    assert _pkg_size("Гвоздика Красная Эквадор 20 шт.") == 20
    assert _pkg_size("Хризантема Магнум 10 шт.") == 10
    assert _pkg_size("Хризантема Балтика BEYOND 5 шт.") == 5


def test_large_counts_not_a_multiple():
    # «N шт.» больше 100 — это содержимое коробки, а не кратность заказа.
    assert _pkg_size("Кризал 5 гр. 1000 шт.") is None
    assert _pkg_size("Открытки 500 шт.") is None


def test_no_pack_in_name():
    assert _pkg_size("Эвкалипт Цинарея Китай") is None
    assert _pkg_size("Рускус Сочи") is None
