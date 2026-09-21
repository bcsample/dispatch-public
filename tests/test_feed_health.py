"""GET /api/feed_health -- per-source freshness chips (fresh<15m/stale 1h/
very_stale 6h/error), distinct from the full-board STALE overlay (that's a
whole-connection watchdog; this is per-individual-feed)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from brief.window import service
from brief.window.app import create_app


def _iso(seconds_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()


# --- _feed_bucket: pure bucket logic ----------------------------------------


def test_bucket_fresh_under_15_minutes():
    assert service._feed_bucket(60, ok=True, ever_succeeded=True) == "fresh"


def test_bucket_stale_between_15min_and_1h():
    assert service._feed_bucket(20 * 60, ok=True, ever_succeeded=True) == "stale"


def test_bucket_very_stale_between_1h_and_6h():
    assert service._feed_bucket(2 * 3600, ok=True, ever_succeeded=True) == "very_stale"


def test_bucket_error_past_6h():
    assert service._feed_bucket(7 * 3600, ok=True, ever_succeeded=True) == "error"


def test_bucket_error_on_explicit_failure_even_if_recent():
    # An explicit failure always reads as error, regardless of age.
    assert service._feed_bucket(5, ok=False, ever_succeeded=True) == "error"


def test_bucket_error_when_never_succeeded():
    assert service._feed_bucket(None, ok=True, ever_succeeded=False) == "error"


# --- compute_feed_health: world feeds + tracked components -----------------


def test_world_feeds_share_the_sweep_age_and_per_feed_ok():
    state = service.WindowState(sweep_interval_seconds=900)
    state.record_sweep(
        feeds=[
            {"name": "USGS Earthquakes", "ok": True},
            {"name": "OFAC SDN Sanctions", "ok": False},
        ],
        ok=False,
    )
    feeds = {f["name"]: f for f in service.compute_feed_health(state)}
    assert feeds["USGS Earthquakes"]["bucket"] == "fresh"  # just swept, ok
    assert feeds["OFAC SDN Sanctions"]["bucket"] == "error"  # just swept, but ok=False


def test_tracked_component_reports_fresh_after_success():
    state = service.WindowState(sweep_interval_seconds=900)
    state.record_feed_health("RSS", True)
    feeds = {f["name"]: f for f in service.compute_feed_health(state)}
    assert feeds["RSS"]["bucket"] == "fresh"
    assert feeds["RSS"]["age_seconds"] < 5


def test_tracked_component_reports_error_after_failure_with_no_prior_success():
    state = service.WindowState(sweep_interval_seconds=900)
    state.record_feed_health("RSS", False, "boom")
    feeds = {f["name"]: f for f in service.compute_feed_health(state)}
    assert feeds["RSS"]["bucket"] == "error"


def test_a_later_failure_does_not_erase_the_earlier_success_age():
    # the operator's rule (implicit): a component that WAS healthy an hour ago and
    # just failed reads by its last real success's age, not "just now" --
    # otherwise a failing feed would misleadingly look instantly fresh again
    # on its next (also failing) attempt.
    state = service.WindowState(sweep_interval_seconds=900)
    state.feed_health["RSS"] = {"last_ok_at": _iso(3 * 3600), "error": None}
    state.record_feed_health("RSS", False, "still down")
    feeds = {f["name"]: f for f in service.compute_feed_health(state)}
    assert feeds["RSS"]["bucket"] == "error"  # failing AND 3h stale
    assert feeds["RSS"]["age_seconds"] > 3 * 3600 - 5


# --- the endpoint ------------------------------------------------------------


def test_endpoint_shape(monkeypatch):
    from brief.window import quiet

    monkeypatch.setattr(quiet, "mic_in_use", lambda: False)
    monkeypatch.setattr(quiet, "focus_active", lambda path=None: False)
    app = create_app(world_feeds=[], window_cfg={}, news_sources=[])
    with TestClient(app) as client:
        r = client.get("/api/feed_health")
        assert r.status_code == 200
        body = r.json()
        assert isinstance(body["feeds"], list)


# --- wiring: NewsLoop / FlightLoop actually record health -------------------


def test_news_loop_records_rss_health_on_success_and_failure(monkeypatch):
    state = service.WindowState(sweep_interval_seconds=900)
    loop = service.NewsLoop(
        [{"name": "S", "rss": "http://x"}], state, interval_seconds=5
    )

    monkeypatch.setattr(service.rss, "fetch_all", lambda sources, **k: [])
    loop.run_one_fetch()
    assert service.compute_feed_health(state)[0]["name"] == "RSS"
    assert service.compute_feed_health(state)[0]["bucket"] == "fresh"

    def _raise(sources, **k):
        raise RuntimeError("feed down")

    monkeypatch.setattr(service.rss, "fetch_all", _raise)
    loop.run_one_fetch()
    assert service.compute_feed_health(state)[0]["bucket"] == "error"


def test_flight_loop_records_health_on_success_and_none(monkeypatch):
    state = service.WindowState(sweep_interval_seconds=900)
    loop = service.FlightLoop(state, lat=38.85, lon=-77.04)

    monkeypatch.setattr(service.flights, "fetch_flights", lambda *a, **k: [])
    monkeypatch.setattr(service.flightroute, "enrich", lambda aircraft: None)
    loop.run_one_fetch()
    feeds = {f["name"]: f for f in service.compute_feed_health(state)}
    assert feeds["Flights"]["bucket"] == "fresh"

    monkeypatch.setattr(service.flights, "fetch_flights", lambda *a, **k: None)
    loop.run_one_fetch()
    feeds = {f["name"]: f for f in service.compute_feed_health(state)}
    assert feeds["Flights"]["bucket"] == "error"


# --- N19: a failed fetch must record as failed, not healthy ------------------
# Wild specimen (2026-09-12 17:43 -> 09-14 09:07, data/logs/brief.log): 38 straight
#   ERROR brief.newsapi NewsAPI.ai fetch FAILED (HTTPError('403 Client Error:
#   Forbidden for url: https://eventregistry.org/api/v1/article/getArticles'))
# while /api/feed_health showed NewsAPI as "stale", never "error", because the
# loop recorded ok=True whenever fetch returned (it always returns).


def _http_403(*a, **k):
    import requests

    raise requests.HTTPError(
        "403 Client Error: Forbidden for url: "
        "https://eventregistry.org/api/v1/article/getArticles"
    )


def _loop(state, **kw):
    return service.NewsLoop([], state, interval_seconds=5, dedup_enabled=False, **kw)


def test_fetch_report_ok_rules():
    from brief.ingest.report import FetchReport

    assert FetchReport().ok  # nothing tried is not a failure
    r = FetchReport()
    r.attempt()
    r.fail("x")
    assert not r.ok and r.summary() == "1/1 failed: x"
    partial = FetchReport()
    for _ in range(3):
        partial.attempt()
    partial.fail("one of three")
    assert partial.ok and partial.summary() is None


def test_newsapi_403_records_error_not_healthy(monkeypatch):
    monkeypatch.setattr(service.newsapi.requests, "post", _http_403)
    monkeypatch.setattr(service.newsapi, "api_key", lambda: "k")
    monkeypatch.setattr(service.rss, "fetch_all", lambda s, **k: [])
    state = service.WindowState(sweep_interval_seconds=900)
    _loop(state, newsapi_enabled=True).run_one_fetch()
    feeds = {f["name"]: f for f in service.compute_feed_health(state)}
    assert feeds["NewsAPI"]["bucket"] == "error"
    assert "403" in state.feed_health_snapshot()["NewsAPI"]["error"]


def test_newsapi_error_payload_records_error(monkeypatch):
    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"error": "quota exceeded"}

    monkeypatch.setattr(service.newsapi.requests, "post", lambda *a, **k: _Resp())
    monkeypatch.setattr(service.newsapi, "api_key", lambda: "k")
    monkeypatch.setattr(service.rss, "fetch_all", lambda s, **k: [])
    state = service.WindowState(sweep_interval_seconds=900)
    _loop(state, newsapi_enabled=True).run_one_fetch()
    assert "quota" in state.feed_health_snapshot()["NewsAPI"]["error"]


def test_gdelt_non_ratelimit_failure_records_error(monkeypatch):
    def boom(*a, **k):
        raise ConnectionError("gdelt down")

    monkeypatch.setattr(service.gdelt.requests, "get", boom)
    monkeypatch.setattr(service.rss, "fetch_all", lambda s, **k: [])
    state = service.WindowState(sweep_interval_seconds=900)
    _loop(state, gdelt_enabled=True).run_one_fetch()
    feeds = {f["name"]: f for f in service.compute_feed_health(state)}
    assert feeds["GDELT"]["bucket"] == "error"


def test_google_news_all_requests_failing_records_error(monkeypatch):
    monkeypatch.setattr(service.googlenews.requests, "get", _http_403)
    monkeypatch.setattr(service.googlenews, "load_keywords", lambda: ["a", "b"])
    monkeypatch.setattr(service.googlenews, "load_feeds", lambda: [])
    monkeypatch.setattr(service.rss, "fetch_all", lambda s, **k: [])
    state = service.WindowState(sweep_interval_seconds=900)
    _loop(state, googlenews_enabled=True).run_one_fetch()
    feeds = {f["name"]: f for f in service.compute_feed_health(state)}
    assert feeds["Google News"]["bucket"] == "error"
    assert state.feed_health_snapshot()["Google News"]["error"].startswith("2/2")


def test_google_news_partial_failure_still_delivering_is_healthy(monkeypatch):
    class _Resp:
        content = b"<rss><channel><item><title>T - O</title><link>http://x/1</link></item></channel></rss>"

        def raise_for_status(self):
            pass

    calls = iter([_http_403, lambda *a, **k: _Resp()])
    monkeypatch.setattr(
        service.googlenews.requests, "get", lambda *a, **k: next(calls)(*a, **k)
    )
    monkeypatch.setattr(service.googlenews, "load_keywords", lambda: ["a", "b"])
    monkeypatch.setattr(service.googlenews, "load_feeds", lambda: [])
    monkeypatch.setattr(service.rss, "fetch_all", lambda s, **k: [])
    state = service.WindowState(sweep_interval_seconds=900)
    _loop(state, googlenews_enabled=True).run_one_fetch()
    feeds = {f["name"]: f for f in service.compute_feed_health(state)}
    assert feeds["Google News"]["bucket"] == "fresh"


def test_rss_roster_entirely_down_records_error_partial_is_healthy(monkeypatch):
    from brief.ingest import rss

    def by_name(source, max_entries=30):
        if source["name"].startswith("dead"):
            raise ConnectionError(source["name"])
        return []

    monkeypatch.setattr(rss, "fetch_source", by_name)
    all_dead = [
        {"name": "dead1", "rss": "http://d/1"},
        {"name": "dead2", "rss": "http://d/2"},
    ]
    state = service.WindowState(sweep_interval_seconds=900)
    service.NewsLoop(
        all_dead, state, interval_seconds=5, dedup_enabled=False
    ).run_one_fetch()
    feeds = {f["name"]: f for f in service.compute_feed_health(state)}
    assert feeds["RSS"]["bucket"] == "error"
    assert state.feed_health_snapshot()["RSS"]["error"].startswith("2/2 failed: dead1")

    mixed = [
        {"name": "dead1", "rss": "http://d/1"},
        {"name": "alive", "rss": "http://a/1"},
    ]
    state = service.WindowState(sweep_interval_seconds=900)
    service.NewsLoop(
        mixed, state, interval_seconds=5, dedup_enabled=False
    ).run_one_fetch()
    feeds = {f["name"]: f for f in service.compute_feed_health(state)}
    assert feeds["RSS"]["bucket"] == "fresh"
