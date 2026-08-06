"""Surge detection (brief/window/surge.py) — the z-score rules that decide
when a place's coverage is "spiking", plus its integration into
dispatch-alerts-v1 (service.build_alerts, kind "surge")."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from brief.window import service, surge

NOW = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)


def _row(place: str, minutes_ago: float, lat=10.0, lon=20.0) -> dict:
    return {
        "place": place,
        "first_seen": NOW - timedelta(minutes=minutes_ago),
        "lat": lat,
        "lon": lon,
    }


def test_steady_coverage_is_not_a_surge():
    # One story every hour for 47h, and one in the last hour: perfectly normal.
    rows = [_row("Taiwan", 60 * h + 30) for h in range(48)]
    assert surge.detect_place_surges(rows, NOW) == []


def test_burst_on_quiet_place_triggers_and_carries_newest_coords():
    # Two stories across the whole baseline, then five in the last hour.
    rows = [_row("Taiwan", 60 * 20), _row("Taiwan", 60 * 40)]
    rows += [_row("Taiwan", m, lat=23.8, lon=121.0) for m in (5, 12, 25, 40, 55)]
    got = surge.detect_place_surges(rows, NOW)
    assert len(got) == 1
    s = got[0]
    assert s["place"] == "Taiwan" and s["count"] == 5
    assert s["z"] >= 3.0
    assert (s["lat"], s["lon"]) == (23.8, 121.0)  # newest story's pin


def test_min_stories_floor_beats_any_z_score():
    # Brand-new place, 2 stories: z would be 2 anyway, but the floor is the
    # rule that matters — 2 stories is never a surge.
    rows = [_row("Reykjavik", 10), _row("Reykjavik", 20)]
    assert surge.detect_place_surges(rows, NOW) == []


def test_new_place_with_three_stories_triggers_via_sigma_floor():
    # No history: mean 0, sigma floored to 1 -> z == count == 3.
    rows = [_row("Suez", 5), _row("Suez", 15), _row("Suez", 45)]
    got = surge.detect_place_surges(rows, NOW)
    assert [s["place"] for s in got] == ["Suez"]
    assert got[0]["z"] == 3.0


def test_strongest_surge_sorts_first():
    rows = [_row("A", m) for m in (1, 2, 3)] + [
        _row("B", m) for m in (1, 2, 3, 4, 5, 6)
    ]
    got = surge.detect_place_surges(rows, NOW)
    assert [s["place"] for s in got] == ["B", "A"]


def test_build_alerts_emits_surge_kind_from_news_articles():
    # Seed the rolling store with a fresh 3-story burst on one place.
    con = service.db.connect()
    try:
        for i in range(3):
            con.execute(
                "INSERT INTO news_articles (url, title, source_name, place, lat, lon) "
                "VALUES (?, ?, 'S', 'Suez', 30.0, 32.3)",
                (f"u{i}", f"Story {i}"),
            )
        con.commit()
    finally:
        con.close()

    state = service.WindowState(sweep_interval_seconds=900)
    body = service.build_alerts(state)
    surges = [a for a in body["alerts"] if a["kind"] == "surge"]
    assert len(surges) == 1
    a = surges[0]
    assert a["place"] == "Suez" and a["count"] == 3
    assert a["speak"] == "Coverage is spiking on Suez. 3 stories in the last hour."
    assert (a["lat"], a["lon"]) == (30.0, 32.3)
    assert a["id"] and a["first_seen"]
    assert a["top_story"] is None  # no headline cache seeded in this test


def test_surge_speak_names_a_representative_headline_when_available():
    # the user: heard "N outlets reporting on Canada" with no idea what the story
    # was -- a surge alert must point at an example, not just a bare count.
    con = service.db.connect()
    try:
        for i in range(3):
            con.execute(
                "INSERT INTO news_articles (url, title, source_name, place, lat, lon) "
                "VALUES (?, ?, 'S', 'Canada', 45.4, -75.7)",
                (f"c{i}", f"Story {i}"),
            )
        con.commit()
    finally:
        con.close()

    state = service.WindowState(sweep_interval_seconds=900)
    state.record_news(
        [
            {
                "title": "Minor local item",
                "source_name": "S",
                "url": "c0",
                "place": "Canada",
                "dupe_count": 1,
            },
            {
                "title": "Trump imposes tariffs on Canadian goods",
                "source_name": "S",
                "url": "c1",
                "place": "Canada",
                "dupe_count": 7,  # most-carried -> the representative pick
            },
        ]
    )
    body = service.build_alerts(state)
    a = next(x for x in body["alerts"] if x["kind"] == "surge")
    assert a["top_story"] == "Trump imposes tariffs on Canadian goods"
    assert a["top_story_url"] == "c1"
    assert 'top story: "Trump imposes tariffs on Canadian goods"' in a["title"]
    assert "Top story: Trump imposes tariffs on Canadian goods." in a["speak"]


def test_surge_top_story_shown_on_board_but_omitted_from_speech_when_non_latin():
    # The board can render any script fine; the voice can't pronounce it.
    con = service.db.connect()
    try:
        for i in range(3):
            con.execute(
                "INSERT INTO news_articles (url, title, source_name, place, lat, lon) "
                "VALUES (?, ?, 'S', 'Iran', 32.4, 53.7)",
                (f"i{i}", f"Story {i}"),
            )
        con.commit()
    finally:
        con.close()

    state = service.WindowState(sweep_interval_seconds=900)
    state.record_news(
        [
            {
                "title": "تنش‌های جدید در خاورمیانه",
                "source_name": "S",
                "url": "i0",
                "place": "Iran",
                "dupe_count": 9,  # most-carried -> the representative pick
            }
        ]
    )
    body = service.build_alerts(state)
    a = next(x for x in body["alerts"] if x["kind"] == "surge")
    # Board still gets the real top story (any script renders fine visually)...
    assert a["top_story"] == "تنش‌های جدید در خاورمیانه"
    assert "تنش‌های جدید در خاورمیانه" in a["title"]
    # ...but the SPOKEN text never attempts it -- falls back to the count only.
    assert (
        a["speak"]
        == f"Coverage is spiking on Iran. {a['count']} stories in the last hour."
    )
