"""Voice cadence: the board shows everything, but only speak_worthy alerts are
ever read aloud, and never more often than the minimum gap. Target is a handful
of utterances a day, not one a minute."""

from __future__ import annotations

from datetime import datetime, timezone

from brief.window import service


def _fresh_ts():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def test_only_widely_carried_news_is_speak_worthy():
    state = service.WindowState(sweep_interval_seconds=900)
    ts = _fresh_ts()
    state.record_news(
        [
            {"title": "Huge", "url": "u1", "dupe_count": 11, "first_seen": ts},
            {"title": "Middling", "url": "u2", "dupe_count": 4, "first_seen": ts},
        ]
    )
    body = service.build_alerts(state, min_sources=3, speak_min_sources=8)
    news = {a["title"]: a for a in body["alerts"] if a["kind"] == "news"}
    # Both reach the BOARD...
    assert set(news) == {"Huge", "Middling"}
    # ...but only the widely-carried one earns the voice.
    assert news["Huge"]["speak_worthy"] is True
    assert news["Middling"]["speak_worthy"] is False


def test_watchlist_always_speak_worthy():
    state = service.WindowState(sweep_interval_seconds=900)
    state.record_news(
        [
            {
                "title": "DIA award",
                "url": "w1",
                "beat": True,
                "beat_score": 9,
                "dupe_count": 1,
                "first_seen": _fresh_ts(),
            }
        ]
    )
    body = service.build_alerts(state)
    watch = [a for a in body["alerts"] if a["kind"] == "watchlist"]
    assert watch and watch[0]["speak_worthy"] is True


def test_non_latin_news_still_boards_but_never_speaks():
    # the user, 2026-07-21: "one of the sources was in Arabic or Farsi, and the
    # system really choked on that." Widely-carried but unreadable-aloud ->
    # board yes, voice no.
    state = service.WindowState(sweep_interval_seconds=900)
    ts = _fresh_ts()
    state.record_news(
        [
            {
                "title": "الشرق الأوسط يشهد توترات جديدة",
                "url": "u1",
                "dupe_count": 11,
                "first_seen": ts,
            }
        ]
    )
    body = service.build_alerts(state, min_sources=3, speak_min_sources=8)
    news = [a for a in body["alerts"] if a["kind"] == "news"]
    assert len(news) == 1  # still on the board
    assert news[0]["speak_worthy"] is False  # never spoken


def test_non_latin_watchlist_still_boards_but_never_speaks():
    state = service.WindowState(sweep_interval_seconds=900)
    state.record_news(
        [
            {
                "title": "تنش‌های جدید در خاورمیانه",
                "url": "w1",
                "beat": True,
                "beat_score": 9,
                "dupe_count": 1,
                "first_seen": _fresh_ts(),
            }
        ]
    )
    body = service.build_alerts(state)
    watch = [a for a in body["alerts"] if a["kind"] == "watchlist"]
    assert len(watch) == 1
    assert watch[0]["speak_worthy"] is False


def test_only_big_surges_are_speak_worthy():
    con = service.db.connect()
    try:
        for i in range(4):  # 4 stories -> surges on the board, below speak bar
            con.execute(
                "INSERT INTO news_articles (url, title, place, lat, lon) "
                "VALUES (?, ?, 'Suez', 30.0, 32.3)",
                (f"s{i}", f"Suez incident {i}"),
            )
        con.commit()
    finally:
        con.close()
    body = service.build_alerts(
        service.WindowState(sweep_interval_seconds=900), speak_surge_min_stories=6
    )
    surges = [a for a in body["alerts"] if a["kind"] == "surge"]
    assert surges and surges[0]["count"] == 4
    assert surges[0]["speak_worthy"] is False  # on the board, not in your ear


# --- the speaker's pacing ---------------------------------------------------


def _load_speaker():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "scripts" / "dispatch_speak.py"
    spec = importlib.util.spec_from_file_location("dispatch_speak", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_current_slot_only_matches_just_after_a_slot():
    sp = _load_speaker()
    at = lambda h, m, sec=0: datetime(2026, 7, 20, h, m, sec)  # noqa: E731
    mins, window = [0, 30], 2
    assert sp.current_slot(at(10, 0, 5), mins, window) == at(10, 0)
    assert sp.current_slot(at(10, 30, 45), mins, window) == at(10, 30)
    assert sp.current_slot(at(10, 1, 30), mins, window) == at(10, 0)  # inside window
    assert sp.current_slot(at(10, 15), mins, window) is None  # between slots
    assert sp.current_slot(at(10, 5), mins, window) is None  # window passed


def test_speaker_accumulates_between_slots_and_reads_a_bulletin(monkeypatch, tmp_path):
    sp = _load_speaker()
    spoken: list[str] = []
    monkeypatch.setattr(sp, "speak", lambda t: spoken.append(t))
    monkeypatch.setattr(sp, "MUTE_PATH", tmp_path / "mute")
    monkeypatch.setattr(sp, "JARVIS_CLAIM_PATH", tmp_path / "claim")
    monkeypatch.setattr(sp, "_schedule", lambda: ([0, 30], 2))
    monkeypatch.setattr(sp.quiet, "mic_in_use", lambda: False)
    monkeypatch.setattr(sp.quiet, "focus_active", lambda path=None: False)

    # 10:12 — a worthy alert appears between slots: captured to pending, silent.
    monkeypatch.setattr(
        sp,
        "fetch_alerts",
        lambda: [
            {"id": "a1", "speak": "First thing.", "speak_worthy": True},
            {"id": "a2", "speak": "Minor.", "speak_worthy": False},
        ],
    )
    seen, slot, pending = sp.run_once(
        [], first_run=False, now=datetime(2026, 7, 20, 10, 12)
    )
    assert spoken == []
    assert [p["id"] for p in pending] == ["a1"]  # held with its speak text
    assert seen == ["a2"]  # unworthy dropped

    # 10:20 — the worthy alert has AGED OUT of the feed, but a new one appears.
    # The aged-out one must NOT be lost.
    monkeypatch.setattr(
        sp,
        "fetch_alerts",
        lambda: [
            {"id": "a3", "speak": "Second thing.", "speak_worthy": True},
        ],
    )
    seen, slot, pending = sp.run_once(
        seen,
        first_run=False,
        now=datetime(2026, 7, 20, 10, 20),
        last_slot=slot,
        pending=pending,
    )
    assert spoken == []
    assert [p["id"] for p in pending] == ["a1", "a3"]  # both accumulated

    # 10:30 — bulletin reads BOTH, even though a1 is long gone from the feed.
    monkeypatch.setattr(sp, "fetch_alerts", lambda: [])  # nothing live now
    seen, slot, pending = sp.run_once(
        seen,
        first_run=False,
        now=datetime(2026, 7, 20, 10, 30, 20),
        last_slot=None,
        pending=pending,
    )
    assert spoken == ["Half past. 2 updates.", "First thing.", "Second thing."]
    assert pending == []
    assert slot == datetime(2026, 7, 20, 10, 30)
    assert "a1" in seen and "a3" in seen


def test_next_slot_finds_the_upcoming_one_same_hour():
    sp = _load_speaker()
    at = lambda h, m, sec=0: datetime(2026, 7, 20, h, m, sec)  # noqa: E731
    assert sp._next_slot(at(10, 12), [0, 30]) == at(10, 30)
    assert sp._next_slot(at(10, 29, 59), [0, 30]) == at(10, 30)


def test_next_slot_rolls_into_the_next_hour_once_todays_have_passed():
    sp = _load_speaker()
    at = lambda h, m, sec=0: datetime(2026, 7, 20, h, m, sec)  # noqa: E731
    assert sp._next_slot(at(10, 45), [0, 30]) == at(11, 0)
    assert sp._next_slot(at(10, 30), [0, 30]) == at(11, 0)  # right at a slot -> next one


def test_ranked_puts_the_biggest_items_first_across_kinds():
    sp = _load_speaker()
    # Deliberately out of arrival order -- a low-carried news story arrived
    # first, but a high-severity world event and a convergence alert should
    # both outrank it regardless of when they showed up.
    pending = [
        {"id": "n1", "speak": "News.", "kind": "news", "dupe_count": 9},
        {"id": "w1", "speak": "World.", "kind": "world", "severity": 8},
        {"id": "c1", "speak": "Convergence.", "kind": "convergence"},
        {"id": "s1", "speak": "Surge.", "kind": "surge", "count": 20},
    ]
    ranked = sp._ranked(pending)
    assert [p["id"] for p in ranked] == ["c1", "w1", "n1", "s1"]


def test_ranked_orders_within_a_kind_by_its_own_magnitude():
    sp = _load_speaker()
    pending = [
        {"id": "w_small", "speak": "x", "kind": "world", "severity": 6.1},
        {"id": "w_big", "speak": "x", "kind": "world", "severity": 9.0},
        {"id": "n_small", "speak": "x", "kind": "news", "dupe_count": 8},
        {"id": "n_big", "speak": "x", "kind": "news", "dupe_count": 40},
    ]
    ranked = sp._ranked(pending)
    assert [p["id"] for p in ranked] == ["w_big", "w_small", "n_big", "n_small"]


def test_ranked_keeps_arrival_order_for_unkinded_or_tied_entries():
    sp = _load_speaker()
    pending = [
        {"id": "a", "speak": "First."},
        {"id": "b", "speak": "Second."},
    ]
    assert [p["id"] for p in sp._ranked(pending)] == ["a", "b"]


def test_bulletin_reads_the_biggest_three_not_first_arrived(monkeypatch, tmp_path):
    sp = _load_speaker()
    spoken: list[str] = []
    monkeypatch.setattr(sp, "speak", lambda t: spoken.append(t))
    monkeypatch.setattr(sp, "MUTE_PATH", tmp_path / "mute")
    monkeypatch.setattr(sp, "JARVIS_CLAIM_PATH", tmp_path / "claim")
    monkeypatch.setattr(sp, "_schedule", lambda: ([0, 30], 2))
    monkeypatch.setattr(sp.quiet, "mic_in_use", lambda: False)
    monkeypatch.setattr(sp.quiet, "focus_active", lambda path=None: False)
    monkeypatch.setattr(
        sp,
        "fetch_alerts",
        lambda: [
            # Arrives first but is the least important of the four. (kinds
            # news/surge used to sit here; they are silenced from bulletins now,
            # so an UNRANKED kind plays the "least important" role instead --
            # what this test pins is the RANKING, not those two kinds.)
            {
                "id": "other1",
                "speak": "Other story.",
                "speak_worthy": True,
                "kind": "misc",
            },
            {
                "id": "conv1",
                "speak": "Convergence story.",
                "speak_worthy": True,
                "kind": "convergence",
            },
            {
                "id": "watch1",
                "speak": "Watchlist story.",
                "speak_worthy": True,
                "kind": "watchlist",
                "beat_score": 9,
            },
            {
                "id": "world1",
                "speak": "World story.",
                "speak_worthy": True,
                "kind": "world",
                "severity": 8,
            },
        ],
    )
    seen, slot, pending = sp.run_once(
        [], first_run=False, now=datetime(2026, 7, 20, 10, 30, 5)
    )
    # 4 items pending, MAX_UTTERANCES=3 -> convergence/world/watchlist read
    # (ranked by kind), the unranked "misc" (least important) collapses into
    # "plus 1 more" even though it arrived first.
    assert spoken == [
        "Half past. 4 updates.",
        "Convergence story.",
        "World story.",
        "Watchlist story.",
        "Plus 1 more on the board.",
    ]


def test_bulletin_texts_matches_the_real_speak_sequence():
    sp = _load_speaker()
    slot = datetime(2026, 7, 20, 10, 30)
    pending = [
        {"id": "a", "speak": "First."},
        {"id": "b", "speak": "Second."},
        {"id": "c", "speak": "Third."},
        {"id": "d", "speak": "Fourth."},
    ]
    assert sp._bulletin_texts(slot, pending) == [
        "Half past. 4 updates.",
        "First.",
        "Second.",
        "Third.",
        "Plus 1 more on the board.",
    ]


def test_maybe_prerender_fires_inside_the_preload_window(monkeypatch):
    sp = _load_speaker()
    monkeypatch.setattr(sp, "_schedule", lambda: ([0, 30], 2))
    monkeypatch.setattr(sp, "_voice_engine", lambda: ("kokoro", "bm_george", "en-gb", 1.0))
    calls = []
    monkeypatch.setattr(
        sp.kokoro_tts, "prerender", lambda text, **k: calls.append(text)
    )
    pending = [{"id": "a", "speak": "Big story."}]

    # 10:29:45 -- 15s out, inside the 30s preload window.
    result = sp.maybe_prerender(datetime(2026, 7, 20, 10, 29, 45), pending, None)
    assert result == datetime(2026, 7, 20, 10, 30)
    assert calls == ["Half past. 1 update.", "Big story."]


def test_maybe_prerender_does_nothing_outside_the_window_or_with_no_pending(monkeypatch):
    sp = _load_speaker()
    monkeypatch.setattr(sp, "_schedule", lambda: ([0, 30], 2))
    calls = []
    monkeypatch.setattr(sp.kokoro_tts, "prerender", lambda text, **k: calls.append(text))

    # Way before the window.
    result = sp.maybe_prerender(
        datetime(2026, 7, 20, 10, 15), [{"id": "a", "speak": "x"}], None
    )
    assert result is None
    assert calls == []

    # Inside the window but nothing pending yet.
    result = sp.maybe_prerender(datetime(2026, 7, 20, 10, 29, 45), [], None)
    assert result is None
    assert calls == []


def test_maybe_prerender_only_renders_once_per_slot(monkeypatch):
    sp = _load_speaker()
    monkeypatch.setattr(sp, "_schedule", lambda: ([0, 30], 2))
    monkeypatch.setattr(sp, "_voice_engine", lambda: ("kokoro", "bm_george", "en-gb", 1.0))
    calls = []
    monkeypatch.setattr(sp.kokoro_tts, "prerender", lambda text, **k: calls.append(text))
    pending = [{"id": "a", "speak": "Big story."}]

    slot = sp.maybe_prerender(datetime(2026, 7, 20, 10, 29, 45), pending, None)
    n_after_first = len(calls)
    slot2 = sp.maybe_prerender(datetime(2026, 7, 20, 10, 29, 55), pending, slot)
    assert slot2 == slot
    assert len(calls) == n_after_first  # no re-render for the same slot


def test_bulletin_is_skipped_not_stacked_when_in_a_meeting(monkeypatch, tmp_path):
    sp = _load_speaker()
    spoken: list[str] = []
    monkeypatch.setattr(sp, "speak", lambda t: spoken.append(t))
    monkeypatch.setattr(sp, "MUTE_PATH", tmp_path / "mute")
    monkeypatch.setattr(sp, "JARVIS_CLAIM_PATH", tmp_path / "claim")
    monkeypatch.setattr(sp, "_schedule", lambda: ([0, 30], 2))
    monkeypatch.setattr(sp.quiet, "mic_in_use", lambda: True)  # on a call
    monkeypatch.setattr(sp.quiet, "focus_active", lambda path=None: False)
    monkeypatch.setattr(
        sp, "fetch_alerts", lambda: [{"id": "x1", "speak": "Hi.", "speak_worthy": True}]
    )

    seen, slot, pending = sp.run_once(
        [], first_run=False, now=datetime(2026, 7, 20, 11, 0, 10), last_slot=None
    )
    assert spoken == []  # said nothing during the meeting
    assert "x1" in seen and pending == []  # dropped, not stacked onto next slot
    assert slot == datetime(2026, 7, 20, 11, 0)


# --- news is request-only (the user, 2026-07-31) ---------------------------------
#
# The board mixes things that concern YOU (a delivery, a calendar collision) with
# OSINT digest items -- kinds "news" and "surge". Only the former earns an
# unprompted bulletin. The matching rule landed in project-jarvis's
# dispatch_voice.plan_utterances the same day; this speaker is the OTHER half of
# that handoff (it reads the board whenever an external voice agent isn't holding the
# claim file), so fixing one side alone would have silenced news exactly while
# the external agent was running and left it talking whenever it wasn't.


def _silenced_fixture(monkeypatch, tmp_path, alerts):
    sp = _load_speaker()
    spoken: list[str] = []
    monkeypatch.setattr(sp, "speak", lambda t: spoken.append(t))
    monkeypatch.setattr(sp, "MUTE_PATH", tmp_path / "mute")
    monkeypatch.setattr(sp, "JARVIS_CLAIM_PATH", tmp_path / "claim")
    monkeypatch.setattr(sp, "_schedule", lambda: ([0, 30], 2))
    monkeypatch.setattr(sp.quiet, "mic_in_use", lambda: False)
    monkeypatch.setattr(sp.quiet, "focus_active", lambda path=None: False)
    monkeypatch.setattr(sp, "fetch_alerts", lambda: alerts)
    return sp, spoken


def test_news_and_surge_never_reach_a_bulletin(monkeypatch, tmp_path):
    sp, spoken = _silenced_fixture(
        monkeypatch,
        tmp_path,
        [
            {"id": "n1", "speak": "News story.", "speak_worthy": True, "kind": "news"},
            {"id": "s1", "speak": "Surge story.", "speak_worthy": True, "kind": "surge"},
        ],
    )
    seen, _slot, pending = sp.run_once([], first_run=False, now=datetime(2026, 7, 20, 10, 30, 5))
    assert spoken == []
    # Marked seen, NOT left pending -- news must never accumulate into a later
    # bulletin, which would just delay the interruption rather than remove it.
    assert pending == []
    assert {"n1", "s1"} <= set(seen)


def test_a_non_news_alert_still_reaches_the_bulletin(monkeypatch, tmp_path):
    sp, spoken = _silenced_fixture(
        monkeypatch,
        tmp_path,
        [{"id": "w1", "speak": "Border movement.", "speak_worthy": True, "kind": "world"}],
    )
    sp.run_once([], first_run=False, now=datetime(2026, 7, 20, 10, 30, 5))
    assert "Border movement." in spoken


def test_silencing_is_env_overridable(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DISPATCH_SILENT_KINDS", "")
    sp, spoken = _silenced_fixture(
        monkeypatch,
        tmp_path,
        [{"id": "n1", "speak": "News story.", "speak_worthy": True, "kind": "news"}],
    )
    sp.run_once([], first_run=False, now=datetime(2026, 7, 20, 10, 30, 5))
    assert "News story." in spoken
