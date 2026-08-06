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
    # the user's rule (implicit): a component that WAS healthy an hour ago and
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

    monkeypatch.setattr(service.rss, "fetch_all", lambda sources: [])
    loop.run_one_fetch()
    assert service.compute_feed_health(state)[0]["name"] == "RSS"
    assert service.compute_feed_health(state)[0]["bucket"] == "fresh"

    def _raise(sources):
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
