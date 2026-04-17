from __future__ import annotations

import sys

from loguru import logger

from .config import settings


def configure_logging() -> None:
    logger.remove()
    logger.add(
        sys.stderr,
        level=settings.log_level,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> "
            "| <level>{level: <8}</level> "
            "| <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> "
            "- <level>{message}</level>"
        ),
        backtrace=False,
        diagnose=False,
    )
    logger.add(
        "logs/arbitrage.jsonl",
        level="DEBUG",
        rotation="50 MB",
        retention="30 days",
        serialize=True,
        enqueue=True,
    )
