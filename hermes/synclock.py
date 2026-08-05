"""Простой файловый маркер «идёт фоновая выгрузка».

Долгие ETL (sync-*, backfill, daily) выставляют маркер на время работы; бот
проверяет его перед тяжёлыми отчётами и предупреждает пользователя вместо того,
чтобы молча повиснуть на контенции с БД/API.
"""
from __future__ import annotations

import os
import time
from contextlib import contextmanager

_LOCK_PATH = os.environ.get("HERMES_SYNC_LOCK", "/tmp/hermes_sync.lock")
_MAX_AGE_S = 40 * 60   # маркер старше 40 мин считаем протухшим (краш синка)


def set_running(name: str) -> None:
    try:
        with open(_LOCK_PATH, "w", encoding="utf-8") as f:
            f.write(f"{name}\n{int(time.time())}\n")
    except OSError:
        pass


def clear() -> None:
    try:
        os.remove(_LOCK_PATH)
    except OSError:
        pass


def active() -> tuple[str, int] | None:
    """Если идёт выгрузка — (имя, сколько минут уже идёт). Иначе None.

    Протухший маркер (старше _MAX_AGE_S) игнорируется — на случай, если процесс
    упал и не убрал файл.
    """
    try:
        with open(_LOCK_PATH, encoding="utf-8") as f:
            name = f.readline().strip()
            started = int(f.readline().strip() or "0")
    except (OSError, ValueError):
        return None
    age = int(time.time()) - started
    if age < 0 or age > _MAX_AGE_S:
        return None
    return name, age // 60


@contextmanager
def running(name: str):
    set_running(name)
    try:
        yield
    finally:
        clear()
