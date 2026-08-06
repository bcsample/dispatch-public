"""v4.1 tests — near-duplicate headline clustering (brief/window/dedup.py,, "V4.1 — Embeddings dedup/cluster").

Uses a stubbed embedder (monkeypatched `dedup.embed`) so these tests are
offline/deterministic and never call the real Ollama endpoint. Fixed vectors
are constructed so cosine similarity within a "story" is 1.0 and across
stories is 0.0, keeping the near-dupe/distinct assertions unambiguous
regardless of the exact threshold.
"""

from __future__ import annotations

from brief.window import dedup


def _headline(title, source_name, published_at, **extra):
    d = {
        "title": title,
        "source_name": source_name,
        "url": f"http://example.test/{source_name}",
        "published_at": published_at,
    }
    d.update(extra)
    return d


def test_cluster_merges_near_dupes_aggregates_sources_keeps_newest_as_representative(
    monkeypatch,
):
    headlines = [
        _headline(
            "Kyiv hit by drone strike",
            "Source A",
            "2026-07-16T09:00:00+00:00",
            lat=1.0,
            lon=2.0,
            place="Kyiv",
        ),
        _headline(
            "Massive drone attack on Kyiv overnight",
            "Source B",
            "2026-07-16T10:00:00+00:00",  # newest of the cluster
            lat=1.0,
            lon=2.0,
            place="Kyiv",
        ),
        _headline(
            "Russian drones strike Kyiv again",
            "Source C",
            "2026-07-16T08:00:00+00:00",
        ),
    ]
    # First three vectors are near-identical (same "story"); orthogonal to
    # nothing else needed here since there's only one story.
    vectors = [
        [1.0, 0.01, 0.0],
        [0.99, 0.02, 0.0],
        [0.98, 0.0, 0.01],
    ]
    monkeypatch.setattr(dedup, "embed", lambda texts: vectors)

    out = dedup.cluster(headlines, threshold=0.8)

    assert len(out) == 1
    row = out[0]
    assert row["dupe_count"] == 3
    assert row["title"] == "Massive drone attack on Kyiv overnight"  # newest wins
    assert row["source_name"] == "Source B"
    assert row["lat"] == 1.0 and row["lon"] == 2.0 and row["place"] == "Kyiv"
    # representative first, then the others in cluster order, unique
    assert row["sources"] == ["Source B", "Source A", "Source C"]


def test_cluster_keeps_distinct_stories_separate(monkeypatch):
    headlines = [
        _headline(
            "Kyiv drone strike overnight", "Source A", "2026-07-16T09:00:00+00:00"
        ),
        _headline("Apple unveils new iPhone", "Source B", "2026-07-16T09:30:00+00:00"),
        _headline(
            "Fed holds interest rates steady", "Source C", "2026-07-16T08:30:00+00:00"
        ),
    ]
    # Orthogonal-ish vectors -> low pairwise cosine similarity, well under
    # any reasonable threshold.
    vectors = [
        [1.0, 0.0, 0.0],
        [0.0, 1.0, 0.0],
        [0.0, 0.0, 1.0],
    ]
    monkeypatch.setattr(dedup, "embed", lambda texts: vectors)

    out = dedup.cluster(headlines, threshold=0.8)

    assert len(out) == 3
    for row in out:
        assert row["dupe_count"] == 1
        assert row["sources"] == [row["source_name"]]
    # newest-first order preserved
    assert [r["source_name"] for r in out] == ["Source B", "Source A", "Source C"]


def test_cluster_falls_back_to_passthrough_singletons_when_embed_returns_none(
    monkeypatch,
):
    headlines = [
        _headline("Story one", "Source A", "2026-07-16T09:00:00+00:00"),
        _headline("Story two", "Source B", "2026-07-16T08:00:00+00:00"),
    ]
    monkeypatch.setattr(dedup, "embed", lambda texts: None)

    out = dedup.cluster(headlines, threshold=0.8)

    assert out == [
        {**headlines[0], "sources": ["Source A"], "dupe_count": 1},
        {**headlines[1], "sources": ["Source B"], "dupe_count": 1},
    ]


def test_cluster_empty_input_returns_empty_list(monkeypatch):
    monkeypatch.setattr(dedup, "embed", lambda texts: [])
    assert dedup.cluster([], threshold=0.8) == []


def test_embed_returns_none_on_request_failure(monkeypatch):
    def boom_post(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr(dedup.requests, "post", boom_post)
    assert dedup.embed(["hello"]) is None


def test_embed_returns_none_on_malformed_response(monkeypatch):
    class _FakeResp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"unexpected": "shape"}

    monkeypatch.setattr(dedup.requests, "post", lambda *a, **k: _FakeResp())
    assert dedup.embed(["hello"]) is None


def test_embed_empty_input_returns_empty_list_without_network_call(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("should not call requests.post for empty input")

    monkeypatch.setattr(dedup.requests, "post", boom)
    assert dedup.embed([]) == []
