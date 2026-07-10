"""Logging setup (PLAN.md LF-002).

One consistent stderr format for CLI commands; libraries obtain loggers via
``get_logger`` and never configure handlers themselves.
"""

from __future__ import annotations

import logging

_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S%z"


def setup_logging(level: int | str = logging.INFO) -> None:
    """Configure the root ``leanfaith`` logger once; repeated calls adjust level only."""
    logger = logging.getLogger("leanfaith")
    logger.setLevel(level)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATE_FORMAT))
        logger.addHandler(handler)
        logger.propagate = False


def get_logger(name: str) -> logging.Logger:
    """Return a logger under the ``leanfaith`` namespace."""
    if name == "leanfaith" or name.startswith("leanfaith."):
        return logging.getLogger(name)
    return logging.getLogger(f"leanfaith.{name}")
