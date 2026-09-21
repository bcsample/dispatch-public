"""Google News RSS keyword search (brief/ingest/googlenews.py) -- the free
NewsAPI.ai replacement: keyword persistence (load/add/remove), article
parsing (outlet-name split, fail-soft per keyword), and NewsLoop integration
including the NewsAPI.ai dial-back throttle."""

from __future__ import annotations

from brief.ingest import googlenews
from brief.window import service


class _Resp:
    def __init__(self, content: bytes, ok: bool = True):
        self.content = content
        self._ok = ok

    def raise_for_status(self):
        if not self._ok:
            raise RuntimeError("http error")


_FEED_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<item>
  <title>Pentagon awards new HUMINT contract - Defense News</title>
  <link>https://news.google.com/rss/articles/AAA1</link>
  <pubDate>Sat, 18 Jul 2026 23:07:33 GMT</pubDate>
  <description>A short summary.</description>
</item>
<item>
  <title>No outlet split here</title>
  <link>https://news.google.com/rss/articles/AAA2</link>
</item>
<item>
  <title></title>
  <link>https://news.google.com/rss/articles/AAA3</link>
</item>
</channel></rss>
"""


# --- keyword persistence -----------------------------------------------------


def test_load_keywords_defaults_when_file_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(googlenews, "KEYWORDS_PATH", tmp_path / "nope.json")
    assert googlenews.load_keywords() == googlenews.DEFAULT_KEYWORDS


def test_load_keywords_fails_soft_on_corrupt_json(tmp_path, monkeypatch):
    path = tmp_path / "kw.json"
    path.write_text("not json{{{", encoding="utf-8")
    monkeypatch.setattr(googlenews, "KEYWORDS_PATH", path)
    assert googlenews.load_keywords() == googlenews.DEFAULT_KEYWORDS


def test_add_keyword_persists_and_dedupes_case_insensitively(tmp_path, monkeypatch):
    monkeypatch.setattr(googlenews, "KEYWORDS_PATH", tmp_path / "kw.json")
    got = googlenews.add_keyword("Post-Quantum Crypto")
    assert got == googlenews.DEFAULT_KEYWORDS + ["Post-Quantum Crypto"]
    # Reload from disk (fresh call) proves it was actually persisted, not
    # just returned in-memory.
    assert googlenews.load_keywords() == got
    # Case-insensitive dup of an existing default -> no-op.
    got2 = googlenews.add_keyword("PENTAGON")
    assert got2 == got


def test_add_keyword_blank_is_a_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(googlenews, "KEYWORDS_PATH", tmp_path / "kw.json")
    before = googlenews.load_keywords()
    assert googlenews.add_keyword("   ") == before


def test_remove_keyword_persists(tmp_path, monkeypatch):
    monkeypatch.setattr(googlenews, "KEYWORDS_PATH", tmp_path / "kw.json")
    googlenews.save_keywords(["Pentagon", "sanctions"])
    got = googlenews.remove_keyword("pentagon")  # case-insensitive
    assert got == ["sanctions"]
    assert googlenews.load_keywords() == ["sanctions"]


# --- article parsing ---------------------------------------------------------


def test_fetch_keyword_splits_outlet_and_skips_blank_titles(monkeypatch):
    monkeypatch.setattr(googlenews.requests, "get", lambda *a, **k: _Resp(_FEED_XML))
    items = googlenews._fetch_keyword("Pentagon", count=10, timeout=5)
    assert len(items) == 2  # the blank-title entry is dropped
    first = items[0]
    assert first.title == "Pentagon awards new HUMINT contract"
    assert first.source_name == "Defense News"
    assert first.url == "https://news.google.com/rss/articles/AAA1"
    assert first.published_at.startswith("2026-07-18")
    # No " - " in the title -> falls back to the generic source name.
    assert items[1].source_name == "Google News"


def test_fetch_keyword_fails_soft_on_network_error(monkeypatch):
    def boom(*a, **k):
        raise ConnectionError("down")

    monkeypatch.setattr(googlenews.requests, "get", boom)
    assert googlenews._fetch_keyword("Pentagon", count=5, timeout=5) == []


def test_fetch_one_bad_keyword_does_not_cost_the_rest(monkeypatch):
    calls = []

    def flaky_get(url, params=None, **k):
        calls.append(params["q"])
        if params["q"] == "bad":
            raise ConnectionError("down")
        return _Resp(_FEED_XML)

    monkeypatch.setattr(googlenews.requests, "get", flaky_get)
    items = googlenews.fetch(
        keywords=["bad", "Pentagon"], count_per_keyword=10, include_feeds=False
    )
    assert calls == ["bad", "Pentagon"]  # both attempted
    assert len(items) == 2  # only Pentagon's results survive


def test_fetch_uses_persisted_keywords_when_none_given(tmp_path, monkeypatch):
    monkeypatch.setattr(googlenews, "KEYWORDS_PATH", tmp_path / "kw.json")
    googlenews.save_keywords(["one term"])
    seen = []
    monkeypatch.setattr(
        googlenews,
        "_fetch_keyword",
        lambda kw, count, timeout, *a: seen.append(kw) or [],
    )
    googlenews.fetch(include_feeds=False)
    assert seen == ["one term"]


# --- named feeds: top stories / topic sections (breaking, world) -----------


def test_load_feeds_defaults_when_file_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(googlenews, "FEEDS_PATH", tmp_path / "nope.json")
    assert googlenews.load_feeds() == googlenews.DEFAULT_FEEDS


def test_load_feeds_fails_soft_on_corrupt_json(tmp_path, monkeypatch):
    path = tmp_path / "feeds.json"
    path.write_text("not json{{{", encoding="utf-8")
    monkeypatch.setattr(googlenews, "FEEDS_PATH", path)
    assert googlenews.load_feeds() == googlenews.DEFAULT_FEEDS


def test_add_feed_persists_and_dedupes_on_type_and_value(tmp_path, monkeypatch):
    monkeypatch.setattr(googlenews, "FEEDS_PATH", tmp_path / "feeds.json")
    got = googlenews.add_feed("DC Local", "topic", "SOME_TOKEN")
    assert {"label": "DC Local", "type": "topic", "value": "SOME_TOKEN"} in got
    assert googlenews.load_feeds() == got  # actually persisted
    # Same (type, value) again -> no-op, even with a different label.
    got2 = googlenews.add_feed("Different Label", "topic", "SOME_TOKEN")
    assert got2 == got


def test_remove_feed_persists(tmp_path, monkeypatch):
    monkeypatch.setattr(googlenews, "FEEDS_PATH", tmp_path / "feeds.json")
    googlenews.save_feeds(
        [
            {"label": "Top", "type": "top"},
            {"label": "World", "type": "topic", "value": "X"},
        ]
    )
    got = googlenews.remove_feed("top")
    assert got == [{"label": "World", "type": "topic", "value": "X"}]
    assert googlenews.load_feeds() == got
    # Removing the LAST feed leaves an empty persisted file -- same fail-soft
    # rule as keywords: load_feeds() falls back to DEFAULT_FEEDS rather than
    # silently running with zero feeds forever.
    googlenews.remove_feed("topic", "X")
    assert googlenews.load_feeds() == googlenews.DEFAULT_FEEDS


def test_feed_url_resolves_top_and_topic_and_rejects_unknown():
    assert googlenews._feed_url({"type": "top"}) == googlenews.TOP_STORIES_URL
    assert googlenews._feed_url({"type": "topic", "value": "XYZ"}) == (
        "https://news.google.com/rss/topics/XYZ"
    )
    assert googlenews._feed_url({"type": "topic"}) is None  # missing value
    assert googlenews._feed_url({"type": "bogus"}) is None


def test_fetch_feed_parses_entries(monkeypatch):
    monkeypatch.setattr(googlenews.requests, "get", lambda *a, **k: _Resp(_FEED_XML))
    items = googlenews._fetch_feed({"type": "top", "label": "Top"}, count=10, timeout=5)
    assert len(items) == 2
    assert items[0].source_name == "Defense News"


def test_fetch_includes_feeds_by_default(tmp_path, monkeypatch):
    monkeypatch.setattr(googlenews, "KEYWORDS_PATH", tmp_path / "kw.json")
    monkeypatch.setattr(googlenews, "FEEDS_PATH", tmp_path / "feeds.json")
    googlenews.save_feeds([{"label": "Top", "type": "top"}])
    seen_keywords, seen_feeds = [], []
    monkeypatch.setattr(
        googlenews,
        "_fetch_keyword",
        lambda kw, count, timeout, *a: seen_keywords.append(kw) or [],
    )
    monkeypatch.setattr(
        googlenews,
        "_fetch_feed",
        lambda feed, count, timeout, *a: seen_feeds.append(feed) or [],
    )
    googlenews.fetch(keywords=["x"], count_per_keyword=5)
    assert seen_keywords == ["x"]
    assert seen_feeds == [{"label": "Top", "type": "top"}]


def test_fetch_can_exclude_feeds(monkeypatch):
    seen = []
    monkeypatch.setattr(googlenews, "_fetch_feed", lambda *a, **k: seen.append(1) or [])
    googlenews.fetch(keywords=[], count_per_keyword=5, include_feeds=False)
    assert seen == []


# --- NewsLoop integration: Google News in, NewsAPI.ai throttled -------------


def test_news_loop_appends_googlenews_items(monkeypatch):
    from brief.models import Item

    monkeypatch.setattr(service.rss, "fetch_all", lambda s, **k: [])
    monkeypatch.setattr(
        service.googlenews,
        "fetch",
        lambda count_per_keyword, **k: [
            Item(
                source_name="Defense News",
                source_type="news",
                title="G",
                url="http://g/1",
            )
        ],
    )
    state = service.WindowState(sweep_interval_seconds=900)
    loop = service.NewsLoop(
        [], state, interval_seconds=5, dedup_enabled=False, googlenews_enabled=True
    )
    loop.run_one_fetch()
    assert {h["title"] for h in state.news_snapshot()} == {"G"}


def test_newsapi_every_n_cycles_throttles_calls(monkeypatch):
    monkeypatch.setattr(service.rss, "fetch_all", lambda s, **k: [])
    calls = []
    monkeypatch.setattr(
        service.newsapi, "fetch", lambda keywords, count, **k: calls.append(1) or []
    )
    state = service.WindowState(sweep_interval_seconds=900)
    loop = service.NewsLoop(
        [],
        state,
        interval_seconds=5,
        dedup_enabled=False,
        newsapi_enabled=True,
        newsapi_every_n_cycles=3,
    )
    for _ in range(6):
        loop.run_one_fetch()
    assert len(calls) == 2  # cycles 3 and 6 only


def test_newsapi_every_n_cycles_default_is_every_cycle(monkeypatch):
    # Unset/default (1) must reproduce the old always-call-it behavior.
    monkeypatch.setattr(service.rss, "fetch_all", lambda s, **k: [])
    calls = []
    monkeypatch.setattr(
        service.newsapi, "fetch", lambda keywords, count, **k: calls.append(1) or []
    )
    state = service.WindowState(sweep_interval_seconds=900)
    loop = service.NewsLoop(
        [], state, interval_seconds=5, dedup_enabled=False, newsapi_enabled=True
    )
    for _ in range(3):
        loop.run_one_fetch()
    assert len(calls) == 3
