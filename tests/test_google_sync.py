"""Tests for brief.window.google_sync -- Dispatch's own Google Calendar
credential (P1-C). Network-free: _accounts()/_token_files()/_events_today()
are monkeypatched directly, never touching the real Google API, token files,
or Keychain."""

from __future__ import annotations

import datetime as dt

import pytest

from brief.window import google_sync


def _event(start_offset_min, end_offset_min, **overrides):
    now = dt.datetime.now().astimezone()
    start = now + dt.timedelta(minutes=start_offset_min)
    end = now + dt.timedelta(minutes=end_offset_min)
    ev = {
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": end.isoformat()},
    }
    ev.update(overrides)
    return ev


class _RecordingLog:
    """Stands in for the module logger so a test can assert that a degraded
    answer announced itself, without depending on applog's propagate=False."""

    def __init__(self):
        self.warnings: list[str] = []

    def warning(self, msg, *args):
        self.warnings.append(msg % args if args else msg)

    def info(self, msg, *args):
        pass


def _one_account(monkeypatch, events, label="personal"):
    """A single authorized account whose primary calendar holds `events`."""
    monkeypatch.setattr(google_sync, "_token_files", lambda: [f"{label}.json"])
    monkeypatch.setattr(google_sync, "_accounts", lambda: [(label, object())])
    monkeypatch.setattr(
        google_sync, "_events_today", lambda creds, cal_id="primary": events
    )


def test_no_token_raises(monkeypatch):
    monkeypatch.setattr(google_sync, "_token_files", lambda: [])
    with pytest.raises(RuntimeError, match="no Dispatch google token"):
        google_sync.is_in_meeting_now()


def test_tokens_present_but_none_readable_raises(monkeypatch):
    # Distinct from "no token at all": the difference between never set up and
    # set up but broken is a real one, and collapsing them hides a rotted token.
    monkeypatch.setattr(google_sync, "_token_files", lambda: ["work.json"])
    monkeypatch.setattr(google_sync, "_accounts", lambda: [])
    with pytest.raises(RuntimeError, match="none readable"):
        google_sync.is_in_meeting_now()


def test_true_for_a_current_event_with_another_attendee(monkeypatch):
    _one_account(monkeypatch, [_event(-10, 10, attendees=[{"self": False}])])
    assert google_sync.is_in_meeting_now() is True


def test_false_for_a_solo_hold_with_no_call_signal(monkeypatch):
    # REGRESSION (the voice assistant project, 2026-07-21): a self-organized block with no
    # other attendee and no video link must not count as a real call.
    _one_account(monkeypatch, [_event(-10, 10)])
    assert google_sync.is_in_meeting_now() is False


def test_true_for_a_video_link_in_the_location(monkeypatch):
    _one_account(monkeypatch, [_event(-5, 5, location="https://zoom.us/j/12345")])
    assert google_sync.is_in_meeting_now() is True


def test_false_for_an_event_that_has_not_started(monkeypatch):
    _one_account(monkeypatch, [_event(30, 60, attendees=[{"self": False}])])
    assert google_sync.is_in_meeting_now() is False


def test_false_for_an_all_day_event(monkeypatch):
    _one_account(
        monkeypatch, [{"start": {"date": "2026-08-16"}, "end": {"date": "2026-08-17"}}]
    )
    assert google_sync.is_in_meeting_now() is False


def test_meeting_on_the_SECOND_account_is_found(monkeypatch):
    # THE regression this module was rewritten for (the operator, 2026-09-08: "I have 2
    # calendars"). P1-C's first version read one account, so a client call on the
    # work calendar returned False -- the same answer as "genuinely free". If this
    # test ever goes red, the voice can talk over a live call.
    personal, work = object(), object()
    monkeypatch.setattr(
        google_sync, "_token_files", lambda: ["personal.json", "work.json"]
    )
    monkeypatch.setattr(
        google_sync, "_accounts", lambda: [("personal", personal), ("work", work)]
    )
    calls: list[str] = []

    def fake_events(creds, cal_id="primary"):
        if creds is personal:
            calls.append("personal")
            return []  # nothing on the personal calendar
        calls.append("work")
        return [_event(-10, 10, attendees=[{"self": False}])]

    monkeypatch.setattr(google_sync, "_events_today", fake_events)

    assert google_sync.is_in_meeting_now() is True
    assert "personal" in calls and "work" in calls, (
        f"both accounts must be consulted before answering, saw {calls}"
    )


def test_partial_visibility_warns_before_answering_not_in_a_meeting(monkeypatch):
    # A False built from fewer calendars than exist is indistinguishable from a
    # genuine "free". It is still returned (fail-soft beats silencing the board),
    # but it must never be returned SILENTLY.
    recording = _RecordingLog()
    monkeypatch.setattr(google_sync, "log", recording)
    monkeypatch.setattr(
        google_sync, "_token_files", lambda: ["personal.json", "work.json"]
    )
    monkeypatch.setattr(google_sync, "_accounts", lambda: [("personal", object())])
    monkeypatch.setattr(
        google_sync, "_events_today", lambda creds, cal_id="primary": []
    )

    assert google_sync.is_in_meeting_now() is False
    assert any("PARTIAL" in w for w in recording.warnings), (
        f"degraded answer must announce itself, got {recording.warnings}"
    )
    assert any("1 of 2" in w for w in recording.warnings)


def test_full_visibility_answers_false_without_warning(monkeypatch):
    # The precondition that keeps the test above honest: the warning fires
    # because visibility was partial, not on every False.
    recording = _RecordingLog()
    monkeypatch.setattr(google_sync, "log", recording)
    monkeypatch.setattr(
        google_sync, "_token_files", lambda: ["personal.json", "work.json"]
    )
    monkeypatch.setattr(
        google_sync,
        "_accounts",
        lambda: [("personal", object()), ("work", object())],
    )
    monkeypatch.setattr(
        google_sync, "_events_today", lambda creds, cal_id="primary": []
    )

    assert google_sync.is_in_meeting_now() is False
    assert recording.warnings == []


def test_an_unreadable_calendar_does_not_abort_the_other_accounts(monkeypatch):
    good = object()
    bad = object()
    monkeypatch.setattr(google_sync, "_token_files", lambda: ["a.json", "b.json"])
    monkeypatch.setattr(
        google_sync, "_accounts", lambda: [("bad", bad), ("good", good)]
    )

    def fake_events(creds, cal_id="primary"):
        if creds is bad:
            raise OSError("calendar API exploded")
        return [_event(-10, 10, attendees=[{"self": False}])]

    monkeypatch.setattr(google_sync, "_events_today", fake_events)
    monkeypatch.setattr(google_sync, "log", _RecordingLog())

    assert google_sync.is_in_meeting_now() is True


def test_extra_calendars_from_env_are_consulted(monkeypatch):
    monkeypatch.setenv("DISPATCH_EXTRA_CALENDARS", "shared@group.calendar.google.com")
    assert google_sync._calendar_ids() == [
        "primary",
        "shared@group.calendar.google.com",
    ]
    monkeypatch.delenv("DISPATCH_EXTRA_CALENDARS")
    assert google_sync._calendar_ids() == ["primary"]


def test_authorize_rejects_a_label_that_would_escape_the_token_dir(
    monkeypatch, tmp_path
):
    # _CREDS is pointed at a nonexistent file ON PURPOSE. The real client JSON
    # exists on this box, so if the label guard ever weakened, authorize() would
    # reach run_local_server() and the SUITE WOULD HANG instead of failing --
    # a test that cannot report bad news. With no creds file the fallthrough is
    # a fast "Missing ..." string, so a broken guard fails loudly in millisecs.
    monkeypatch.setattr(google_sync, "_CREDS", tmp_path / "nope.json")
    assert "Bad account label" in google_sync.authorize("../../etc/passwd")
    assert "Bad account label" in google_sync.authorize("")
    assert "Bad account label" in google_sync.authorize("  ")
    # precondition: the fallthrough really is the harmless one
    assert "Missing" in google_sync.authorize("work")
