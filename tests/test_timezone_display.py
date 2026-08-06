"""Регрессия D1: timestamptz выводится в московском времени.

П12 стал хранить moment в UTC (корректно), но сессия Postgres по умолчанию в
UTC → отчёты печатали время на 3 ч назад. db.connect() теперь ставит зону
сессии Europe/Moscow. Тест требует БД; без неё — skip.
"""
from datetime import datetime, timezone

import pytest


def _conn_or_skip():
    try:
        from hermes import config, db
        return db.connect(config.DATABASE_URL())
    except Exception as e:  # нет psycopg / нет БД / нет DATABASE_URL
        pytest.skip(f"нет доступной БД: {e}")


def test_session_timezone_is_moscow():
    conn = _conn_or_skip()
    with conn.cursor() as cur:
        cur.execute("SHOW TimeZone")
        tz = cur.fetchone()[0]
        assert tz in ("Europe/Moscow", "W-SU"), tz


def test_known_moment_prints_moscow_hour():
    # Пример из PDF: Николай Дуб 200 000 ₽ — 01.08 23:50 МСК.
    # Хранится 20:50 UTC, при выводе должно снова стать 23:50.
    conn = _conn_or_skip()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT %s::timestamptz",
            (datetime(2026, 8, 1, 20, 50, tzinfo=timezone.utc),),
        )
        got = cur.fetchone()[0]
    assert (got.hour, got.minute) == (23, 50), got
