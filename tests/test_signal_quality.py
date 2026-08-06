"""U2 signal-quality: sports/entertainment stoplist, persisted beat scores
(so a skipped curation cycle stays filtered), and surge noise filtering."""

from __future__ import annotations

from brief.models import Item
from brief.window import service, stoplist

# --- stoplist ---------------------------------------------------------------


def test_stoplist_flags_sports_and_entertainment_not_real_news():
    assert stoplist.is_noise("England beat Spain in the World Cup final")
    assert stoplist.is_noise("Premier League hat-trick stuns rivals")
    assert stoplist.is_noise("Andrew Tate arrested again")
    assert not stoplist.is_noise("US and Iran trade strikes in the Gulf")
    assert not stoplist.is_noise("DIA awards HUMINT modernization contract")
    assert not stoplist.is_noise("")


def test_headline_dicts_carries_noise_flag():
    items = [
        Item(source_name="S", source_type="news", title="World Cup semifinal set"),
        Item(source_name="S", source_type="news", title="Sanctions hit Moscow banks"),
    ]
    out = service._headline_dicts(items)
    flags = {h["title"]: h["noise"] for h in out}
    assert flags["World Cup semifinal set"] is True
    assert flags["Sanctions hit Moscow banks"] is False


# --- persisted beat scores --------------------------------------------------


def test_beat_scores_persist_and_reload():
    con = service.db.connect()
    try:
        service.db.upsert_news(
            con, [{"url": "u1", "title": "A"}, {"url": "u2", "title": "B"}]
        )
        service.db.set_beat_scores(con, {"u1": 9, "u2": 3})
        assert service.db.news_beat_map(con) == {"u1": 9, "u2": 3}
    finally:
        con.close()


def test_news_loop_reapplies_beat_when_curation_skips(monkeypatch):
    def fake_rss(sources):
        return [
            Item(
                source_name="S",
                source_type="news",
                title="T",
                url="http://x/1",
                published_at="2026-07-18T00:00:00+00:00",
            )
        ]

    monkeypatch.setattr(service.rss, "fetch_all", fake_rss)

    state = service.WindowState(sweep_interval_seconds=900)
    loop = service.NewsLoop(
        [{"name": "S", "rss": "http://example.test/feed"}],
        state,
        interval_seconds=5,
        dedup_enabled=False,
        curation_enabled=True,
        curation_min_score=8,
    )

    # Cycle 1: curation returns a high score -> applied AND persisted.
    monkeypatch.setattr(service.curate, "curate", lambda hs, **k: {0: 9})
    loop.run_one_fetch()
    assert state.news_snapshot()[0]["beat"] is True

    # Cycle 2: curation SKIPS (returns None) -> the persisted score is re-applied,
    # so the story stays on-beat instead of going unscored.
    monkeypatch.setattr(service.curate, "curate", lambda hs, **k: None)
    loop.run_one_fetch()
    h = state.news_snapshot()[0]
    assert h["beat_score"] == 9 and h["beat"] is True


# --- surge noise filter -----------------------------------------------------


def test_surge_ignores_sports_stories():
    con = service.db.connect()
    try:
        # Three World-Cup stories on England: should NOT surge.
        for i in range(3):
            con.execute(
                "INSERT INTO news_articles (url, title, place, lat, lon) "
                "VALUES (?, ?, 'England', 52.5, -1.9)",
                (f"e{i}", f"World Cup match report {i}"),
            )
        # Three real stories on Suez: should surge.
        for i in range(3):
            con.execute(
                "INSERT INTO news_articles (url, title, place, lat, lon) "
                "VALUES (?, ?, 'Suez', 30.0, 32.3)",
                (f"s{i}", f"Suez canal incident {i}"),
            )
        con.commit()
    finally:
        con.close()

    body = service.build_alerts(service.WindowState(sweep_interval_seconds=900))
    surge_places = {a["place"] for a in body["alerts"] if a["kind"] == "surge"}
    assert "Suez" in surge_places
    assert "England" not in surge_places
