"""Convergence detection — the deepest intel signal: 3+ independent channels
(a structured world-feed event, general news volume, an anomalous coverage
spike, and/or the user's own curated professional beat) all pointing at the
same ~1-degree patch of the map within 24h."""

from __future__ import annotations

from datetime import datetime, timezone

from brief.window import service


def _world_row(lat, lon, title="Quake"):
    return {"lat": lat, "lon": lon, "title": title}


NOW = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)


# --- compute_convergence_alerts: pure logic ---------------------------------


def test_no_convergence_below_min_kinds():
    world = [_world_row(35.7, 51.4)]  # Tehran-ish
    news = [
        {"title": "Story", "place": "Iran", "lat": 35.7, "lon": 51.4, "beat_score": 2}
    ]
    out = service.compute_convergence_alerts(world, news, [], NOW)
    assert out == []  # only 2 kinds (world + news), need 3


def test_three_kinds_at_one_cell_converges():
    world = [_world_row(35.7, 51.4)]
    news = [
        {
            "title": "Iran story",
            "place": "Iran",
            "lat": 35.7,
            "lon": 51.4,
            "beat_score": 9,
        },
    ]
    surges = [{"place": "Iran", "lat": 35.7, "lon": 51.4, "count": 7}]
    out = service.compute_convergence_alerts(
        world, news, surges, NOW, curation_min_score=8
    )
    assert len(out) == 1
    a = out[0]
    assert a["kind"] == "convergence"
    assert a["place"] == "Iran"
    assert a["kinds"] == ["news", "surge", "watchlist", "world"]
    assert a["lat"] == 35.7 and a["lon"] == 51.4
    assert a["speak_worthy"] is True


def test_news_and_watchlist_from_the_same_article_count_as_two_not_double():
    # A single on-beat article contributes BOTH "news" and "watchlist" -- two
    # distinct properties of one data point, not one point counted twice.
    # Combined with world alone, that's still only 3 kinds -- exactly at the
    # bar, not artificially inflated past it.
    world = [_world_row(35.7, 51.4)]
    news = [
        {
            "title": "On-beat story",
            "place": "Iran",
            "lat": 35.7,
            "lon": 51.4,
            "beat_score": 9,
        }
    ]
    out = service.compute_convergence_alerts(world, news, [], NOW, curation_min_score=8)
    assert len(out) == 1
    assert out[0]["kinds"] == ["news", "watchlist", "world"]


def test_below_curation_bar_does_not_count_as_watchlist():
    world = [_world_row(35.7, 51.4)]
    surges = [{"place": "Iran", "lat": 35.7, "lon": 51.4, "count": 7}]
    news = [
        {
            "title": "Low-score story",
            "place": "Iran",
            "lat": 35.7,
            "lon": 51.4,
            "beat_score": 2,
        }
    ]
    out = service.compute_convergence_alerts(
        world, news, surges, NOW, curation_min_score=8
    )
    # world + news + surge = 3 kinds still converges, but "watchlist" must
    # NOT be among them (score too low).
    assert len(out) == 1
    assert "watchlist" not in out[0]["kinds"]


def test_noise_titled_news_never_contributes_a_signal():
    world = [_world_row(51.5, -0.1)]
    surges = [{"place": "England", "lat": 51.5, "lon": -0.1, "count": 7}]
    news = [
        {
            "title": "England win the World Cup",  # stoplist sports noise
            "place": "England",
            "lat": 51.5,
            "lon": -0.1,
            "beat_score": 9,
        }
    ]
    out = service.compute_convergence_alerts(
        world, news, surges, NOW, curation_min_score=8
    )
    # Noise excludes BOTH the news and watchlist signals it would have
    # contributed -- only world + surge remain, below the 3-kind bar.
    assert out == []


def test_different_cells_never_merge():
    # World's alone in Iran (1 kind); news is alone in Canada, off-beat and
    # with no surge (1 kind) -- neither cell reaches the 3-kind bar, and they
    # must never combine across cells to fake a convergence.
    world = [_world_row(35.7, 51.4)]  # Iran
    news = [
        {
            "title": "Story",
            "place": "Canada",
            "lat": 45.4,
            "lon": -75.7,
            "beat_score": 2,
        }
    ]
    out = service.compute_convergence_alerts(world, news, [], NOW, curation_min_score=8)
    assert out == []


def test_missing_lat_lon_is_skipped_not_crashed():
    world = [{"lat": None, "lon": None, "title": "X"}]
    news = [{"title": "Y", "place": None, "lat": None, "lon": None, "beat_score": 9}]
    assert service.compute_convergence_alerts(world, news, [], NOW) == []


def test_stable_hourly_id_reannounces_at_most_once_an_hour():
    world = [_world_row(35.7, 51.4)]
    news = [
        {"title": "Story", "place": "Iran", "lat": 35.7, "lon": 51.4, "beat_score": 9}
    ]
    surges = [{"place": "Iran", "lat": 35.7, "lon": 51.4, "count": 7}]
    a1 = service.compute_convergence_alerts(
        world, news, surges, NOW, curation_min_score=8
    )[0]
    a2 = service.compute_convergence_alerts(
        world, news, surges, NOW, curation_min_score=8
    )[0]
    assert a1["id"] == a2["id"]


def test_falls_back_to_coordinates_when_no_place_string_available():
    # World-only signals never carry a "place" string; if somehow the OTHER
    # contributing kinds don't supply one either, fall back to coordinates
    # rather than leaving it blank.
    world = [_world_row(35.7, 51.4)]
    news = [
        {"title": "Story", "place": None, "lat": 35.7, "lon": 51.4, "beat_score": 9}
    ]
    surges = [{"place": None, "lat": 35.7, "lon": 51.4, "count": 7}]
    out = service.compute_convergence_alerts(
        world, news, surges, NOW, curation_min_score=8
    )
    assert len(out) == 1
    assert "°" in out[0]["place"]


# --- integration: build_alerts() actually surfaces convergence -------------


def test_build_alerts_surfaces_a_real_convergence(monkeypatch):
    # Seed a world item, an on-beat news headline, and a 3-story burst, all
    # at the same place -- via the real DB + state, exercising the full
    # build_alerts() wiring (not just the pure function above).
    con = service.db.connect()
    try:
        con.execute(
            "INSERT INTO items (content_hash, source_name, source_type, title, "
            "severity, lat, lon, first_seen) VALUES (?, 'USGS', 'world', ?, ?, ?, ?, "
            "datetime('now'))",
            ("h-conv", "New: M6.5 quake near Tehran", 6.5, 35.7, 51.4),
        )
        for i in range(3):
            con.execute(
                "INSERT INTO news_articles (url, title, source_name, place, lat, lon, "
                "beat_score, first_seen) VALUES (?, ?, 'S', 'Iran', 35.7, 51.4, 9, "
                "datetime('now'))",
                (f"conv{i}", f"Iran story {i}"),
            )
        con.commit()
    finally:
        con.close()

    state = service.WindowState(sweep_interval_seconds=900)
    state.record_news(
        [
            {
                "title": "Iran story 0",
                "source_name": "S",
                "url": "conv0",
                "place": "Iran",
                "lat": 35.7,
                "lon": 51.4,
                "beat": True,
                "beat_score": 9,
                "dupe_count": 1,
                "first_seen": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            }
        ]
    )
    body = service.build_alerts(state, min_severity=6.0, curation_min_score=8)
    convergence = [a for a in body["alerts"] if a["kind"] == "convergence"]
    assert len(convergence) == 1
    assert convergence[0]["place"] == "Iran"
    assert set(convergence[0]["kinds"]) >= {
        "world",
        "surge",
    }  # watchlist may also be in
