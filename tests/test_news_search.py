"""GET /api/news/search — "read me the top news on <query>" (the user,
2026-07-28: "just a top news, reader"), LIVE on-demand Google News RSS
search, distinct from /api/news/digest's pre-curated board pool. See
brief/window/service.py::news_search."""

from __future__ import annotations

from fastapi.testclient import TestClient

from brief.models import Item
from brief.window import service
from brief.window.app import create_app


def _item(title, source_name="Some Outlet", url="http://x/1", published_at=""):
    return Item(
        source_name=source_name,
        source_type="news",
        title=title,
        url=url,
        published_at=published_at,
    )


def test_news_search_returns_items_for_the_query(monkeypatch):
    seen = {}

    def fake_fetch(keywords, count_per_keyword, include_feeds):
        seen["keywords"] = keywords
        seen["include_feeds"] = include_feeds
        return [_item("Pentagon awards new contract", url="http://x/1")]

    monkeypatch.setattr(service.googlenews, "fetch", fake_fetch)
    got = service.news_search("Pentagon")
    assert seen["keywords"] == ["Pentagon"]
    assert seen["include_feeds"] is False  # a curated feed-section pull would be wrong here
    assert got == [
        {
            "title": "Pentagon awards new contract",
            "source_name": "Some Outlet",
            "url": "http://x/1",
            "published_at": "",
        }
    ]


def test_news_search_blank_query_is_a_noop(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("must not call googlenews.fetch for a blank query")

    monkeypatch.setattr(service.googlenews, "fetch", boom)
    assert service.news_search("") == []
    assert service.news_search("   ") == []


def test_news_search_excludes_noise_and_non_latin_titles(monkeypatch):
    items = [
        _item("England beat Spain in the World Cup final"),  # sports stoplist
        _item("شركة تفوز بجائزة"),  # non-Latin -> unspeakable
        _item("Company wins defense contract"),  # keeper
    ]
    monkeypatch.setattr(
        service.googlenews, "fetch", lambda keywords, count_per_keyword, include_feeds: items
    )
    got = service.news_search("Company")
    assert [h["title"] for h in got] == ["Company wins defense contract"]


def test_news_search_respects_limit(monkeypatch):
    items = [_item(f"Story {i}", url=f"http://x/{i}") for i in range(10)]
    monkeypatch.setattr(
        service.googlenews, "fetch", lambda keywords, count_per_keyword, include_feeds: items
    )
    got = service.news_search("Company", limit=3)
    assert len(got) == 3


def test_api_news_search_endpoint(monkeypatch):
    monkeypatch.setattr(
        service.googlenews,
        "fetch",
        lambda keywords, count_per_keyword, include_feeds: [
            _item("Palantir wins new contract", source_name="Defense News", url="http://x/1")
        ],
    )
    app = create_app(
        world_feeds=[], window_cfg={"sweep_interval_seconds": 5}, news_sources=[]
    )
    with TestClient(app) as client:
        res = client.get("/api/news/search", params={"q": "Palantir"})
        assert res.status_code == 200
        body = res.json()
        assert body["query"] == "Palantir"
        assert body["items"][0]["title"] == "Palantir wins new contract"

        # No query -> empty, and no network call (would raise in the
        # monkeypatched fetch above if one were attempted with count 0 items
        # requested for blank keywords -- but news_search short-circuits first).
        empty = client.get("/api/news/search")
        assert empty.json() == {"query": "", "items": []}
