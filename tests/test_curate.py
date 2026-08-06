"""Curation (brief/window/curate.py) — the ComfyUI render guard, the batched
scoring call, and the "watchlist" alert kind it feeds (dispatch-alerts-v1)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from brief.models import Item
from brief.window import curate, service


@pytest.fixture(autouse=True)
def _not_quiet_hours(monkeypatch):
    # curate.curate() now also skips during curation quiet hours (default
    # 18:00-9:00) -- tests must not depend on the real wall-clock time to
    # pass, so default every test to "not quiet" unless it explicitly tests
    # the quiet-hours behavior itself.
    monkeypatch.setattr(curate, "in_quiet_hours", lambda *a, **k: False)


class _Resp:
    def __init__(self, body, ok=True):
        self._body = body
        self.ok = ok

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError("http error")

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


# --- comfyui_busy: renders are sacred ---------------------------------------


def test_comfyui_busy_true_when_anything_running(monkeypatch):
    monkeypatch.setattr(
        curate.requests,
        "get",
        lambda *a, **k: _Resp({"queue_running": [["job"]], "queue_pending": []}),
    )
    assert curate.comfyui_busy() is True


def test_comfyui_idle_and_unreachable_are_not_busy(monkeypatch):
    monkeypatch.setattr(
        curate.requests,
        "get",
        lambda *a, **k: _Resp({"queue_running": [], "queue_pending": []}),
    )
    assert curate.comfyui_busy() is False

    def boom(*a, **k):
        raise ConnectionError("not running")

    monkeypatch.setattr(curate.requests, "get", boom)
    assert curate.comfyui_busy() is False


def test_comfyui_reachable_but_unparseable_counts_as_busy(monkeypatch):
    # When in doubt about a LIVE ComfyUI, protect the render.
    monkeypatch.setattr(
        curate.requests, "get", lambda *a, **k: _Resp(ValueError("not json"))
    )
    assert curate.comfyui_busy() is True


# --- curate(): skip, score, fail-soft ---------------------------------------


def test_curate_skips_without_calling_ollama_during_quiet_hours(monkeypatch):
    # the user, 2026-07-27: "it doesn't need to run between the hours of 6 PM
    # and 9 AM." Override the autouse "not quiet" default for this one test.
    monkeypatch.setattr(curate, "in_quiet_hours", lambda *a, **k: True)
    monkeypatch.setattr(curate, "comfyui_busy", lambda url: False)

    def no_post(*a, **k):
        raise AssertionError("Ollama must not be called during quiet hours")

    monkeypatch.setattr(curate.requests, "post", no_post)
    assert curate.curate([{"title": "T"}]) is None


def test_curate_passes_configured_quiet_hours_to_in_quiet_hours(monkeypatch):
    monkeypatch.setattr(curate, "comfyui_busy", lambda url: False)
    seen = {}

    def fake_in_quiet_hours(now, start, end):
        seen["start"], seen["end"] = start, end
        return True  # short-circuit before any Ollama call

    monkeypatch.setattr(curate, "in_quiet_hours", fake_in_quiet_hours)
    curate.curate([{"title": "T"}], quiet_start_hour=20, quiet_end_hour=6)
    assert seen == {"start": 20, "end": 6}


def test_curate_default_quiet_hours_are_6pm_to_9am():
    assert curate.DEFAULT_QUIET_START_HOUR == 18
    assert curate.DEFAULT_QUIET_END_HOUR == 9


def test_curate_skips_without_calling_ollama_when_comfyui_busy(monkeypatch):
    monkeypatch.setattr(curate, "comfyui_busy", lambda url: True)

    def no_post(*a, **k):
        raise AssertionError("Ollama must not be called during a render")

    monkeypatch.setattr(curate.requests, "post", no_post)
    assert curate.curate([{"title": "T"}]) is None


def test_curate_parses_and_clamps_scores(monkeypatch):
    monkeypatch.setattr(curate, "comfyui_busy", lambda url: False)
    monkeypatch.setattr(curate, "_profile", lambda: {"persona": "analyst"})
    content = json.dumps(
        {"scores": [{"i": 0, "s": 9}, {"i": 1, "s": 99}, {"i": 7, "s": 5}]}
    )
    monkeypatch.setattr(
        curate.requests,
        "post",
        lambda *a, **k: _Resp({"message": {"content": content}}),
    )
    got = curate.curate([{"title": "A"}, {"title": "B"}])
    assert got == {0: 9, 1: 10}  # 99 clamped to 10; index 7 out of range dropped


def test_curate_fail_soft_on_garbage_and_empty_profile(monkeypatch):
    monkeypatch.setattr(curate, "comfyui_busy", lambda url: False)
    monkeypatch.setattr(curate, "_profile", lambda: {"persona": "analyst"})
    monkeypatch.setattr(
        curate.requests,
        "post",
        lambda *a, **k: _Resp({"message": {"content": "not json at all"}}),
    )
    assert curate.curate([{"title": "A"}]) is None

    monkeypatch.setattr(curate, "_profile", lambda: {})
    assert curate.curate([{"title": "A"}]) is None  # no beat -> no curation


# --- batching: qwen3.5:9b's per-call latency made one giant call unsafe ----
# (measured live: 30 headlines took 122.5s with real variance -- batching
# bounds each individual call and lets a mid-cycle render or a bad batch cost
# only that chunk, not the whole cycle).


def _chunk_content(post_kwargs) -> str:
    return post_kwargs["json"]["messages"][1]["content"]


def _score_all_as(post_kwargs, score: int) -> _Resp:
    n = _chunk_content(post_kwargs).count("\n") + 1
    scores = [{"i": i, "s": score} for i in range(n)]
    return _Resp({"message": {"content": json.dumps({"scores": scores})}})


def test_curate_splits_into_batches_and_merges_global_indices(monkeypatch):
    monkeypatch.setattr(curate, "comfyui_busy", lambda url: False)
    monkeypatch.setattr(curate, "_profile", lambda: {"persona": "analyst"})
    calls = []

    def fake_post(url, **kwargs):
        calls.append((_chunk_content(kwargs).count("\n") + 1, kwargs["json"]["keep_alive"]))
        return _score_all_as(kwargs, 7)

    monkeypatch.setattr(curate.requests, "post", fake_post)
    headlines = [{"title": f"T{i}"} for i in range(7)]
    got = curate.curate(headlines, batch_size=3, timeout=5)
    # 7 headlines / batch_size 3 -> chunks of 3, 3, 1 -> 3 calls.
    assert [n for n, _ in calls] == [3, 3, 1]
    # keep_alive is 0 on EVERY batch (unload after each, never left resident).
    assert all(ka == 0 for _, ka in calls)
    # Global indices 0..6 all scored, correctly offset per chunk.
    assert got == {i: 7 for i in range(7)}


def test_curate_stops_remaining_batches_when_render_starts_mid_cycle(monkeypatch):
    busy_calls = {"n": 0}

    def busy(url):
        busy_calls["n"] += 1
        return busy_calls["n"] > 1  # idle for the FIRST check, busy after

    monkeypatch.setattr(curate, "comfyui_busy", busy)
    monkeypatch.setattr(curate, "_profile", lambda: {"persona": "analyst"})
    posts = []

    def fake_post(url, **kwargs):
        posts.append(1)
        return _score_all_as(kwargs, 5)

    monkeypatch.setattr(curate.requests, "post", fake_post)
    headlines = [{"title": f"T{i}"} for i in range(9)]
    got = curate.curate(headlines, batch_size=3, timeout=5)
    # Only the FIRST batch (before the render-started check fires) is scored;
    # the remaining two chunks are skipped, not attempted.
    assert len(posts) == 1
    assert got == {0: 5, 1: 5, 2: 5}


def test_curate_one_bad_batch_does_not_cost_the_rest(monkeypatch):
    monkeypatch.setattr(curate, "comfyui_busy", lambda url: False)
    monkeypatch.setattr(curate, "_profile", lambda: {"persona": "analyst"})

    def flaky_post(url, **kwargs):
        if "T0" in _chunk_content(kwargs):
            raise ConnectionError("down")
        return _score_all_as(kwargs, 6)

    monkeypatch.setattr(curate.requests, "post", flaky_post)
    headlines = [{"title": f"T{i}"} for i in range(6)]
    got = curate.curate(headlines, batch_size=3, timeout=5)
    # First chunk (T0-T2) failed entirely; second chunk (T3-T5) still scored.
    assert got == {3: 6, 4: 6, 5: 6}


# --- NewsLoop wiring ---------------------------------------------------------


def test_news_loop_marks_beat_headlines(monkeypatch):
    def fake_fetch_all(sources):
        return [
            Item(
                source_name="S",
                source_type="news",
                title=f"T{i}",
                url=f"http://x/{i}",
                published_at="2026-07-16T00:00:00+00:00",
            )
            for i in range(2)
        ]

    monkeypatch.setattr(service.rss, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(service.curate, "curate", lambda hs, **k: {0: 9, 1: 4})

    state = service.WindowState(sweep_interval_seconds=900)
    loop = service.NewsLoop(
        [{"name": "S", "rss": "http://example.test/feed"}],
        state,
        interval_seconds=5,
        dedup_enabled=False,
        curation_enabled=True,
        curation_min_score=8,
    )
    loop.run_one_fetch()
    news = state.news_snapshot()
    flags = {h["title"]: (h.get("beat"), h.get("beat_score")) for h in news}
    assert flags["T0"] == (True, 9)
    assert flags["T1"] == (False, 4)


def test_news_loop_threads_curation_quiet_hours_to_curate(monkeypatch):
    monkeypatch.setattr(
        service.rss,
        "fetch_all",
        lambda sources: [
            Item(source_name="S", source_type="news", title="T", url="http://x/1")
        ],
    )
    seen = {}

    def fake_curate(headlines, **kwargs):
        seen.update(kwargs)
        return None

    monkeypatch.setattr(service.curate, "curate", fake_curate)
    state = service.WindowState(sweep_interval_seconds=900)
    loop = service.NewsLoop(
        [],
        state,
        interval_seconds=5,
        dedup_enabled=False,
        curation_enabled=True,
        curation_quiet_start_hour=20,
        curation_quiet_end_hour=6,
    )
    loop.run_one_fetch()
    assert seen.get("quiet_start_hour") == 20
    assert seen.get("quiet_end_hour") == 6


# --- watchlist alerts --------------------------------------------------------


def test_build_alerts_watchlist_kind_and_no_double_alert():
    state = service.WindowState(sweep_interval_seconds=900)
    now = datetime.now(timezone.utc)
    fresh = now.strftime("%Y-%m-%d %H:%M:%S")
    stale = (now - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
    state.record_news(
        [
            {
                "title": "DIA award",
                "url": "u1",
                "beat": True,
                "beat_score": 9,
                "dupe_count": 1,
                "first_seen": fresh,
            },
            # Also a 4-outlet cluster -> alerted as "news", NOT again as watchlist.
            {
                "title": "Big beat cluster",
                "url": "u2",
                "beat": True,
                "beat_score": 8,
                "dupe_count": 4,
                "first_seen": fresh,
            },
            {
                "title": "Old beat story",
                "url": "u3",
                "beat": True,
                "beat_score": 9,
                "dupe_count": 1,
                "first_seen": stale,
            },
            {
                "title": "Off-beat",
                "url": "u4",
                "beat": False,
                "dupe_count": 1,
                "first_seen": fresh,
            },
        ]
    )
    body = service.build_alerts(state)
    by_kind = {}
    for a in body["alerts"]:
        by_kind.setdefault(a["kind"], []).append(a)
    assert [a["title"] for a in by_kind.get("watchlist", [])] == ["DIA award"]
    assert by_kind["watchlist"][0]["speak"] == "On your watchlist. DIA award."
    assert [a["title"] for a in by_kind.get("news", [])] == ["Big beat cluster"]
