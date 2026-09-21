"""Test isolation: every test runs against a throwaway SQLite DB in tmp_path,
never the live data/brief.db the wall display is serving. Individual tests that
already monkeypatch db paths are unaffected (their patch just wins twice)."""

from __future__ import annotations

import logging
import tempfile
import threading
from pathlib import Path

import pytest
from _isolation import leftover_threads

from brief import applog, config

# --- Logging never reaches the live data/logs/ (, found 2026-09-14) ---------
# brief modules configure applog AT IMPORT, i.e. during collection, before any
# fixture runs, so the test process's "brief" logger was attached to the LIVE
# data/logs/brief.log. The full suite looked clean only by accident: test_applog
# runs early and its teardown removes the handler for every later test. Any
# subset run leaked. Wild specimens, written into the operator's live log by one window's
# partial runs on 2026-09-14 (brief.log lines 7208-7209):
#   11:06:58 INFO  brief.dispatch_speak  bulletin skipped — in a meeting
#                  (microphone in use)
#   11:07:19 ERROR brief.gdelt  GDELT fetch FAILED (ConnectionError('gdelt down'))
# The first reads like a real speaker event and is not one. So: this runs at
# conftest import, before test modules are collected, and points applog at a
# throwaway directory. tests/test_log_isolation.py asserts it held.
TEST_LOG_DIR = Path(tempfile.mkdtemp(prefix="brief-test-logs-"))


def _redirect_brief_logging() -> None:
    root = logging.getLogger("brief")
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()
    applog._LOG_DIR = TEST_LOG_DIR
    applog._CONFIGURED = False
    applog.setup()


_redirect_brief_logging()
LIVE_DATA_DIR = config.DATA_DIR.resolve()

from brief import db  # noqa: E402
from brief.window import quiet  # noqa: E402


@pytest.fixture
def conftest_log_dir() -> Path:
    return TEST_LOG_DIR.resolve()


@pytest.fixture(autouse=True)
def _no_thread_outlives_its_test():
    """HS-2 thread-lifetime audit ( shape, a sibling project 2026-09-11): isolation by
    monkeypatch dies with the patch, so a thread still running after its test reads
    the RESTORED real paths (db.DATA_DIR, KEYWORDS_PATH, applog) and writes live
    data. `with TestClient(app)` starts the sweep/news/flight loops, whose stop()
    joins with a 5s timeout; a cycle stuck in a network call outlives it. Any
    thread started by a test and still alive after a short grace fails that test,
    by name, instead of leaking silently."""
    before = set(threading.enumerate())
    yield
    alive = leftover_threads(before)
    if alive:
        pytest.fail(
            "thread(s) outlived the test and could write live data: " + ", ".join(alive)
        )


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "brief.db")
    yield


@pytest.fixture(autouse=True)
def _no_real_calendar_check(monkeypatch):
    """quiet_reason() now checks Google Calendar (calendar_meeting_active) --
    a real cross-process/network call. Default it to "no meeting" for every
    test so the suite stays fast and network-free; tests exercising the
    calendar-gate specifically override this themselves."""
    monkeypatch.setattr(quiet, "calendar_meeting_active", lambda: False)
    yield
