"""The voice's silence rules (brief/window/quiet.py) — the operator's hard rule is
that it must never read anything out while he's in a meeting."""

from __future__ import annotations

import json
import time
from datetime import datetime

from brief.window import quiet, service

# --- individual signals -----------------------------------------------------


def test_manual_mute_file(tmp_path):
    mute = tmp_path / "speaker_mute"
    assert quiet.manual_mute(mute) is False
    mute.write_text("")
    assert quiet.manual_mute(mute) is True


def test_focus_active_reads_assertion_records(tmp_path):
    p = tmp_path / "Assertions.json"
    # Only historical invalidations -> Focus is OFF.
    p.write_text(json.dumps({"data": [{"storeInvalidationRecords": [{"x": 1}]}]}))
    assert quiet.focus_active(p) is False
    # An active assertion -> Focus is ON.
    p.write_text(json.dumps({"data": [{"storeAssertionRecords": [{"x": 1}]}]}))
    assert quiet.focus_active(p) is True


def test_focus_active_fail_soft_on_missing_or_garbage(tmp_path):
    assert quiet.focus_active(tmp_path / "nope.json") is False
    bad = tmp_path / "bad.json"
    bad.write_text("not json")
    assert quiet.focus_active(bad) is False


def test_quiet_hours_wraps_midnight():
    at = lambda h: datetime(2026, 7, 19, h, 0)  # noqa: E731
    assert quiet.in_quiet_hours(at(23), 23, 8) is True
    assert quiet.in_quiet_hours(at(3), 23, 8) is True
    assert quiet.in_quiet_hours(at(7), 23, 8) is True
    assert quiet.in_quiet_hours(at(8), 23, 8) is False
    assert quiet.in_quiet_hours(at(14), 23, 8) is False


# --- the combined decision --------------------------------------------------


def _noon():
    return datetime(2026, 7, 19, 12, 0)


def test_speaks_when_nothing_is_blocking(tmp_path, monkeypatch):
    monkeypatch.setattr(quiet, "mic_in_use", lambda: False)
    monkeypatch.setattr(quiet, "focus_active", lambda path=None: False)
    assert quiet.quiet_reason(_noon(), tmp_path / "mute") is None


def test_weekends_silence_the_voice_when_weekdays_only(tmp_path, monkeypatch):
    # the operator's rule: no voice on the weekend. Sat 2026-07-18 / Sun 2026-07-19.
    monkeypatch.setattr(quiet, "mic_in_use", lambda: False)
    monkeypatch.setattr(quiet, "focus_active", lambda path=None: False)
    mute = tmp_path / "mute"
    for day in (18, 19):  # Sat, Sun
        weekend = datetime(2026, 7, day, 12, 0)
        assert quiet.quiet_reason(weekend, mute, 17, 9, weekdays_only=True) == "weekend"
    # ...but Monday 2026-07-20 noon still speaks.
    monday = datetime(2026, 7, 20, 12, 0)
    assert quiet.quiet_reason(monday, mute, 17, 9, weekdays_only=True) is None
    # ...and with weekdays_only off, the weekend speaks too.
    assert quiet.quiet_reason(datetime(2026, 7, 18, 12, 0), mute, 17, 9) is None


def test_calendar_meeting_silences_the_voice(tmp_path, monkeypatch):
    # the operator: mic-in-use alone missed the case where call audio doesn't route
    # through this machine, or the meeting has JUST started (the bulletin's
    # own :00/:30 cadence collides with a meeting starting on the hour).
    monkeypatch.setattr(quiet, "mic_in_use", lambda: False)
    monkeypatch.setattr(quiet, "focus_active", lambda path=None: False)
    monkeypatch.setattr(quiet, "calendar_meeting_active", lambda: True)
    reason = quiet.quiet_reason(_noon(), tmp_path / "mute")
    assert reason == "a calendar meeting is in progress"


def test_calendar_check_can_be_disabled(tmp_path, monkeypatch):
    monkeypatch.setattr(quiet, "mic_in_use", lambda: False)
    monkeypatch.setattr(quiet, "focus_active", lambda path=None: False)
    monkeypatch.setattr(quiet, "calendar_meeting_active", lambda: True)
    assert (
        quiet.quiet_reason(_noon(), tmp_path / "mute", respect_calendar=False) is None
    )


def test_mic_in_use_silences_the_voice(tmp_path, monkeypatch):
    # THE rule: in a meeting -> say nothing.
    monkeypatch.setattr(quiet, "mic_in_use", lambda: True)
    monkeypatch.setattr(quiet, "focus_active", lambda path=None: False)
    reason = quiet.quiet_reason(_noon(), tmp_path / "mute")
    assert reason and "meeting" in reason


def test_focus_silences_the_voice(tmp_path, monkeypatch):
    monkeypatch.setattr(quiet, "mic_in_use", lambda: False)
    monkeypatch.setattr(quiet, "focus_active", lambda path=None: True)
    reason = quiet.quiet_reason(_noon(), tmp_path / "mute")
    assert reason and "Focus" in reason


def test_mute_file_beats_everything(tmp_path, monkeypatch):
    monkeypatch.setattr(quiet, "mic_in_use", lambda: False)
    monkeypatch.setattr(quiet, "focus_active", lambda path=None: False)
    mute = tmp_path / "mute"
    mute.write_text("")
    assert quiet.quiet_reason(_noon(), mute) == "muted"


def test_mic_check_never_raises():
    # Real call against CoreAudio — must return a bool, never blow up.
    assert isinstance(quiet.mic_in_use(), bool)


# --- nobody's at the machine (display idle) ---------------------------------


def test_display_probably_off_true_only_past_the_threshold(monkeypatch):
    monkeypatch.setattr(quiet, "display_idle_seconds", lambda: 25 * 60)
    assert quiet.display_probably_off(20) is True
    monkeypatch.setattr(quiet, "display_idle_seconds", lambda: 5 * 60)
    assert quiet.display_probably_off(20) is False


def test_display_idle_seconds_never_raises():
    # Real call against ioreg — must return a float, never blow up.
    assert isinstance(quiet.display_idle_seconds(), float)


def test_quiet_reason_ignores_display_idle_by_default(tmp_path, monkeypatch):
    # respect_display_idle defaults False -- must never gate the manual
    # button path (a click already proves someone's there).
    monkeypatch.setattr(quiet, "mic_in_use", lambda: False)
    monkeypatch.setattr(quiet, "focus_active", lambda path=None: False)
    monkeypatch.setattr(quiet, "display_probably_off", lambda minutes: True)
    assert quiet.quiet_reason(_noon(), tmp_path / "mute") is None


def test_quiet_reason_respects_display_idle_when_opted_in(tmp_path, monkeypatch):
    monkeypatch.setattr(quiet, "mic_in_use", lambda: False)
    monkeypatch.setattr(quiet, "focus_active", lambda path=None: False)
    monkeypatch.setattr(quiet, "display_probably_off", lambda minutes: True)
    reason = quiet.quiet_reason(_noon(), tmp_path / "mute", respect_display_idle=True)
    assert reason == "no one's at the machine"


def test_display_idle_checked_last_behind_a_meeting(tmp_path, monkeypatch):
    # A meeting still wins even if the machine also reads idle -- e.g. mic
    # picked up before any HID input this session.
    monkeypatch.setattr(quiet, "mic_in_use", lambda: True)
    monkeypatch.setattr(quiet, "focus_active", lambda path=None: False)
    monkeypatch.setattr(quiet, "display_probably_off", lambda minutes: True)
    reason = quiet.quiet_reason(_noon(), tmp_path / "mute", respect_display_idle=True)
    assert reason and "meeting" in reason


# --- spoken phrasing --------------------------------------------------------


def test_speechify_expands_abbreviations_and_trims():
    out = service._speechify("M6.2 earthquake — 90 km SW of Puerto Madero")
    assert "magnitude 6.2" in out
    assert "90 kilometers" in out
    assert "—" not in out
    long = service._speechify("word " * 100)
    assert len(long) <= 181 and long.endswith("…")


# --- the board's mute button ------------------------------------------------


def test_voice_endpoint_and_mute_button_round_trip(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from brief.window.app import create_app

    monkeypatch.setattr(service.db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(service.db, "DB_PATH", tmp_path / "brief.db")
    monkeypatch.setattr(quiet, "mic_in_use", lambda: False)
    monkeypatch.setattr(quiet, "focus_active", lambda path=None: False)

    app = create_app(
        world_feeds=[],
        window_cfg={
            "sweep_interval_seconds": 5,
            "news_interval_seconds": 5,
            # widen quiet hours off so the test is time-independent
            "speak_quiet_start_hour": 0,
            "speak_quiet_end_hour": 0,
        },
        news_sources=[],
    )
    with TestClient(app) as client:
        assert client.get("/api/voice").json()["muted"] is False

        # Button ON -> muted, and the reason reflects the manual switch.
        assert client.post("/api/voice/mute?on=true").json()["muted"] is True
        v = client.get("/api/voice").json()
        assert v["muted"] is True and v["can_speak"] is False
        assert v["reason"] == "muted"
        # The speaker daemon honours the very same file.
        assert (tmp_path / "speaker_mute").exists()

        # Button OFF -> speaking again.
        assert client.post("/api/voice/mute?on=false").json()["muted"] is False
        v2 = client.get("/api/voice").json()
        assert v2["muted"] is False and v2["can_speak"] is True


# --- the Jarvis handoff -----------------------------------------------------


def test_jarvis_claim_defers_then_expires(tmp_path, monkeypatch):
    claim = tmp_path / "voice_claim_jarvis"
    assert quiet.jarvis_has_voice(claim) is False  # no claim -> we speak

    claim.write_text("")  # Jarvis heartbeats
    assert quiet.jarvis_has_voice(claim) is True

    # A stale heartbeat (Jarvis died / was killed to protect a render) hands
    # the voice straight back to the light speaker.
    import os

    old = time.time() - 300
    os.utime(claim, (old, old))
    assert quiet.jarvis_has_voice(claim, max_age_seconds=120) is False


def test_quiet_reason_defers_to_jarvis(tmp_path, monkeypatch):
    monkeypatch.setattr(quiet, "mic_in_use", lambda: False)
    monkeypatch.setattr(quiet, "focus_active", lambda path=None: False)
    claim = tmp_path / "voice_claim_jarvis"
    claim.write_text("")
    reason = quiet.quiet_reason(
        datetime(2026, 7, 20, 12, 0), tmp_path / "mute", jarvis_claim_path=claim
    )
    assert reason == "Jarvis has the voice"
