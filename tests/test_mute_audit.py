"""SEC-2: every flip of the voice mute switch leaves a log line naming its origin.

THE WILD SPECIMEN (2026-09-21). `data/speaker_mute` existed at 01:01 -- the
speaker logged "bulletin skipped — muted" -- and was gone by 09:44, with
`/api/voice` answering `{"muted": false}`. Nothing in `brief.log`, `speaker.log`
or anywhere else could say what removed it. Asked directly, the owner said it was
him and to leave the voice live, so nothing was actually wrong. The defect is
that the record could not answer the question at all, about the one switch the
project guards with a standing ABSOLUTE rule.

This is the RECORD half of the finding, deliberately split from the token gate
(SEC-1, architecture review ruling 2026-09-21, ): the gate changes a surface the
owner uses from his phone and needs his word on the shape first; the audit line
needs nobody's permission and is the half that answers "who moved it".

BIRTH TEST. Every assertion here fails against `api_voice_mute` as it stood on
the morning of 2026-09-21, which logged nothing at all.
"""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from brief.window import quiet
from brief.window.app import create_app


@pytest.fixture
def client(monkeypatch, tmp_path):
    """An app whose DATA_DIR is a temp dir, so the switch these tests flip is
    never the live one. T-48/T-52: the suite must not touch live data, and this
    file's whole subject is a file that lives in the live data dir."""
    from brief.window import service as service_mod

    monkeypatch.setattr(service_mod.db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(quiet, "mic_in_use", lambda: False)
    monkeypatch.setattr(quiet, "focus_active", lambda path=None: False)
    app = create_app(world_feeds=[], window_cfg={}, news_sources=[])
    return TestClient(app), tmp_path


def _mute_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if "VOICE MUTE" in r.getMessage()]


def test_muting_logs_the_change_and_the_origin(client, caplog):
    tc, tmp_path = client
    with caplog.at_level(logging.WARNING, logger="brief.app"):
        assert tc.post("/api/voice/mute?on=true").json() == {"muted": True}

    lines = _mute_lines(caplog)
    assert len(lines) == 1, f"expected exactly one audit line, got {lines}"
    assert "changed" in lines[0]
    assert "off -> on" in lines[0]
    # The origin is the point of the whole item: a line that says the switch
    # moved but not what moved it would not have answered 2026-09-21 either.
    assert "testclient" in lines[0]
    assert (tmp_path / "speaker_mute").exists()


def test_unmuting_logs_the_change(client, caplog):
    """The direction that actually happened on 2026-09-21 and left no trace."""
    tc, tmp_path = client
    (tmp_path / "speaker_mute").touch()
    with caplog.at_level(logging.WARNING, logger="brief.app"):
        assert tc.post("/api/voice/mute?on=false").json() == {"muted": False}

    lines = _mute_lines(caplog)
    assert len(lines) == 1
    assert "changed" in lines[0]
    assert "on -> off" in lines[0]
    assert not (tmp_path / "speaker_mute").exists()


def test_a_no_op_flip_is_logged_too_as_re_asserted(client, caplog):
    """Logged even when the state does not move.

    "Something pushed mute=off while it was already off" is the trace that tells
    a stuck client apart from a person, and it is invisible if only changes are
    recorded. It is marked `re-asserted` rather than `changed` so reading the log
    for what actually moved stays easy.
    """
    tc, _ = client
    with caplog.at_level(logging.WARNING, logger="brief.app"):
        tc.post("/api/voice/mute?on=false")  # already off

    lines = _mute_lines(caplog)
    assert len(lines) == 1
    assert "re-asserted" in lines[0]
    assert "changed" not in lines[0]


def test_the_audit_line_is_warning_not_info(client, caplog):
    """INFO would bury it. The speaker writes an INFO line every half hour it
    skips a bulletin; on 2026-09-21 there were dozens, and the one event worth
    finding would have been among them rather than above them."""
    tc, _ = client
    with caplog.at_level(logging.INFO, logger="brief.app"):
        tc.post("/api/voice/mute?on=true")

    records = [r for r in caplog.records if "VOICE MUTE" in r.getMessage()]
    assert records and all(r.levelno >= logging.WARNING for r in records)


def test_the_audit_line_carries_no_credentials(client, caplog):
    """Origin means peer address and user-agent. An Authorization header, a
    cookie or a token must never reach the log -- SEC-1 will put a token on this
    very endpoint, and an audit line that logged it would turn the fix into a
    credential leak in a file the host monitor tails."""
    tc, _ = client
    with caplog.at_level(logging.WARNING, logger="brief.app"):
        tc.post(
            "/api/voice/mute?on=true",
            headers={
                "Authorization": "Bearer sekrit-token-value",
                "Cookie": "session=sekrit-cookie-value",
                "X-Api-Key": "sekrit-key-value",
            },
        )

    blob = " ".join(_mute_lines(caplog))
    assert blob, "nothing logged at all"
    for secret in ("sekrit-token-value", "sekrit-cookie-value", "sekrit-key-value"):
        assert secret not in blob
    assert "Bearer" not in blob


def test_a_failed_flip_is_logged_as_failed(client, caplog, monkeypatch):
    """A switch that REFUSED to move is at least as interesting as one that
    moved, and the 500 goes to a caller who may not be watching."""
    tc, tmp_path = client

    def _boom(*a, **k):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(type(tmp_path / "x"), "touch", _boom)
    with caplog.at_level(logging.WARNING, logger="brief.app"):
        r = tc.post("/api/voice/mute?on=true")

    assert r.status_code == 500
    lines = _mute_lines(caplog)
    assert len(lines) == 1
    assert "FAILED" in lines[0]
    assert "read-only filesystem" in lines[0]
