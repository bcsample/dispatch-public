"""GET /api/news/digest — the "one news brain" round-up project-jarvis's
get_news/get_defense_news read on demand, instead of running their own
separate, uncurated RSS pull. See brief/window/service.py::news_digest."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from brief.window import service
from brief.window.app import create_app


def _iso(minutes_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _state_with_headlines():
    state = service.WindowState(sweep_interval_seconds=900)
    state.record_news(
        [
            {
                "title": "Widely carried story",
                "source_name": "A",
                "url": "u1",
                "published_at": _iso(5),
                "dupe_count": 6,
                "sources": ["A", "B", "C"],
            },
            {
                "title": "Single-source story",
                "source_name": "B",
                "url": "u2",
                "published_at": _iso(10),
                "dupe_count": 1,
            },
            {
                "title": "On the user's beat",
                "source_name": "C",
                "url": "u3",
                "published_at": _iso(15),
                "dupe_count": 1,
                "beat": True,
                "beat_score": 9,
            },
            {
                "title": "Lower-scored beat item",
                "source_name": "D",
                "url": "u4",
                "published_at": _iso(20),
                "dupe_count": 1,
                "beat": True,
                "beat_score": 5,
            },
            {
                "title": "Sports noise",
                "source_name": "E",
                "url": "u5",
                "published_at": _iso(2),
                "dupe_count": 1,
                "noise": True,
            },
            {
                "title": "Too old",
                "source_name": "F",
                "url": "u6",
                "published_at": _iso(600),  # 10h ago, outside a 6h window
                "dupe_count": 9,
            },
        ]
    )
    return state


def test_world_kind_excludes_noise_and_stale_ranks_by_carriage():
    items = service.news_digest(_state_with_headlines(), kind="world", limit=10)
    titles = [i["title"] for i in items]
    assert "Sports noise" not in titles  # stoplist-flagged
    assert "Too old" not in titles  # outside the digest window
    # Most-carried (dupe_count) first.
    assert titles[0] == "Widely carried story"


def test_beat_kind_only_returns_on_beat_ranked_by_score():
    items = service.news_digest(_state_with_headlines(), kind="beat", limit=10)
    titles = [i["title"] for i in items]
    assert titles == ["On the user's beat", "Lower-scored beat item"]
    assert items[0]["beat_score"] == 9


def test_digest_excludes_non_latin_script_headlines():
    # the user, 2026-07-21: "one of the sources was in Arabic or Farsi, and the
    # system really choked on that." Everything news_digest() returns is
    # meant to be SPOKEN (read_news button, an external agent's get_news/get_defense_
    # news), unlike /api/news which also feeds the visual ticker.
    state = service.WindowState(sweep_interval_seconds=900)
    state.record_news(
        [
            {
                "title": "الشرق الأوسط يشهد توترات جديدة",
                "source_name": "A",
                "url": "u1",
                "published_at": _iso(1),
                "dupe_count": 9,
            },
            {
                "title": "تنش‌های جدید در خاورمیانه",  # on-beat, but in Farsi
                "source_name": "B",
                "url": "u2",
                "published_at": _iso(1),
                "beat": True,
                "beat_score": 9,
            },
        ]
    )
    world = service.news_digest(state, kind="world")
    beat = service.news_digest(state, kind="beat")
    assert world == []  # the non-Latin headline is the ONLY world-pool item
    assert beat == []  # ditto for the beat pool


def test_beat_kind_empty_when_nothing_on_beat():
    state = service.WindowState(sweep_interval_seconds=900)
    state.record_news(
        [
            {
                "title": "Not on beat",
                "source_name": "A",
                "url": "u1",
                "published_at": _iso(1),
            }
        ]
    )
    assert service.news_digest(state, kind="beat") == []  # honest empty, not fabricated


# --- read_news_script: the on-demand "read the news" button's spoken lines --


def test_read_news_script_leads_with_beat_then_world_no_repeats():
    lines = service.read_news_script(
        _state_with_headlines(), beat_limit=4, world_limit=8
    )
    assert lines[0] == "Here's the news, sir."
    # Beat items first, tagged; world items after, most-carried first; the
    # noise/stale items never appear at all.
    assert lines[1] == "On your beat: On the user's beat."
    assert lines[2] == "On your beat: Lower-scored beat item."
    assert lines[3] == "Widely carried story — 6 outlets."
    assert not any("Sports noise" in line or "Too old" in line for line in lines)


def test_read_news_script_caps_total_length():
    state = service.WindowState(sweep_interval_seconds=900)
    state.record_news(
        [
            {
                "title": f"Story {i}",
                "source_name": "A",
                "url": f"u{i}",
                "published_at": _iso(i),
                "dupe_count": 1,
            }
            for i in range(20)
        ]
    )
    lines = service.read_news_script(state, beat_limit=4, world_limit=8, total_cap=8)
    assert len(lines) == 1 + 8  # lead-in + at most total_cap headlines


def test_read_news_script_empty_when_nothing_to_say():
    state = service.WindowState(sweep_interval_seconds=900)
    assert (
        service.read_news_script(state) == []
    )  # no canned line with nothing behind it


def test_read_news_script_dedupes_beat_item_repeated_in_world():
    state = service.WindowState(sweep_interval_seconds=900)
    state.record_news(
        [
            {
                "title": "On-beat AND widely carried",
                "source_name": "A",
                "url": "shared",
                "published_at": _iso(1),
                "dupe_count": 9,
                "beat": True,
                "beat_score": 8,
            }
        ]
    )
    lines = service.read_news_script(state)
    # Appears once (as the beat line), not a second time via the world pool.
    assert sum("On-beat AND widely carried" in line for line in lines) == 1


def test_endpoint_shape(monkeypatch):
    from brief.window import quiet

    monkeypatch.setattr(quiet, "mic_in_use", lambda: False)
    monkeypatch.setattr(quiet, "focus_active", lambda path=None: False)
    app = create_app(world_feeds=[], window_cfg={}, news_sources=[])
    with TestClient(app) as client:
        r = client.get("/api/news/digest?kind=world&limit=3")
        assert r.status_code == 200
        body = r.json()
        assert body["kind"] == "world"
        assert isinstance(body["items"], list)
