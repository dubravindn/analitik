"""Тесты московской таймзоны (пункт 12): сдвиг +3ч и «сегодня» по Москве."""
from datetime import datetime, timedelta, timezone

from hermes import config


def test_msk_offset_is_plus_three():
    assert config.MSK.utcoffset(None) == timedelta(hours=3)


def test_msk_now_offset():
    assert config.msk_now().utcoffset() == timedelta(hours=3)


def test_msk_moment_conversion_known():
    # МойСклад отдаёт 18:47 (МСК) без зоны → помечаем MSK → это 15:47 UTC.
    naive = datetime.strptime("2026-08-04 18:47:00", "%Y-%m-%d %H:%M:%S")
    utc = naive.replace(tzinfo=config.MSK).astimezone(timezone.utc)
    assert (utc.hour, utc.minute) == (15, 47)


def test_msk_today_matches_now_date():
    assert config.msk_today() == config.msk_now().date()
