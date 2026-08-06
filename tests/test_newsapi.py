"""NewsAPI.ai (Event Registry) firehose source (brief/ingest/newsapi.py) — key
gating, article parsing, duplicate skip, fail-soft, dotenv loader, and NewsLoop
integration."""

from __future__ import annotations

from brief import config
from brief.ingest import newsapi
from brief.window import service


class _Resp:
    def __init__(self, body, ok=True):
        self._body = body
        self.ok = ok

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError("http error")

    def json(self):
        return self._body


_ARTS = {
    "articles": {
        "results": [
            {
                "title": "DIA awards new HUMINT contract",
                "url": "https://ex.com/a",
                "dateTime": "2026-07-18T23:07:33Z",
                "source": {"title": "Defense News", "uri": "defensenews.com"},
                "isDuplicate": False,
            },
            {
                "title": "Dup story",
                "url": "https://ex.com/b",
                "dateTime": "2026-07-18T23:00:00Z",
                "source": {"title": "X"},
                "isDuplicate": True,  # skipped
            },
            {"title": "", "url": "https://ex.com/c"},  # no title -> skipped
        ]
    }
}


def test_fetch_returns_empty_without_key(monkeypatch):
    monkeypatch.delenv("NEWSAPI_KEY", raising=False)

    def no_post(*a, **k):
        raise AssertionError("must not call the API without a key")

    monkeypatch.setattr(newsapi.requests, "post", no_post)
    assert newsapi.fetch() == []


def test_fetch_parses_and_skips_duplicates(monkeypatch):
    monkeypatch.setattr(newsapi.requests, "post", lambda *a, **k: _Resp(_ARTS))
    items = newsapi.fetch(key="test-key")
    assert len(items) == 1
    it = items[0]
    assert it.title == "DIA awards new HUMINT contract"
    assert it.source_name == "Defense News"
    assert it.published_at == "2026-07-18T23:07:33Z"


def test_fetch_fail_soft_on_error_payload_and_exception(monkeypatch):
    monkeypatch.setattr(
        newsapi.requests, "post", lambda *a, **k: _Resp({"error": "quota"})
    )
    assert newsapi.fetch(key="k") == []

    def boom(*a, **k):
        raise ConnectionError("down")

    monkeypatch.setattr(newsapi.requests, "post", boom)
    assert newsapi.fetch(key="k") == []


def test_load_dotenv_sets_without_override(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text(
        "# a comment\nNEWSAPI_KEY=from-file\nOTHER='quoted val'\n", encoding="utf-8"
    )
    monkeypatch.delenv("NEWSAPI_KEY", raising=False)
    monkeypatch.setenv("OTHER", "preexisting")
    config.load_dotenv(env)
    import os

    assert os.environ["NEWSAPI_KEY"] == "from-file"
    assert os.environ["OTHER"] == "preexisting"  # not overridden


def test_news_loop_appends_newsapi_items(monkeypatch):
    from brief.models import Item

    monkeypatch.setattr(
        service.rss,
        "fetch_all",
        lambda s: [
            Item(source_name="RSS", source_type="news", title="R", url="http://r/1")
        ],
    )
    monkeypatch.setattr(
        service.newsapi,
        "fetch",
        lambda keywords, count: [
            Item(
                source_name="Defense News",
                source_type="news",
                title="Beat story",
                url="http://na/1",
            )
        ],
    )
    state = service.WindowState(sweep_interval_seconds=900)
    loop = service.NewsLoop(
        [{"name": "S", "rss": "http://example.test/feed"}],
        state,
        interval_seconds=5,
        dedup_enabled=False,
        newsapi_enabled=True,
    )
    loop.run_one_fetch()
    assert {h["title"] for h in state.news_snapshot()} == {"R", "Beat story"}
