"""Local structured logging for the brief (Helm 3b, 2026-07-27 — the operator: "logs
are just text, I have space... very high priority").

Writes timestamped, component-tagged lines to data/logs/brief.log (rotating,
gitignored) instead of bare print(), so a launchd service's stdout/stderr
capture (which macOS wipes from /tmp on reboot) isn't the only record of what
happened. Donor pattern: the voice assistant project's applog.py — same shape, adapted to
this repo's existing data/ convention (brief.config.DATA_DIR) instead of a
hardcoded path. Logging must never crash the app, so setup is best-effort.

Usage:
    from brief import applog
    log = applog.get(__name__)
    log.warning("ollama unreachable: %r", exc)

Verbose logging is deliberately welcome for now (the operator, 2026-07-27: the host monitor becomes the project log reader) — INFO by default, BRIEF_LOG_LEVEL
env var to turn up to DEBUG without a code change.

One file per PROCESS (HS-5, 2026-09-14). `BRIEF_LOG_FILE` names the file under
data/logs/ (default brief.log). The speaker daemon is a second live process
that imports brief modules; before this it attached its own rotating handler
to the engine's brief.log, and two processes rotating one file lose lines at
every rollover. It now sets BRIEF_LOG_FILE=speaker.log before any brief import.
"""

from __future__ import annotations

import logging
import logging.handlers
import os

from .config import DATA_DIR

_LOG_DIR = DATA_DIR / "logs"
_CONFIGURED = False


def log_filename() -> str:
    """The file under data/logs/ this process writes: BRIEF_LOG_FILE, reduced to
    a bare name so an env value can never point the log outside data/logs/."""
    from pathlib import PurePath

    name = PurePath(os.environ.get("BRIEF_LOG_FILE", "").strip()).name
    return name or "brief.log"


def setup() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    _CONFIGURED = True  # set first so a failure here doesn't retry every call
    try:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        filename = log_filename()
        handler = logging.handlers.RotatingFileHandler(
            _LOG_DIR / filename,
            maxBytes=1_000_000,
            backupCount=3,
            encoding="utf-8",
            # delay=True: open the file on the FIRST EMIT, not at construction
            # (fable #1657 item 4, 2026-09-21). Module-level `log =
            # applog.get(__name__)` runs setup() at IMPORT, so without this any
            # ad-hoc `import brief.window.kokoro_tts` from a shell opened a
            # handle on the operator's live brief.log before a single line was logged.
            #  says fix it at the sink; this is the sink.
            delay=True,
        )
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s  %(levelname)-7s %(name)s  %(message)s",
                "%Y-%m-%d %H:%M:%S",
            )
        )
        root = logging.getLogger("brief")
        level_name = os.environ.get("BRIEF_LOG_LEVEL", "INFO").upper()
        root.setLevel(getattr(logging, level_name, logging.INFO))
        if not any(
            isinstance(h, logging.handlers.RotatingFileHandler) for h in root.handlers
        ):
            root.addHandler(handler)
        root.propagate = False
    except OSError:
        pass  # e.g. read-only fs — never block the brief on logging


def get(name: str) -> logging.Logger:
    setup()
    return logging.getLogger(f"brief.{name.split('.')[-1]}")
