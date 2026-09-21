"""Tests for calendar_check.in_meeting_now() -- the extra Google-Calendar
meeting signal via Dispatch's own OAuth credential (brief.window.google_sync,
P1-C). Network-free: is_in_meeting_now() is monkeypatched directly, never
touching the real Google API, token file, or Keychain."""

from __future__ import annotations

from brief.window import calendar_check, google_sync


def test_true_when_calendar_reports_a_meeting(monkeypatch):
    monkeypatch.setattr(google_sync, "is_in_meeting_now", lambda: True)
    assert calendar_check.in_meeting_now() is True


def test_false_when_calendar_reports_no_meeting(monkeypatch):
    monkeypatch.setattr(google_sync, "is_in_meeting_now", lambda: False)
    assert calendar_check.in_meeting_now() is False


def test_fails_soft_when_no_token_yet(monkeypatch):
    def _raise():
        raise RuntimeError(
            "no Dispatch google token -- run google_sync.authorize() first"
        )

    monkeypatch.setattr(google_sync, "is_in_meeting_now", _raise)
    assert calendar_check.in_meeting_now() is False


def test_fails_soft_when_the_calendar_call_itself_raises(monkeypatch):
    def _raise():
        raise RuntimeError("Google API unreachable")

    monkeypatch.setattr(google_sync, "is_in_meeting_now", _raise)
    assert calendar_check.in_meeting_now() is False
