"""Test isolation: every test runs against a throwaway SQLite DB in tmp_path,
never the live data/brief.db the wall display is serving. Individual tests that
already monkeypatch db paths are unaffected (their patch just wins twice)."""

from __future__ import annotations

import pytest

from brief import db
from brief.window import quiet


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
