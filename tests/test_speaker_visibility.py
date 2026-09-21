"""HS-5: the speaker daemon is visible -- a stamp at start, a heartbeat per poll,
its own log file, and a status reader that checks the stamp against launchd.

Every path is a tmp path; nothing under the live data/ is read or written.
"""

from __future__ import annotations

import importlib.util
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def sp(tmp_path, monkeypatch):
    mod = _load("dispatch_speak")
    monkeypatch.setattr(mod, "STAMP_PATH", tmp_path / "speaker_stamp.json")
    monkeypatch.setattr(mod, "STATE_PATH", tmp_path / "speaker_seen.json")
    monkeypatch.setattr(mod, "MUTE_PATH", tmp_path / "speaker_mute")
    monkeypatch.setattr(mod, "JARVIS_CLAIM_PATH", tmp_path / "claim")
    monkeypatch.setattr(mod.kokoro_tts, "clear_prerendered", lambda: None)
    monkeypatch.setattr(mod, "maybe_prerender", lambda now, pending, pre: pre)
    return mod


class _StopLoop(Exception):
    pass


def _run_main_for_polls(sp, monkeypatch, polls: int) -> None:
    ticks = {"n": 0}

    def fake_sleep(_seconds):
        ticks["n"] += 1
        if ticks["n"] >= polls:
            raise _StopLoop
        monkeypatch.setattr(sp.time, "monotonic", lambda: 1e12 * (ticks["n"] + 1))

    monkeypatch.setattr(sp.time, "sleep", fake_sleep)
    with pytest.raises(_StopLoop):
        sp.main()


# --- the stamp -------------------------------------------------------------------


def test_importing_the_speaker_does_not_redirect_this_process_log(monkeypatch):
    monkeypatch.delenv("BRIEF_LOG_FILE", raising=False)
    _load("dispatch_speak")
    assert "BRIEF_LOG_FILE" not in os.environ


def test_stamp_is_written_at_start_before_the_first_poll(sp, monkeypatch):
    seen_at_first_poll = {}

    def fake_run_once(seen, first_run, last_slot=None, pending=None):
        seen_at_first_poll.update(json.loads(sp.STAMP_PATH.read_text()))
        return seen, last_slot, pending or []

    monkeypatch.setattr(sp, "run_once", fake_run_once)
    _run_main_for_polls(sp, monkeypatch, polls=1)
    assert seen_at_first_poll["pid"] == os.getpid()
    assert seen_at_first_poll["polls"] == 0
    assert seen_at_first_poll["last_poll_at"] is None
    assert seen_at_first_poll["git_sha"] == sp.version.GIT_SHA
    assert seen_at_first_poll["source_hash"] == sp.version.STARTED_FP
    assert seen_at_first_poll["version"] == sp.version.VERSION
    assert seen_at_first_poll["service"] == "dispatch-speaker"


def test_every_poll_moves_the_heartbeat_success_and_failure(sp, monkeypatch):
    outcomes = iter([None, ConnectionError("engine down"), None])

    def fake_run_once(seen, first_run, last_slot=None, pending=None):
        exc = next(outcomes)
        if exc:
            raise exc
        return seen, last_slot, pending or []

    monkeypatch.setattr(sp, "run_once", fake_run_once)
    stamps = []
    real_write = sp.write_stamp
    monkeypatch.setattr(
        sp, "write_stamp", lambda s, path=None: (stamps.append(dict(s)), real_write(s))
    )
    _run_main_for_polls(sp, monkeypatch, polls=3)
    polled = [s for s in stamps if s["polls"]]
    assert [s["polls"] for s in polled] == [1, 2, 3]
    assert [s["last_poll_ok"] for s in polled] == [True, False, True]
    assert "engine down" in polled[1]["last_error"]
    assert polled[2]["last_error"] is None
    final = json.loads(sp.STAMP_PATH.read_text())
    assert final["polls"] == 3 and final["last_poll_ok"] is True


def test_stamp_write_is_atomic_and_never_raises(sp, tmp_path):
    sp.write_stamp({"pid": 1}, tmp_path / "s.json")
    assert json.loads((tmp_path / "s.json").read_text()) == {"pid": 1}
    assert not list(tmp_path.glob("*.tmp"))
    blocked = tmp_path / "blocked"
    blocked.write_text("a file where a directory should be")
    sp.write_stamp({"pid": 1}, blocked / "s.json")  # must not raise


# --- the status reader -------------------------------------------------------------

NOW = datetime(2026, 9, 14, 15, 0, 0, tzinfo=timezone.utc)
# The wild specimen (2026-09-13 speaker restart): launchctl list columns are TABS.
LISTING = (
    "PID\tStatus\tLabel\n"
    "22552\t-15\tcom.local.dispatch\n"
    "30646\t-15\tcom.local.dispatch-speaker\n"
    "840\t0\tcom.local.dispatch-shell\n"
)


@pytest.fixture
def ss():
    return _load("speaker_status")


def _stamp(tmp_path, **over) -> Path:
    stamp = {
        "service": "dispatch-speaker",
        "pid": 30646,
        "started_at": (NOW - timedelta(minutes=10)).isoformat(),
        "git_sha": "c49cd511ff444dfb68d4f11532c4c37ecff035ea",
        "version": "1.0.0",
        "source_hash": "e3efebdee99f107f",
        "python_version": "3.13.9",
        "sqlite_version": "3.50.2",
        "poll_seconds": 60,
        "polls": 9,
        "last_poll_at": (NOW - timedelta(seconds=40)).isoformat(),
        "last_poll_ok": True,
        "last_error": None,
    }
    stamp.update(over)
    path = tmp_path / "speaker_stamp.json"
    path.write_text(json.dumps(stamp))
    return path


LIVE_FP = "e3efebdee99f107f"  # disk now, matching the stamp's source_hash


def test_status_healthy_line(ss, tmp_path):
    ok, line = ss.status(LISTING, _stamp(tmp_path), LIVE_FP, now=NOW)
    assert ok, line
    assert "speaker pid 30646" in line and "version 1.0.0" in line
    assert "running c49cd51" in line and "source_hash e3efebdee99f107f" in line
    assert "source_stale false" in line and "last poll 40s ago ok" in line


def test_launchd_row_requires_tab_columns_and_exact_label(ss):
    assert ss.launchd_row("com.local.dispatch-speaker", LISTING) == ("30646", "-15")
    # The failure that printed STABLE on 09-13: a space-separated match. A listing
    # without tabs must not produce a row, and a label prefix must not match.
    spaced = LISTING.replace("\t", " ")
    assert ss.launchd_row("com.local.dispatch-speaker", spaced) is None
    assert ss.launchd_row("com.local.dispatch", LISTING) == ("22552", "-15")
    assert ss.launchd_row("com.local.dispatch-spea", LISTING) is None


def test_status_fails_without_a_launchd_row(ss, tmp_path):
    ok, line = ss.status(
        LISTING.replace("\t", " "), _stamp(tmp_path), "c49cd51", now=NOW
    )
    assert not ok and "no launchd row" in line


def test_status_stopped_is_healthy(ss, tmp_path):
    listing = LISTING.replace(
        "30646\t-15\tcom.local.dispatch-speaker", "-\t0\tcom.local.dispatch-speaker"
    )
    ok, line = ss.status(listing, tmp_path / "absent.json", "c49cd51", now=NOW)
    assert ok and "stopped" in line


def test_status_fails_when_running_but_never_stamped(ss, tmp_path):
    ok, line = ss.status(LISTING, tmp_path / "absent.json", "c49cd51", now=NOW)
    assert not ok and "pre-HS-5" in line


def test_status_fails_on_a_stamp_from_an_earlier_run(ss, tmp_path):
    ok, line = ss.status(LISTING, _stamp(tmp_path, pid=26637), "c49cd51", now=NOW)
    assert not ok and "earlier run" in line


def test_status_fails_on_a_hung_loop_with_a_live_pid(ss, tmp_path):
    stale = (NOW - timedelta(minutes=5)).isoformat()
    ok, line = ss.status(
        LISTING, _stamp(tmp_path, last_poll_at=stale), "c49cd51", now=NOW
    )
    assert not ok and "loop hung" in line


def test_status_reports_stale_source_and_a_failing_poll_without_failing(ss, tmp_path):
    path = _stamp(tmp_path, last_poll_ok=False, last_error="ConnectionError('x')")
    ok, line = ss.status(LISTING, path, "0000aaaa1111bbbb", now=NOW)  # disk moved
    assert ok
    assert "source_stale true" in line and "FAILED ConnectionError('x')" in line


def test_status_unknown_fingerprint_is_none_not_false(ss, tmp_path):
    # A pre-HS-1 stamp has no source_hash: unknown, never "fresh".
    ok, line = ss.status(LISTING, _stamp(tmp_path, source_hash=None), LIVE_FP, now=NOW)
    assert ok and "source_stale none" in line and "source_hash unverifiable" in line
    # And a live side that cannot be hashed is equally unknown.
    ok, line = ss.status(LISTING, _stamp(tmp_path), None, now=NOW)
    assert ok and "source_stale none" in line


def test_status_docs_only_commit_is_not_stale(ss, tmp_path):
    """The sha differs from HEAD but the fingerprint matches: fresh. The reader no
    longer looks at HEAD at all."""
    ok, line = ss.status(
        LISTING, _stamp(tmp_path, git_sha="36fc552" + "0" * 33), LIVE_FP, now=NOW
    )
    assert ok and "source_stale false" in line
