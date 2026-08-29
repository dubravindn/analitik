from datetime import date

from hermes.dashboard_snapshot import period_label


def test_period_label_week_inside_month():
    assert period_label(date(2026, 8, 16), date(2026, 8, 22)) == (
        "16–22 августа 2026"
    )


def test_period_label_single_day():
    assert period_label(date(2026, 8, 22), date(2026, 8, 22)) == (
        "22 августа 2026"
    )


def test_period_label_across_months():
    assert period_label(date(2026, 8, 29), date(2026, 9, 4)) == (
        "29 августа 2026 – 4 сентября 2026"
    )
