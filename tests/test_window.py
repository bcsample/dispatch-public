"""Open Window (v1) tests — the status-endpoint shape (Engine Room's dial
builds against this exact contract, plus the v1 feed-scope filter. Uses an empty world_feeds list
when exercising the live FastAPI app so the sweep loop's real sweep is a
network-free no-op, keeping these tests offline and deterministic.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from brief import config
from brief.models import Item
from brief.window import service
from brief.window.app import create_app

_ALL_FEEDS = [
    {
        "name": "USGS Earthquakes",
        "adapter": "usgs_quakes",
        "trust": "high",
        "min_severity": 4.5,
    },
    {
        "name": "NASA FIRMS Fires",
        "adapter": "nasa_firms",
        "trust": "medium",
        "min_severity": 0,
    },
    {
        "name": "OFAC SDN Sanctions",
        "adapter": "ofac_sdn",
        "trust": "high",
        "min_severity": 0,
    },
    {
        "name": "Flights — Watch Box",
        "adapter": "opensky_flights",
        "trust": "medium",
        "min_severity": 0,
    },
]


def test_select_v1_feeds_scope_without_firms_key(monkeypatch):
    monkeypatch.delenv("NASA_FIRMS_MAP_KEY", raising=False)
    selected = service.select_v1_feeds(_ALL_FEEDS)
    names = {f["name"] for f in selected}
    assert names == {"USGS Earthquakes", "OFAC SDN Sanctions"}


def test_select_v1_feeds_scope_with_firms_key(monkeypatch):
    monkeypatch.setenv("NASA_FIRMS_MAP_KEY", "test-key")
    selected = service.select_v1_feeds(_ALL_FEEDS)
    names = {f["name"] for f in selected}
    assert names == {"USGS Earthquakes", "OFAC SDN Sanctions", "NASA FIRMS Fires"}
    assert "Flights — Watch Box" not in names  # flights always stays skipped in v1


def test_build_status_matches_the_fixed_contract_shape():
    state = service.WindowState(sweep_interval_seconds=900)
    state.record_sweep(
        feeds=[
            {
                "name": "USGS Earthquakes",
                "ok": True,
                "records": 293,
                "deltas_last_sweep": 4,
            }
        ],
        ok=True,
    )
    status = service.build_status(state, 900)

    assert set(status.keys()) == {
        "service",
        "ok",
        "started_at",
        "last_sweep_at",
        "sweep_interval_seconds",
        "feeds",
        "deltas_today",
        "highest_severity_today",
        "top_event",
    }
    assert status["service"] == "dispatch"
    assert status["started_at"]  # wall tab reloads when this changes
    assert status["sweep_interval_seconds"] == 900
    assert status["last_sweep_at"] is not None
    assert status["feeds"] == [
        {"name": "USGS Earthquakes", "ok": True, "records": 293, "deltas_last_sweep": 4}
    ]
    assert isinstance(status["deltas_today"], int)


def test_status_before_first_sweep_reports_ok_true_with_null_last_sweep():
    state = service.WindowState(sweep_interval_seconds=900)
    status = service.build_status(state, 900)
    assert status["last_sweep_at"] is None
    assert status["ok"] is True  # nothing has failed yet — not the same as "down"
    assert status["feeds"] == []


# ---------------------------------------------------------------------------
# current-state retention (WindowState.current) + the news loop
# (WindowState.news / service.NewsLoop).
# ---------------------------------------------------------------------------


def test_window_state_current_snapshot_defaults_empty_and_updates():
    state = service.WindowState(sweep_interval_seconds=900)
    assert state.current_snapshot() == []
    state.record_sweep(feeds=[], ok=True, current=[{"name": "X", "records": []}])
    assert state.current_snapshot() == [{"name": "X", "records": []}]


def test_window_state_record_sweep_without_current_leaves_prior_current_untouched():
    state = service.WindowState(sweep_interval_seconds=900)
    state.record_sweep(feeds=[], ok=True, current=[{"name": "X", "records": []}])
    state.record_sweep(feeds=[], ok=True)  # current omitted, mirrors `stats` pattern
    assert state.current_snapshot() == [{"name": "X", "records": []}]


def test_window_state_news_snapshot_defaults_empty_and_updates():
    state = service.WindowState(sweep_interval_seconds=900)
    assert state.news_snapshot() == []
    state.record_news(
        [{"title": "T", "source_name": "S", "url": "u", "published_at": ""}]
    )
    assert state.news_snapshot()[0]["title"] == "T"


def test_news_loop_run_one_fetch_populates_state_newest_first(monkeypatch):
    def fake_fetch_all(sources):
        return [
            Item(
                source_name="S",
                source_type="news",
                title="Older",
                url="http://x/1",
                published_at="2026-07-15T00:00:00+00:00",
            ),
            Item(
                source_name="S",
                source_type="news",
                title="Newer",
                url="http://x/2",
                published_at="2026-07-16T00:00:00+00:00",
            ),
        ]

    monkeypatch.setattr(service.rss, "fetch_all", fake_fetch_all)

    state = service.WindowState(sweep_interval_seconds=900)
    loop = service.NewsLoop(
        [{"name": "S", "rss": "http://example.test/feed"}], state, interval_seconds=5
    )
    loop.run_one_fetch()

    news = state.news_snapshot()
    assert [n["title"] for n in news] == ["Newer", "Older"]
    assert news[0]["source_name"] == "S" and news[0]["url"] == "http://x/2"


def test_news_articles_upsert_preserves_first_seen_and_prune_trims(
    tmp_path, monkeypatch
):
    # Rolling news window: INSERT OR IGNORE keeps a story's original first_seen
    # (and original row) when re-seen, and prune_news trims past the retention.
    monkeypatch.setattr(service.db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(service.db, "DB_PATH", tmp_path / "brief.db")
    con = service.db.connect()
    try:
        service.db.upsert_news(
            con, [{"url": "u1", "title": "A", "source_name": "S", "place": "Paris"}]
        )
        first = service.db.news_first_seen_map(con)["u1"]
        # Re-seeing the same url must not reset first_seen or overwrite the row.
        service.db.upsert_news(con, [{"url": "u1", "title": "A-changed"}])
        assert service.db.news_first_seen_map(con)["u1"] == first
        row = con.execute("SELECT title FROM news_articles WHERE url = 'u1'").fetchone()
        assert row["title"] == "A"
        # A row aged past retention gets pruned; a url-less article is skipped.
        service.db.upsert_news(con, [{"title": "no-url"}])
        con.execute(
            "UPDATE news_articles SET first_seen = datetime('now', '-72 hours')"
        )
        con.commit()
        assert service.db.prune_news(con, keep_hours=48) == 1
        assert service.db.news_first_seen_map(con) == {}
    finally:
        con.close()


def test_news_loop_stamps_first_seen_from_store(tmp_path, monkeypatch):
    monkeypatch.setattr(service.db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(service.db, "DB_PATH", tmp_path / "brief.db")

    def fake_fetch_all(sources):
        return [
            Item(
                source_name="S",
                source_type="news",
                title="T",
                url="http://x/1",
                published_at="2026-07-16T00:00:00+00:00",
            )
        ]

    monkeypatch.setattr(service.rss, "fetch_all", fake_fetch_all)
    state = service.WindowState(sweep_interval_seconds=900)
    loop = service.NewsLoop(
        [{"name": "S", "rss": "http://example.test/feed"}],
        state,
        interval_seconds=5,
        dedup_enabled=False,
    )
    loop.run_one_fetch()
    assert state.news_snapshot()[0]["first_seen"]


# ---------------------------------------------------------------------------
# dispatch-alerts-v1 — the live breaking feed for voice consumers.
# Fixed contract, same discipline as /api/status: version-bump on breaking
# change, never silently reshape.
# ---------------------------------------------------------------------------


def _seed_world_item(title, severity, first_seen_sql="datetime('now')"):
    con = service.db.connect()
    try:
        con.execute(
            "INSERT INTO items (content_hash, source_name, source_type, title, "
            f"severity, first_seen) VALUES (?, 'USGS', 'world', ?, ?, {first_seen_sql})",
            (f"h-{title}", title, severity),
        )
        con.commit()
    finally:
        con.close()


def test_build_alerts_world_severity_and_recency_rules():
    _seed_world_item("New: M6.2 quake near Tokyo", 6.2)
    _seed_world_item("New: M4.9 quake", 4.9)  # below floor -> excluded
    _seed_world_item(
        "New: M7.0 quake last week", 7.0, "datetime('now', '-3 days')"
    )  # stale -> excluded

    state = service.WindowState(sweep_interval_seconds=900)
    body = service.build_alerts(state)

    assert body["contract"] == "dispatch-alerts-v1"
    assert [a["kind"] for a in body["alerts"]] == ["world"]
    alert = body["alerts"][0]
    assert alert["title"] == "M6.2 quake near Tokyo"  # kind prefix stripped
    # spoken form expands abbreviations so it reads naturally aloud
    assert alert["speak"] == "Heads up. magnitude 6.2 quake near Tokyo."
    assert alert["id"] and alert["first_seen"]


def test_world_alert_non_latin_title_still_boards_but_never_speaks():
    # the user, 2026-07-21: "one of the sources was in Arabic or Farsi, and the
    # system really choked on that" -- world alerts are otherwise ALWAYS
    # speak_worthy (they're already floored at severity), except this.
    _seed_world_item("New: الشرق الأوسط يشهد توترات جديدة", 7.0)
    state = service.WindowState(sweep_interval_seconds=900)
    body = service.build_alerts(state)
    world = [a for a in body["alerts"] if a["kind"] == "world"]
    assert len(world) == 1  # still on the board
    assert world[0]["speak_worthy"] is False  # never spoken


def test_build_alerts_news_needs_multi_source_and_freshness_and_since():
    state = service.WindowState(sweep_interval_seconds=900)
    now = datetime.now(timezone.utc)
    fresh_ts = now.strftime("%Y-%m-%d %H:%M:%S")
    stale_ts = (now - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
    state.record_news(
        [
            {
                "title": "Big story",
                "url": "u1",
                "sources": ["A", "B", "C", "D"],
                "dupe_count": 4,
                "first_seen": fresh_ts,
            },
            {
                "title": "Solo story",
                "url": "u2",
                "dupe_count": 1,
                "first_seen": fresh_ts,
            },
            {
                "title": "Old cluster",
                "url": "u3",
                "dupe_count": 5,
                "first_seen": stale_ts,
            },
        ]
    )

    body = service.build_alerts(state)
    assert [a["title"] for a in body["alerts"]] == ["Big story"]
    assert body["alerts"][0]["speak"] == "4 outlets are reporting. Big story."

    # since AFTER the alert's first_seen -> nothing new for the consumer.
    later = (now + timedelta(minutes=1)).isoformat()
    assert service.build_alerts(state, since=later)["alerts"] == []


def test_api_alerts_endpoint_returns_contract_shape():
    # Pre-create the (per-test tmp) DB schema so the lifespan's sweep thread
    # and this request thread never race the very first executescript.
    service.db.connect().close()
    app = create_app(
        world_feeds=[],
        window_cfg={"sweep_interval_seconds": 5, "news_interval_seconds": 5},
        news_sources=[],
    )
    with TestClient(app) as client:
        res = client.get("/api/alerts")
        assert res.status_code == 200
        body = res.json()
        assert body["contract"] == "dispatch-alerts-v1"
        assert body["generated_at"]
        assert body["alerts"] == []


def test_news_loop_clusters_dupes_when_dedup_enabled(monkeypatch):
    # v4.1 — NewsLoop.run_one_fetch runs dedup.cluster() on the headline
    # dicts before caching them, gated on dedup_enabled (WORLD_DELTA_BUILD_
    # PLAN.md's "v4" section). Stub both rss.fetch_all and dedup.cluster so
    # this test never touches the network or Ollama.
    def fake_fetch_all(sources):
        return [
            Item(
                source_name="S",
                source_type="news",
                title="Same story",
                url="http://x/1",
                published_at="2026-07-16T00:00:00+00:00",
            )
        ]

    monkeypatch.setattr(service.rss, "fetch_all", fake_fetch_all)

    calls = []

    def fake_cluster(headlines, threshold):
        calls.append((headlines, threshold))
        return [{"title": "clustered", "sources": ["S"], "dupe_count": 1}]

    monkeypatch.setattr(service.dedup, "cluster", fake_cluster)

    state = service.WindowState(sweep_interval_seconds=900)
    loop = service.NewsLoop(
        [{"name": "S", "rss": "http://example.test/feed"}],
        state,
        interval_seconds=5,
        dedup_enabled=True,
        dedup_similarity=0.8,
    )
    loop.run_one_fetch()

    assert len(calls) == 1
    assert calls[0][1] == 0.8
    assert state.news_snapshot() == [
        {"title": "clustered", "sources": ["S"], "dupe_count": 1}
    ]


def test_news_loop_skips_clustering_when_dedup_disabled(monkeypatch):
    def fake_fetch_all(sources):
        return [
            Item(
                source_name="S",
                source_type="news",
                title="Headline",
                url="http://x/1",
                published_at="2026-07-16T00:00:00+00:00",
            )
        ]

    monkeypatch.setattr(service.rss, "fetch_all", fake_fetch_all)

    def boom(headlines, threshold):
        raise AssertionError("cluster() should not be called when dedup_enabled=False")

    monkeypatch.setattr(service.dedup, "cluster", boom)

    state = service.WindowState(sweep_interval_seconds=900)
    loop = service.NewsLoop(
        [{"name": "S", "rss": "http://example.test/feed"}],
        state,
        interval_seconds=5,
        dedup_enabled=False,
    )
    loop.run_one_fetch()

    news = state.news_snapshot()
    assert len(news) == 1
    assert news[0]["title"] == "Headline"
    assert "dupe_count" not in news[0]  # untouched by cluster()


def test_news_loop_fail_soft_when_ollama_down_headlines_still_populate(monkeypatch):
    # v4.1 fail-soft proof: dedup.embed() (and therefore cluster()) hitting a
    # real connection error still leaves the news loop populating headlines,
    # unclustered singletons — the window works with Ollama down.
    def fake_fetch_all(sources):
        return [
            Item(
                source_name="S",
                source_type="news",
                title="Headline while Ollama is down",
                url="http://x/1",
                published_at="2026-07-16T00:00:00+00:00",
            )
        ]

    monkeypatch.setattr(service.rss, "fetch_all", fake_fetch_all)

    def boom_post(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr(service.dedup.requests, "post", boom_post)

    state = service.WindowState(sweep_interval_seconds=900)
    loop = service.NewsLoop(
        [{"name": "S", "rss": "http://example.test/feed"}],
        state,
        interval_seconds=5,
        dedup_enabled=True,
    )
    loop.run_one_fetch()

    news = state.news_snapshot()
    assert len(news) == 1
    assert news[0]["title"] == "Headline while Ollama is down"
    assert news[0]["dupe_count"] == 1
    assert news[0]["sources"] == ["S"]


def test_news_loop_fetch_failure_keeps_previous_headlines(monkeypatch):
    state = service.WindowState(sweep_interval_seconds=900)
    state.record_news(
        [{"title": "Old", "source_name": "S", "url": "u", "published_at": ""}]
    )

    def boom(sources):
        raise RuntimeError("rss boom")

    monkeypatch.setattr(service.rss, "fetch_all", boom)

    loop = service.NewsLoop(
        [{"name": "S", "rss": "http://example.test/feed"}], state, interval_seconds=5
    )
    loop.run_one_fetch()
    assert state.news_snapshot() == [
        {"title": "Old", "source_name": "S", "url": "u", "published_at": ""}
    ]


def test_live_app_current_and_news_endpoints_empty_when_nothing_configured():
    # Empty world_feeds + empty news_sources -> both loops are network-free
    # no-ops, keeping this test offline (same pattern as the existing v1 test).
    app = create_app(
        world_feeds=[],
        window_cfg={"sweep_interval_seconds": 5, "news_interval_seconds": 5},
        news_sources=[],
    )
    with TestClient(app) as client:
        current = client.get("/api/current")
        assert current.status_code == 200
        assert current.json() == []

        news = client.get("/api/news")
        assert news.status_code == 200
        assert news.json() == []

        page = client.get("/")
        assert page.status_code == 200
        assert "Headlines" in page.text
        assert "Common Operating Picture" in page.text


# ---------------------------------------------------------------------------
# the news firehose roster loader (config/news_firehose.yaml, falling
# back to sources.yaml) and the /api/news last-N-hours filter.
# ---------------------------------------------------------------------------


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def test_load_news_firehose_returns_the_firehose_file_when_present(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    (tmp_path / "news_firehose.yaml").write_text(
        "sources:\n  - name: X\n    type: news\n    trust: high\n    rss: http://example.test/feed\n",
        encoding="utf-8",
    )
    assert config.load_news_firehose() == [
        {
            "name": "X",
            "type": "news",
            "trust": "high",
            "rss": "http://example.test/feed",
        }
    ]


def test_load_news_firehose_falls_back_to_sources_yaml_when_firehose_file_absent(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    (tmp_path / "sources.yaml").write_text(
        "sources:\n  - name: Y\n    type: news\n    trust: medium\n    rss: http://example.test/y\n",
        encoding="utf-8",
    )
    # no news_firehose.yaml written -> falls back to load_sources()
    assert config.load_news_firehose() == [
        {"name": "Y", "type": "news", "trust": "medium", "rss": "http://example.test/y"}
    ]


def test_recent_news_filters_to_window_hours_and_excludes_undated():
    state = service.WindowState(sweep_interval_seconds=900)
    now = datetime.now(timezone.utc)
    state.record_news(
        [
            {
                "title": "Fresh",
                "source_name": "S",
                "url": "u1",
                "published_at": _iso(now - timedelta(minutes=30)),
            },
            {
                "title": "Stale",
                "source_name": "S",
                "url": "u2",
                "published_at": _iso(now - timedelta(hours=5)),
            },
            {"title": "Undated", "source_name": "S", "url": "u3", "published_at": ""},
        ]
    )
    out = service.recent_news(state, limit=100, window_hours=2)
    assert [n["title"] for n in out] == ["Fresh"]


def test_recent_news_without_window_hours_returns_everything_cached():
    state = service.WindowState(sweep_interval_seconds=900)
    state.record_news(
        [{"title": "A", "source_name": "S", "url": "u", "published_at": ""}]
    )
    assert service.recent_news(state, limit=100, window_hours=None) == [
        {"title": "A", "source_name": "S", "url": "u", "published_at": ""}
    ]


def test_live_app_news_endpoint_applies_configured_window_hours():
    # Deliberately NOT `with TestClient(app) as client:` -- that triggers the
    # real lifespan, which starts a background NewsLoop thread that fires an
    # immediate fetch (even with news_sources=[], it still calls
    # state.record_news([]) right after). That raced with this test's own
    # manual record_news() call below -- whichever won last decided the
    # result, an intermittent "database is locked" / empty-list flake. This
    # test only exercises /api/news's window-hours filter, which needs no
    # live loop at all.
    app = create_app(
        world_feeds=[],
        window_cfg={"sweep_interval_seconds": 5, "news_window_hours": 1},
        news_sources=[],
    )
    client = TestClient(app)
    now = datetime.now(timezone.utc)
    app.state.window_state.record_news(
        [
            {
                "title": "Fresh",
                "source_name": "S",
                "url": "u1",
                "published_at": _iso(now - timedelta(minutes=10)),
            },
            {
                "title": "Stale",
                "source_name": "S",
                "url": "u2",
                "published_at": _iso(now - timedelta(hours=3)),
            },
        ]
    )
    news = client.get("/api/news")
    assert news.status_code == 200
    assert [n["title"] for n in news.json()] == ["Fresh"]


# ---------------------------------------------------------------------------
# v3 — the /static mount (bundled Natural Earth land polygons) and the
# repositioned Headlines hero panel in the dashboard HTML.
# ---------------------------------------------------------------------------


def test_static_mount_serves_the_bundled_land_geojson():
    app = create_app(
        world_feeds=[], window_cfg={"sweep_interval_seconds": 5}, news_sources=[]
    )
    with TestClient(app) as client:
        res = client.get("/static/ne_110m_land.json")
        assert res.status_code == 200
        body = res.json()
        assert body["type"] == "FeatureCollection"
        assert len(body["features"]) > 0


def test_static_js_urls_are_cache_busted_and_still_load(tmp_path, monkeypatch):
    # the user's wall kept a pre-deploy JS file alive after a real edit (no
    # Cache-Control from StaticFiles -> browser heuristic caching). Stamping
    # each <script> URL with that file's mtime (?v=...) means an edit is a
    # genuinely new URL, so there's nothing stale to serve.
    from brief.window import app as app_module

    app = create_app(
        world_feeds=[], window_cfg={"sweep_interval_seconds": 5}, news_sources=[]
    )
    with TestClient(app) as client:
        html = client.get("/").text
        matches = re.findall(r'src="(/static/js/[\w.-]+\.js)\?v=(\d+)"', html)
        assert len(matches) >= 10  # all 11 numbered modules, cache-busted
        # The served file must actually be reachable at that busted URL (the
        # StaticFiles mount ignores the query string, same file underneath).
        path, version = matches[0]
        res = client.get(f"{path}?v={version}")
        assert res.status_code == 200

    # A file with no real mtime (missing) falls back to the un-busted tag
    # rather than raising — this is decorative, must never break page load.
    html2 = app_module._cache_bust_js('<script src="/static/js/does-not-exist.js"></script>')
    assert html2 == '<script src="/static/js/does-not-exist.js"></script>'


# ---------------------------------------------------------------------------
# Google News RSS keyword management (free NewsAPI.ai replacement) -- the
# API the standalone /keywords page drives, plus the page route itself.
# ---------------------------------------------------------------------------


def test_google_news_keywords_api_list_add_remove(tmp_path, monkeypatch):
    from brief.ingest import googlenews

    monkeypatch.setattr(googlenews, "KEYWORDS_PATH", tmp_path / "kw.json")
    app = create_app(
        world_feeds=[], window_cfg={"sweep_interval_seconds": 5}, news_sources=[]
    )
    with TestClient(app) as client:
        listed = client.get("/api/google_news/keywords").json()
        assert listed["keywords"] == googlenews.DEFAULT_KEYWORDS

        added = client.post(
            "/api/google_news/keywords/add", params={"keyword": "PQC"}
        ).json()
        assert "PQC" in added["keywords"]

        removed = client.post(
            "/api/google_news/keywords/remove", params={"keyword": "PQC"}
        ).json()
        assert "PQC" not in removed["keywords"]


def test_google_news_feeds_and_topics_api(tmp_path, monkeypatch):
    from brief.ingest import googlenews

    monkeypatch.setattr(googlenews, "FEEDS_PATH", tmp_path / "feeds.json")
    app = create_app(
        world_feeds=[], window_cfg={"sweep_interval_seconds": 5}, news_sources=[]
    )
    with TestClient(app) as client:
        topics = client.get("/api/google_news/topics").json()
        assert topics["topics"] == googlenews.TOPIC_TOKENS

        listed = client.get("/api/google_news/feeds").json()
        assert listed["feeds"] == googlenews.DEFAULT_FEEDS

        added = client.post(
            "/api/google_news/feeds/add",
            params={"label": "Business", "feed_type": "topic", "value": "TOK123"},
        ).json()
        assert {"label": "Business", "type": "topic", "value": "TOK123"} in added["feeds"]

        removed = client.post(
            "/api/google_news/feeds/remove",
            params={"feed_type": "topic", "value": "TOK123"},
        ).json()
        assert {"label": "Business", "type": "topic", "value": "TOK123"} not in removed["feeds"]


def test_keywords_page_serves_html():
    app = create_app(
        world_feeds=[], window_cfg={"sweep_interval_seconds": 5}, news_sources=[]
    )
    with TestClient(app) as client:
        res = client.get("/keywords")
        assert res.status_code == 200
        assert "text/html" in res.headers["content-type"]
        assert "BEAT KEYWORDS" in res.text
        assert "/api/google_news/keywords" in res.text


def test_dashboard_links_to_keywords_page():
    app = create_app(
        world_feeds=[], window_cfg={"sweep_interval_seconds": 5}, news_sources=[]
    )
    with TestClient(app) as client:
        html = client.get("/").text
        assert 'href="/keywords"' in html


def test_dashboard_html_fetches_land_once_and_leads_with_headlines():
    app = create_app(
        world_feeds=[], window_cfg={"sweep_interval_seconds": 5}, news_sources=[]
    )
    with TestClient(app) as client:
        page = client.get("/")
        assert page.status_code == 200
        html = page.text
        # Dashboard JS lives in separate static files (loaded in order via
        # <script src> tags) rather than one inline block; loadLand() is
        # defined in 05-map-news.js. Each tag is cache-busted with that file's
        # own mtime (?v=...) so a browser can't keep serving a pre-deploy
        # script after an edit — assert the path, not the exact query value.
        assert re.search(r'<script src="/static/js/05-map-news\.js\?v=\d+"></script>', html)
        map_news_js = client.get("/static/js/05-map-news.js").text
        # Map GeoJSON is fetched by loadLand(), called exactly once, outside
        # the 10s poll() loop — not re-fetched on every poll. (loadLand now
        # prefers country boundaries and falls back to plain land.)
        assert "/static/ne_110m_countries.json" in map_news_js
        assert map_news_js.count("loadLand();") == 1
        assert "loadLand()" in map_news_js
        # Decluttered one-screen grid: the map (Common Operating Picture) and
        # the Headlines panel are the anchors; the World Events / Current State
        # panels were removed to give map/headlines/Skies room.
        assert "Common Operating Picture" in html
        assert "gm-news" in html and "Headlines" in html
        # The removed panels' elements are gone from the UI (the words still
        # appear in JS comments, so check the actual containers).
        assert 'id="deltaList"' not in html and 'id="currentLists"' not in html


def test_live_app_health_and_status_and_dashboard_endpoints():
    # Empty world_feeds -> select_v1_feeds returns [] -> the sweep loop's
    # real sweep is a no-op (no network calls), keeping this test offline.
    app = create_app(world_feeds=[], window_cfg={"sweep_interval_seconds": 5})

    with TestClient(app) as client:
        health = client.get("/api/health")
        health_body = health.json()
        assert "ok" in health_body
        # house pattern: the status code must mirror ok, whichever way it lands
        assert health.status_code == (200 if health_body["ok"] else 503)

        status = client.get("/api/status")
        assert status.status_code == 200
        status_body = status.json()
        assert status_body["service"] == "dispatch"
        assert status_body["sweep_interval_seconds"] == 5
        assert set(status_body.keys()) == {
            "service",
            "ok",
            "started_at",
            "last_sweep_at",
            "sweep_interval_seconds",
            "feeds",
            "deltas_today",
            "highest_severity_today",
            "top_event",
        }

        deltas = client.get("/api/deltas")
        assert deltas.status_code == 200
        assert isinstance(deltas.json(), list)

        page = client.get("/")
        assert page.status_code == 200
        # Renamed "dispatch — Open Window" -> "The Dispatch" in the
        # news-first redesign (ui-the-dispatch); same invariant (the page
        # renders and carries its own branding), new label.
        assert "The Dispatch" in page.text


def test_geo_locate_matches_places_prefers_specific_and_none_when_absent():
    from brief.window import geo

    r = geo.locate("Beijing denounces US chip curbs to global supply chains")
    assert r is not None and "Beijing" in r["place"]
    assert abs(r["lat"] - 39.9) < 1.5 and abs(r["lon"] - 116.4) < 1.5
    # longest-name-first: "south china sea" wins over the substring "china"
    scs = geo.locate("South China Sea tensions escalate this week")
    assert scs is not None and scs["place"].lower().startswith("south china sea")
    # a headline with no gazetteer place gets no pin
    assert geo.locate("Quarterly earnings beat expectations, shares jump") is None


# ---------------------------------------------------------------------------
# Hardening (Fable review 2026-07-18): C2 content-hash, C1 thread health.
# ---------------------------------------------------------------------------


def test_world_item_content_hash_differentiates_shared_url():
    # C2: every OFAC row shares the same search URL; keying on url alone
    # collapsed all sanctions deltas onto ONE items row. World items must key
    # on source+title so distinct entities/CVEs/quakes stay distinct.
    a = Item(
        source_name="OFAC",
        source_type="world",
        title="New: Entity A",
        url="https://ofac.example/search",
    )
    b = Item(
        source_name="OFAC",
        source_type="world",
        title="New: Entity B",
        url="https://ofac.example/search",
    )
    assert a.content_hash != b.content_hash


def test_news_item_content_hash_still_keys_on_url():
    # News dedup is unchanged: same url from two outlets stays one item.
    a = Item(source_name="BBC", source_type="news", title="X", url="http://n/1")
    b = Item(source_name="CNN", source_type="news", title="Y", url="http://n/1")
    assert a.content_hash == b.content_hash


class _FakeLoop:
    def __init__(self, alive, enabled=True):
        self._alive = alive
        self.enabled = enabled

    def is_alive(self):
        return self._alive


def test_check_health_fails_and_reports_when_news_thread_dead():
    ok, detail = service.check_health(
        _FakeLoop(True), _FakeLoop(False), _FakeLoop(True)
    )
    assert ok is False  # dead news thread -> not ok (no green light on a dead board)
    assert detail["threads"] == {"sweep": True, "news": False, "flights": True}


def test_build_status_ok_false_when_a_core_thread_is_dead():
    state = service.WindowState(sweep_interval_seconds=900)
    state.record_sweep(feeds=[], ok=True)
    dead = {"sweep": _FakeLoop(True), "news": _FakeLoop(False)}
    assert service.build_status(state, 900, loops=dead)["ok"] is False
    alive = {"sweep": _FakeLoop(True), "news": _FakeLoop(True)}
    assert service.build_status(state, 900, loops=alive)["ok"] is True
