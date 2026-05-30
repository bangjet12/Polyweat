"""Logging utilities - rotating file logger plus console handler."""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Optional


_FMT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"


def setup_logging(log_dir: Path, level: str = "INFO") -> logging.Logger:
    """Configure the root `polyweat` logger. Idempotent."""
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("polyweat")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    if logger.handlers:
        return logger  # already configured

    fmt = logging.Formatter(_FMT)

    # Console
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    # Rotating file
    fh = logging.handlers.RotatingFileHandler(
        log_dir / "polyweat.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    logger.propagate = False
    return logger


def get_logger(name: Optional[str] = None) -> logging.Logger:
    return logging.getLogger("polyweat" if name is None else f"polyweat.{name}")
