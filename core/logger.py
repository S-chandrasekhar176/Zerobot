"""
ZeroBot — Centralized Logging
Uses loguru for structured, rotated, colored logs.
Logs go to: logs/trades/, logs/errors/, logs/signals/
"""
import sys
from pathlib import Path
from loguru import logger

LOG_DIR = Path(__file__).parent.parent / "logs"
# Ensure all log subdirectories exist on startup
for _sub in ("trades", "errors", "signals"):
    (LOG_DIR / _sub).mkdir(parents=True, exist_ok=True)

def setup_logger(level: str = "INFO"):
    logger.remove()

    # Console — colored, human-readable
    logger.add(
        sys.stdout,
        level=level,
        format="<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | <cyan>{name}</cyan>:<cyan>{line}</cyan> — <level>{message}</level>",
        colorize=True,
    )

    # Error file — persistent
    logger.add(
        LOG_DIR / "errors" / "errors_{time:YYYY-MM-DD}.log",
        level="ERROR",
        rotation="1 day",
        retention="30 days",
        encoding="utf-8",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {name}:{line} | {message}",
    )

    # Trade file — every trade logged
    logger.add(
        LOG_DIR / "trades" / "trades_{time:YYYY-MM-DD}.log",
        level="INFO",
        rotation="1 day",
        retention="90 days",
        encoding="utf-8",
        filter=lambda r: "TRADE" in r["message"] or "ORDER" in r["message"],
        format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {message}",
    )

    # Signal file — every signal
    logger.add(
        LOG_DIR / "signals" / "signals_{time:YYYY-MM-DD}.log",
        level="INFO",
        rotation="1 day",
        retention="30 days",
        encoding="utf-8",
        filter=lambda r: "SIGNAL" in r["message"],
        format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {message}",
    )

    return logger


log = setup_logger()
