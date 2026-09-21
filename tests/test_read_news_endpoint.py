"""POST /api/voice/read_news — the board's on-demand "read the news" button
(the operator: a small Jarvis orb he clicks at his desk). Speaking runs in a
background thread; these tests replace threading.Thread with a synchronous
stand-in so the speak call happens deterministically within the test."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from brief.window import app as app_module
from brief.window import kokoro_tts, quiet
from brief.window.app import create_app


class _SyncThread:
    """threading.Thread stand-in that runs its target immediately and
    synchronously in start() -- deterministic, no real background thread."""

    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}

    def start(self):
        self._target(*self._args, **self._kwargs)


def _iso(minutes_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


class _FixedDatetime(datetime):
    """Stand-in for the datetime CLASS imported into app_module -- real
    datetime.datetime can't have .now monkeypatched directly (immutable
    C type), but the NAME `datetime` in app_module's namespace can be
    rebound to this instead. Injects a fixed clock so these tests don't
    flake depending on what time it actually is when they run (real
    incident: failed every morning inside the default 23:00-08:00 quiet
    window, since the endpoint calls the real wall clock)."""

    @classmethod
    def now(cls, tz=None):
        return datetime(
            2026, 7, 20, 12, 0, tzinfo=tz
        )  # noon -- outside any quiet window


def _client(monkeypatch, *, mic_in_use=False):
    monkeypatch.setattr(quiet, "mic_in_use", lambda: mic_in_use)
    monkeypatch.setattr(quiet, "focus_active", lambda path=None: False)
    monkeypatch.setattr(app_module.threading, "Thread", _SyncThread)
    monkeypatch.setattr(app_module, "datetime", _FixedDatetime)
    app = create_app(world_feeds=[], window_cfg={}, news_sources=[])
    return app, TestClient(app)


def test_blocks_in_a_meeting_and_never_speaks(monkeypatch):
    app, client = _client(monkeypatch, mic_in_use=True)
    app.state.window_state.record_news(
        [{"title": "Story", "source_name": "A", "url": "u1", "published_at": _iso(1)}]
    )
    spoke = {}
    monkeypatch.setattr(
        kokoro_tts, "speak_or_say", lambda *a, **k: spoke.setdefault("called", True)
    )

    r = client.post("/api/voice/read_news")
    body = r.json()
    assert body == {"ok": False, "reason": "in a meeting (microphone in use)"}
    assert "called" not in spoke  # the ONE unconditional gate held


def test_calendar_meeting_does_not_block_a_manual_click(monkeypatch):
    # the operator, 2026-07-21: "I'm OK with a calendar hold, but if I click the
    # button manually.. I'd like it to read the news." A calendar hold is a
    # PREDICTION he's busy, not evidence he actually is -- unlike mute/mic-
    # in-use, an explicit click outweighs it. Real incident: a self-organized
    # calendar block outlasted the actual (already-ended) call and silently
    # blocked the button.
    app, client = _client(monkeypatch, mic_in_use=False)
    monkeypatch.setattr(quiet, "calendar_meeting_active", lambda: True)
    app.state.window_state.record_news(
        [{"title": "Story", "source_name": "A", "url": "u1", "published_at": _iso(1)}]
    )
    spoken = []
    monkeypatch.setattr(
        kokoro_tts, "speak_or_say", lambda text, **kw: spoken.append(text)
    )

    r = client.post("/api/voice/read_news")
    body = r.json()
    assert body["ok"] is True
    assert spoken  # actually spoke, calendar hold notwithstanding
    assert any("Story" in t for t in spoken)  # the real digest, not just the ack


def test_calendar_meeting_still_gates_the_scheduled_style_voice_check(monkeypatch):
    # /api/voice (drives the orb/ambient status, and the scheduled bulletin's
    # own gate) is UNCHANGED -- only the manual button skips the calendar
    # check. Confirms the two paths didn't accidentally converge.
    monkeypatch.setattr(quiet, "mic_in_use", lambda: False)
    monkeypatch.setattr(quiet, "focus_active", lambda path=None: False)
    monkeypatch.setattr(quiet, "calendar_meeting_active", lambda: True)
    app = create_app(world_feeds=[], window_cfg={}, news_sources=[])
    client = TestClient(app)
    r = client.get("/api/voice")
    assert r.json() == {
        "can_speak": False,
        "reason": "a calendar meeting is in progress",
        "muted": False,
    }


def test_manual_mute_blocks_the_button_no_exceptions(monkeypatch, tmp_path):
    # REGRESSION (live incident, 2026-07-21): an earlier version let this
    # endpoint override manual mute ("he clicked, he means it"). the operator hit
    # mute mid-call and it spoke anyway. Mute means mute, full stop -- no
    # override for how the speech was triggered.
    from brief.window import service as service_mod

    monkeypatch.setattr(service_mod.db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(quiet, "mic_in_use", lambda: False)  # not even in a meeting
    monkeypatch.setattr(quiet, "focus_active", lambda path=None: False)
    monkeypatch.setattr(app_module.threading, "Thread", _SyncThread)
    app = create_app(world_feeds=[], window_cfg={}, news_sources=[])
    client = TestClient(app)
    app.state.window_state.record_news(
        [{"title": "Story", "source_name": "A", "url": "u1", "published_at": _iso(1)}]
    )
    (tmp_path / "speaker_mute").touch()  # the manual mute switch is ON

    spoke = {}
    monkeypatch.setattr(
        kokoro_tts, "speak_or_say", lambda *a, **k: spoke.setdefault("called", True)
    )

    r = client.post("/api/voice/read_news")
    body = r.json()
    assert body == {"ok": False, "reason": "muted"}
    assert "called" not in spoke  # never speaks while muted, regardless of the click


def test_returns_ok_false_when_nothing_to_report(monkeypatch):
    _app, client = _client(monkeypatch)  # empty state -- no news recorded
    r = client.post("/api/voice/read_news")
    assert r.json() == {"ok": False, "reason": "nothing to report right now"}


def test_speaks_the_digest_via_kokoro_with_configured_voice(monkeypatch):
    monkeypatch.setattr(quiet, "mic_in_use", lambda: False)
    monkeypatch.setattr(quiet, "focus_active", lambda path=None: False)
    monkeypatch.setattr(app_module.threading, "Thread", _SyncThread)
    monkeypatch.setattr(app_module, "datetime", _FixedDatetime)
    app = create_app(
        world_feeds=[],
        window_cfg={
            "speak_engine": "kokoro",
            "speak_kokoro_voice": "bm_george",
            "speak_kokoro_lang": "en-gb",
            "speak_kokoro_speed": 1.0,
        },
        news_sources=[],
    )
    client = TestClient(app)
    app.state.window_state.record_news(
        [
            {
                "title": "Big story",
                "source_name": "A",
                "url": "u1",
                "published_at": _iso(1),
                "dupe_count": 5,
            }
        ]
    )
    spoken = []
    monkeypatch.setattr(
        kokoro_tts,
        "speak_or_say",
        lambda text, **kw: spoken.append((text, kw)),
    )

    r = client.post("/api/voice/read_news")
    body = r.json()
    assert body["ok"] is True
    assert body["lines"] >= 2  # lead-in + at least one headline

    texts = [t for t, _ in spoken]
    # Instant ack leads, forced onto the `say` engine regardless of config --
    # never waits on a cold Kokoro model load.
    assert texts[0] == "One moment, sir -- pulling up the news."
    assert spoken[0][1].get("engine") == "say"
    assert texts[1] == "Here's the news, sir."
    assert any("Big story" in t for t in texts)
    # The actual digest lines (excluding the forced-`say` ack) use the
    # configured voice, not some hardcoded default.
    digest_kwargs = [kw for _, kw in spoken[1:]]
    assert all(kw.get("engine") == "kokoro" for kw in digest_kwargs)
    assert all(kw.get("voice") == "bm_george" for kw in digest_kwargs)


def test_client_speak_or_say_never_called_when_blocked_by_meeting(monkeypatch):
    # Belt-and-suspenders on the real kokoro_tts.speak_or_say symbol (not a
    # mocked stand-in) -- patch subprocess/available so a real call would be
    # observable, and confirm it truly never fires when mic_in_use is True.
    called = {"n": 0}
    monkeypatch.setattr(
        kokoro_tts,
        "available",
        lambda: (called.__setitem__("n", called["n"] + 1), False)[1],
    )
    app, client = _client(monkeypatch, mic_in_use=True)
    app.state.window_state.record_news(
        [{"title": "Story", "source_name": "A", "url": "u1", "published_at": _iso(1)}]
    )
    client.post("/api/voice/read_news")
    assert called["n"] == 0


def test_second_click_while_speaking_is_refused_not_stacked(monkeypatch):
    # Real incident (2026-08-05): a click while a previous read was still
    # talking spawned a COMPETING thread -- concurrent Kokoro (onnxruntime)
    # sessions fighting for the same CPU cores turned a few-second read into
    # one that took two full minutes. The lock must refuse outright, never
    # queue or stack a second render on top of the first.
    app, client = _client(monkeypatch)
    app.state.window_state.record_news(
        [{"title": "Story", "source_name": "A", "url": "u1", "published_at": _iso(1)}]
    )
    spoke = {}
    monkeypatch.setattr(
        kokoro_tts, "speak_or_say", lambda *a, **k: spoke.setdefault("called", True)
    )
    assert app_module._speak_lock.acquire(blocking=False)  # simulate "already speaking"
    try:
        r = client.post("/api/voice/read_news")
        assert r.json() == {"ok": False, "reason": "already reading the news"}
        assert "called" not in spoke  # never even tried to speak on top of it
    finally:
        app_module._speak_lock.release()


def test_lock_releases_after_speaking_so_the_next_click_works(monkeypatch):
    app, client = _client(monkeypatch)
    app.state.window_state.record_news(
        [{"title": "Story", "source_name": "A", "url": "u1", "published_at": _iso(1)}]
    )
    monkeypatch.setattr(kokoro_tts, "speak_or_say", lambda *a, **k: None)

    r1 = client.post("/api/voice/read_news")
    assert r1.json()["ok"] is True
    # The _SyncThread stand-in ran the target (including its own
    # `finally: _speak_lock.release()`) fully synchronously inside .start(),
    # so the lock must already be free again for the very next click.
    r2 = client.post("/api/voice/read_news")
    assert r2.json()["ok"] is True
