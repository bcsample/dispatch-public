"""GDELT DOC 2.0 firehose source (brief/ingest/gdelt.py) — artlist parsing,
seendate handling, fail-soft on throttle/errors, and NewsLoop integration."""

from __future__ import annotations

from brief.ingest import gdelt
from brief.window import service


class _Resp:
    def __init__(self, body, ct="application/json", ok=True, status_code=200):
        self._body = body
        self.headers = {"Content-Type": ct}
        self.ok = ok
        self.status_code = status_code

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError("http error")

    def json(self):
        return self._body


_ARTS = {
    "articles": [
        {
            "url": "https://ex.com/a",
            "title": "Sanctions imposed on entity",
            "seendate": "20260718T030000Z",
            "domain": "ex.com",
            "language": "English",
            "sourcecountry": "United States",
        },
        {"url": "", "title": "no url", "seendate": "20260718T030000Z"},  # dropped
        {"url": "https://ex.com/c", "title": "", "domain": "ex.com"},  # dropped
    ]
}


def test_fetch_parses_articles_to_items(monkeypatch):
    monkeypatch.setattr(gdelt.requests, "get", lambda *a, **k: _Resp(_ARTS))
    items = gdelt.fetch()
    assert len(items) == 1  # the two malformed ones dropped
    it = items[0]
    assert it.title == "Sanctions imposed on entity"
    assert it.url == "https://ex.com/a"
    assert it.source_name == "ex.com"
    assert it.source_type == "news"
    assert it.published_at == "2026-07-18T03:00:00+00:00"


def test_seendate_unparseable_yields_empty_published():
    assert gdelt._published("garbage") == ""
    assert gdelt._published("") == ""


def test_fetch_returns_none_on_throttle_text(monkeypatch):
    # GDELT throttling can come back as a 200 with text/plain, not JSON --
    # None (not []) so NewsLoop knows to back off, not just "zero articles".
    monkeypatch.setattr(
        gdelt.requests,
        "get",
        lambda *a, **k: _Resp("Please limit requests", ct="text/plain"),
    )
    assert gdelt.fetch() is None


def test_fetch_returns_none_on_real_429(monkeypatch):
    # Fable's full-log scan (2026-07-28) found 755 real 429s in the wild --
    # this is the other throttle signal, distinct from the 200-plaintext one.
    monkeypatch.setattr(
        gdelt.requests, "get", lambda *a, **k: _Resp(None, status_code=429)
    )
    assert gdelt.fetch() is None


def test_fetch_fail_soft_on_exception(monkeypatch):
    def boom(*a, **k):
        raise ConnectionError("gdelt down")

    monkeypatch.setattr(gdelt.requests, "get", boom)
    assert gdelt.fetch() == []


def test_news_loop_appends_gdelt_items(monkeypatch):
    from brief.models import Item

    def fake_rss(sources):
        return [
            Item(
                source_name="RSS",
                source_type="news",
                title="RSS story",
                url="http://rss/1",
                published_at="2026-07-18T00:00:00+00:00",
            )
        ]

    def fake_gdelt(query, timespan, max_records):
        return [
            Item(
                source_name="gdelt.com",
                source_type="news",
                title="GDELT story",
                url="http://gdelt/1",
                published_at="2026-07-18T01:00:00+00:00",
            )
        ]

    monkeypatch.setattr(service.rss, "fetch_all", fake_rss)
    monkeypatch.setattr(service.gdelt, "fetch", fake_gdelt)

    state = service.WindowState(sweep_interval_seconds=900)
    loop = service.NewsLoop(
        [{"name": "S", "rss": "http://example.test/feed"}],
        state,
        interval_seconds=5,
        dedup_enabled=False,
        gdelt_enabled=True,
    )
    loop.run_one_fetch()
    titles = {h["title"] for h in state.news_snapshot()}
    assert titles == {"RSS story", "GDELT story"}


def test_news_loop_skips_gdelt_when_disabled(monkeypatch):
    from brief.models import Item

    monkeypatch.setattr(
        service.rss,
        "fetch_all",
        lambda s: [
            Item(source_name="RSS", source_type="news", title="R", url="http://r/1")
        ],
    )

    def boom(*a, **k):
        raise AssertionError("GDELT must not be called when disabled")

    monkeypatch.setattr(service.gdelt, "fetch", boom)
    state = service.WindowState(sweep_interval_seconds=900)
    loop = service.NewsLoop(
        [{"name": "S", "rss": "http://example.test/feed"}],
        state,
        interval_seconds=5,
        dedup_enabled=False,
        gdelt_enabled=False,
    )
    loop.run_one_fetch()
    assert [h["title"] for h in state.news_snapshot()] == ["R"]


# --- GDELT backoff (2026-07-28, Fable's log-scan: 755 real 429s in the wild) -


def test_gdelt_rate_limit_triggers_exponential_backoff(monkeypatch):
    monkeypatch.setattr(service.rss, "fetch_all", lambda s: [])
    calls = {"n": 0}

    def rate_limited(query, timespan, max_records):
        calls["n"] += 1
        return None  # every call gets rate-limited

    monkeypatch.setattr(service.gdelt, "fetch", rate_limited)
    state = service.WindowState(sweep_interval_seconds=900)
    loop = service.NewsLoop([], state, interval_seconds=5, dedup_enabled=False, gdelt_enabled=True)

    loop.run_one_fetch()  # 1st rate-limit -> backoff = 2**1 = 2 cycles
    assert calls["n"] == 1
    assert loop._gdelt_backoff_cycles_remaining == 2

    loop.run_one_fetch()  # skipped (backoff 2 -> 1)
    loop.run_one_fetch()  # skipped (backoff 1 -> 0)
    assert calls["n"] == 1  # fetch() never called again during the skip window

    loop.run_one_fetch()  # backoff expired -> tries again, rate-limited again
    assert calls["n"] == 2
    assert loop._gdelt_consecutive_ratelimits == 2
    assert loop._gdelt_backoff_cycles_remaining == 4  # 2**2, growing


def test_gdelt_success_resets_backoff_state(monkeypatch):
    from brief.models import Item

    monkeypatch.setattr(service.rss, "fetch_all", lambda s: [])
    state = service.WindowState(sweep_interval_seconds=900)
    loop = service.NewsLoop([], state, interval_seconds=5, dedup_enabled=False, gdelt_enabled=True)

    monkeypatch.setattr(service.gdelt, "fetch", lambda query, timespan, max_records: None)
    loop.run_one_fetch()
    assert loop._gdelt_consecutive_ratelimits == 1

    monkeypatch.setattr(
        service.gdelt,
        "fetch",
        lambda query, timespan, max_records: [
            Item(source_name="g", source_type="news", title="G", url="http://g/1")
        ],
    )
    loop.run_one_fetch()  # still backing off from the prior rate-limit -> skipped
    assert loop._gdelt_consecutive_ratelimits == 1  # unchanged, this cycle was skipped

    loop._gdelt_backoff_cycles_remaining = 0  # fast-forward past the backoff window
    loop.run_one_fetch()
    assert loop._gdelt_consecutive_ratelimits == 0  # reset on success
    assert "G" in [h["title"] for h in state.news_snapshot()]


def test_gdelt_feed_health_marked_down_on_rate_limit(monkeypatch):
    monkeypatch.setattr(service.rss, "fetch_all", lambda s: [])
    monkeypatch.setattr(service.gdelt, "fetch", lambda query, timespan, max_records: None)
    state = service.WindowState(sweep_interval_seconds=900)
    loop = service.NewsLoop([], state, interval_seconds=5, dedup_enabled=False, gdelt_enabled=True)
    loop.run_one_fetch()
    health = {f["name"]: f for f in service.compute_feed_health(state)}
    assert health["GDELT"]["bucket"] == "error"
    assert "rate-limited" in (state.feed_health_snapshot()["GDELT"]["error"] or "")
