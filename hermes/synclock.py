"""Простой файловый маркер «идёт фоновая выгрузка».

Долгие ETL (sync-*, backfill, daily) выставляют маркер на время работы; бот
проверяет его перед тяжёлыми отчётами и предупреждает пользователя вместо того,
чтобы молча повиснуть на контенции с БД/API.

Маркер хранит PID — если процесс мёртв (убит по таймауту), active() считает
маркер протухшим и удаляет его, чтобы бот не залипал дольше одного обращения.
"""
from __future__ import annotations

import os
import time
from contextlib import contextmanager

_LOCK_PATH = os.environ.get("HERMES_SYNC_LOCK", "/tmp/hermes_sync.lock")
_MAX_AGE_S = 15 * 60   # страховка: маркер старше 15 мин считаем протухшим


def set_running(name: str) -> None:
    try:
        with open(_LOCK_PATH, "w", encoding="utf-8") as f:
            f.write(f"{name}\n{int(time.time())}\n{os.getpid()}\n")
    except OSError:
        pass


def clear() -> None:
    try:
        os.remove(_LOCK_PATH)
    except OSError:
        pass


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True   # существует, но чужой — считаем живым
    except OSError:
        return False
    return True


def active() -> tuple[str, int] | None:
    """Если идёт выгрузка — (имя, сколько минут уже идёт). Иначе None.

    Протухший маркер (мёртвый PID или возраст > _MAX_AGE_S) удаляется и даёт None.
    """
    try:
        with open(_LOCK_PATH, encoding="utf-8") as f:
            name = f.readline().strip()
            started = int(f.readline().strip() or "0")
            pid = int(f.readline().strip() or "0")
    except (OSError, ValueError):
        return None
    age = int(time.time()) - started
    if age < 0 or age > _MAX_AGE_S or not _pid_alive(pid):
        clear()
        return None
    return name, age // 60


@contextmanager
def running(name: str):
    set_running(name)
    try:
        yield
    finally:
        clear()
