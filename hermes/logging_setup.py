"""Единая настройка логирования. Каждый запуск пишет в logs/hermes.log и в консоль."""
from __future__ import annotations

import logging
from pathlib import Path

_CONFIGURED = False


def setup(level: int = logging.INFO) -> logging.Logger:
    global _CONFIGURED
    logger = logging.getLogger("hermes")
    if _CONFIGURED:
        return logger

    logs_dir = Path(__file__).resolve().parent.parent / "logs"
    logs_dir.mkdir(exist_ok=True)

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    fh = logging.FileHandler(logs_dir / "hermes.log", encoding="utf-8")
    fh.setFormatter(fmt)
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)

    logger.setLevel(level)
    logger.addHandler(fh)
    logger.addHandler(ch)
    logger.propagate = False
    _CONFIGURED = True
    return logger
