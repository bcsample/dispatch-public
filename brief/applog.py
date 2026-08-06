"""Local structured logging for the brief.

Writes timestamped, component-tagged lines to data/logs/brief.log (rotating,
gitignored) instead of bare print(), so a launchd service's stdout/stderr
capture (which macOS wipes from /tmp on reboot) isn't the only record of what
happened. Logging must never crash the app, so setup is best-effort.

Usage:
    from brief import applog
    log = applog.get(__name__)
    log.warning("ollama unreachable: %r", exc)

Verbose logging is deliberately welcome for now (the user, 2026-07-27: Engine
Room becomes the household log reader) — INFO by default, BRIEF_LOG_LEVEL
env var to turn up to DEBUG without a code change.
"""

from __future__ import annotations

import logging
import logging.handlers
import os

from .config import DATA_DIR

_LOG_DIR = DATA_DIR / "logs"
_CONFIGURED = False


def setup() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    _CONFIGURED = True  # set first so a failure here doesn't retry every call
    try:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            _LOG_DIR / "brief.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter(
            "%(asctime)s  %(levelname)-7s %(name)s  %(message)s", "%Y-%m-%d %H:%M:%S"
        ))
        root = logging.getLogger("brief")
        level_name = os.environ.get("BRIEF_LOG_LEVEL", "INFO").upper()
        root.setLevel(getattr(logging, level_name, logging.INFO))
        if not any(isinstance(h, logging.handlers.RotatingFileHandler) for h in root.handlers):
            root.addHandler(handler)
        root.propagate = False
    except OSError:
        pass  # e.g. read-only fs — never block the brief on logging


def get(name: str) -> logging.Logger:
    setup()
    return logging.getLogger(f"brief.{name.split('.')[-1]}")
