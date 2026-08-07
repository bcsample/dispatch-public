"""The sweep loop + status/health logic behind the dashboard.

Reuses the existing World Delta engine (`brief.ingest.world.fetch_all`) on a
timer, and answers a fixed status contract (`GET /api/status`) so any
external monitoring/dashboard tool can build against a stable shape.

Layered on top of the base sweep:
- the sweep also retains each feed's *current* state (not just deltas) via
  `world.fetch_all`'s optional `current` out-param, for `/api/current`.
- a second, lighter-cadence loop (`NewsLoop`) reuses `brief.ingest.rss` to
  cache headlines for `/api/news`. Headlines only, no synthesis.

LLM boundary (v4): the 10s POLL path (build_status / recent_deltas /
current_state / check_health, backing /api/status /deltas /current /health)
stays LLM-free — it must never touch Ollama or brief/generate.py. The NewsLoop
(~10min cadence, off the poll) may cluster headlines via `dedup` (nomic-embed,
fail-soft); that's the only Ollama use, and it never runs per-request.
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .. import applog, db
from ..ingest import gdelt, googlenews, newsapi, rss, world
from . import curate, dedup, flightroute, flights, geo, stoplist, surge

log = applog.get(__name__)

# ---------------------------------------------------------------------------
# v1 feed scope (see the plan: "Feeds in v1: USGS quakes + OFAC sanctions
# only ... NASA fires only lights up if NASA_FIRMS_MAP_KEY is set. Flights
# stays skipped."). config/world_feeds.yaml stays the one shared roster —
# v1 just doesn't light up every adapter in it yet; v2 widens this set, not
# the yaml shape.
# ---------------------------------------------------------------------------
_V1_ALWAYS_ON_ADAPTERS = {"usgs_quakes", "ofac_sdn", "cisa_kev"}
_V1_CONDITIONAL_ADAPTERS = {"nasa_firms": "NASA_FIRMS_MAP_KEY"}


def select_v1_feeds(all_feeds: list[dict]) -> list[dict]:
    """Filter the shared world_feeds.yaml roster down to what v1 actually
    sweeps. opensky_flights (and any future/unknown adapter) is left out —
    v2 territory or explicitly out of scope for this build."""
    selected = []
    for feed in all_feeds:
        adapter = feed.get("adapter", "")
        if adapter in _V1_ALWAYS_ON_ADAPTERS:
            selected.append(feed)
        elif adapter in _V1_CONDITIONAL_ADAPTERS:
            env_var = _V1_CONDITIONAL_ADAPTERS[adapter]
            if os.environ.get(env_var, "").strip():
                selected.append(feed)
    return selected


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _strip_kind_prefix(title: str) -> str:
    """Items are stored as "New: <label>" / "Updated: <label>" /
    "No longer active: <label>" (brief.ingest.world._build_item). The status
    contract's top_event example is the bare label, so strip the fixed
    prefix back off for display."""
    for prefix in ("New: ", "Updated: ", "No longer active: "):
        if title.startswith(prefix):
            return title[len(prefix) :]
    return title


@dataclass
class WindowState:
    """Thread-safe snapshot the FastAPI handlers read. One writer (the sweep
    loop thread), any number of HTTP-request readers."""

    sweep_interval_seconds: int
    last_sweep_at: str | None = field(default=None, init=False)
    last_sweep_ok: bool = field(default=False, init=False)
    feeds: list[dict] = field(default_factory=list, init=False)
    error: str | None = field(default=None, init=False)
    started_at: str = field(default_factory=_utcnow_iso, init=False)
    # v2 — retained so the window shows "the world now", not just deltas
    #, V2.1/V2.2).
    current: list[dict] = field(default_factory=list, init=False)
    news: list[dict] = field(default_factory=list, init=False)
    flights: list[dict] = field(default_factory=list, init=False)
    # Per-component feed health (RSS/GDELT/NewsAPI/Flights) — name -> {last_ok_at,
    # error}. World feeds (USGS/OFAC/CISA-KEV) reuse the existing `feeds`/
    # `last_sweep_at` above instead of duplicating state here; see
    # compute_feed_health().
    feed_health: dict[str, dict] = field(default_factory=dict, init=False)
    _lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False
    )

    def record_sweep(
        self,
        feeds: list[dict],
        ok: bool,
        error: str | None = None,
        current: list[dict] | None = None,
    ) -> None:
        with self._lock:
            self.last_sweep_at = _utcnow_iso()
            self.last_sweep_ok = ok
            self.feeds = feeds
            self.error = error
            # additive, mirrors `stats`: omitted -> leave whatever was there.
            if current is not None:
                self.current = current

    def record_news(self, headlines: list[dict]) -> None:
        """v2 — cache the latest headlines from the news loop (see NewsLoop)."""
        with self._lock:
            self.news = headlines

    def record_feed_health(self, name: str, ok: bool, error: str | None = None) -> None:
        """A named component (RSS/GDELT/NewsAPI/Flights) just attempted a
        fetch. On success, stamps `last_ok_at` (age since this is what
        compute_feed_health buckets on) and clears any prior error. On
        failure, records the error but leaves `last_ok_at` at its last real
        success -- so a component that's been down for hours reads as
        genuinely stale, not falsely "just failed a second ago"."""
        with self._lock:
            entry = self.feed_health.setdefault(
                name, {"last_ok_at": None, "error": None}
            )
            if ok:
                entry["last_ok_at"] = _utcnow_iso()
                entry["error"] = None
            else:
                entry["error"] = error

    def feed_health_snapshot(self) -> dict:
        with self._lock:
            return {k: dict(v) for k, v in self.feed_health.items()}

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "last_sweep_at": self.last_sweep_at,
                "last_sweep_ok": self.last_sweep_ok,
                "feeds": list(self.feeds),
                "error": self.error,
                "started_at": self.started_at,
            }

    def current_snapshot(self) -> list[dict]:
        """GET /api/current payload — per-feed current records captured by
        the sweep's `current` out-param (world.fetch_all), already floored
        by each feed's min_severity from world_feeds.yaml."""
        with self._lock:
            return list(self.current)

    def news_snapshot(self) -> list[dict]:
        """GET /api/news payload — cached headlines, newest-first (NewsLoop
        sorts before caching)."""
        with self._lock:
            return list(self.news)

    def record_flights(self, aircraft: list[dict]) -> None:
        with self._lock:
            self.flights = aircraft

    def flights_snapshot(self) -> list[dict]:
        """GET /api/flights payload — cached aircraft board (FlightLoop)."""
        with self._lock:
            return list(self.flights)


class SweepLoop:
    """Background thread: `world.fetch_all()` on a timer. Fail-soft one level
    up from the per-feed fail-soft already inside fetch_all — a totally
    broken sweep (e.g. the DB briefly unreachable) still leaves the loop
    alive to try again next interval instead of killing the thread."""

    def __init__(
        self, world_feeds: list[dict], state: WindowState, interval_seconds: int
    ):
        self.world_feeds = world_feeds
        self.state = state
        self.interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def run_one_sweep(self) -> None:
        """One sweep cycle: fetch_all() -> store the resulting Items via the
        existing db.upsert_items() (no LLM, no scoring — structured storage
        only) -> update the shared state. Public so tests/manual runs can
        trigger a single sweep synchronously without starting the thread."""
        con = db.connect()
        try:
            stats: list[dict] = []
            current: list[dict] = []
            try:
                items = world.fetch_all(
                    self.world_feeds, con, stats=stats, current=current
                )
            except Exception as exc:  # noqa: BLE001 — sweep-level fail-soft
                self.state.record_sweep(
                    stats, ok=False, error=repr(exc), current=current
                )
                return
            if items:
                db.upsert_items(con, items)
            self.state.record_sweep(stats, ok=True, current=current)
        finally:
            con.close()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_one_sweep()
            except (
                Exception
            ) as exc:  # noqa: BLE001 — a bad cycle must NOT kill the thread
                log.error("sweep loop cycle FAILED (%r)", exc)
            self._stop.wait(self.interval_seconds)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="world-window-sweep", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()


def _headline_dicts(items: list) -> list[dict]:
    """Item -> the /api/news shape, newest-first. Items with no parsed
    published_at sort last rather than raising."""
    ordered = sorted(items, key=lambda it: it.published_at or "", reverse=True)
    out = []
    for it in ordered:
        d = {
            "title": it.title,
            "source_name": it.source_name,
            "url": it.url,
            "published_at": it.published_at,
            # Deterministic sports/entertainment flag — a backstop for the map
            # filter and surge, independent of whether curation ran this cycle.
            "noise": stoplist.is_noise(it.title),
        }
        # v4.2 stage 1: geolocate the headline (off the live path — this runs in
        # the news loop). Adds lat/lon/place when a place name is recognized, so
        # the map can drop a clickable news pin. No pin when nothing matches.
        loc = geo.locate(it.title)
        if loc:
            d["lat"], d["lon"], d["place"] = loc["lat"], loc["lon"], loc["place"]
        out.append(d)
    return out


class NewsLoop:
    """Background thread: brief.ingest.rss.fetch_all() on its own, lighter
    interval (v2 — 's "V2.2 — News panel"). Headlines
    only, no synthesis — this stays on the LLM-free live path. rss.fetch_all()
    is already fail-soft per source (one dead RSS source logs and is skipped);
    a totally unexpected top-level failure here just leaves the previously
    cached headlines in place rather than blanking the panel.

    v4.1: after building the
    headline dicts, optionally run them through `dedup.cluster()` to collapse
    near-duplicate stories from multiple sources into one row. This is the
    ONE place Ollama is ever called on the window's live path — the 10s poll
    (`/api/status`, `/api/health`, `/api/deltas`, `/api/current`) never touches
    it; clustering runs here, on the news-fetch cadence (~10min), and is
    fail-soft (dedup.cluster falls back to passthrough singletons if Ollama is
    unreachable)."""

    def __init__(
        self,
        sources: list[dict],
        state: WindowState,
        interval_seconds: int,
        limit: int = 100,
        dedup_enabled: bool = True,
        dedup_similarity: float = dedup.DEFAULT_SIMILARITY_THRESHOLD,
        retain_hours: float = 48,
        curation_enabled: bool = False,
        curation_model: str = curate.DEFAULT_MODEL,
        curation_min_score: int = 8,
        curation_quiet_start_hour: int = curate.DEFAULT_QUIET_START_HOUR,
        curation_quiet_end_hour: int = curate.DEFAULT_QUIET_END_HOUR,
        comfyui_url: str = curate.DEFAULT_COMFYUI_URL,
        gdelt_enabled: bool = False,
        gdelt_query: str = gdelt.DEFAULT_QUERY,
        gdelt_timespan: str = "1h",
        gdelt_max_records: int = 75,
        newsapi_enabled: bool = False,
        newsapi_keywords: list[str] | None = None,
        newsapi_count: int = 40,
        newsapi_every_n_cycles: int = 1,
        googlenews_enabled: bool = False,
        googlenews_count_per_keyword: int = 8,
    ):
        self.sources = sources
        self.state = state
        self.interval_seconds = interval_seconds
        self.limit = limit
        self.dedup_enabled = dedup_enabled
        self.dedup_similarity = dedup_similarity
        self.retain_hours = retain_hours
        self.curation_enabled = curation_enabled
        self.curation_model = curation_model
        self.curation_min_score = curation_min_score
        self.curation_quiet_start_hour = curation_quiet_start_hour
        self.curation_quiet_end_hour = curation_quiet_end_hour
        self.comfyui_url = comfyui_url
        self.gdelt_enabled = gdelt_enabled
        self.gdelt_query = gdelt_query
        self.gdelt_timespan = gdelt_timespan
        self.gdelt_max_records = gdelt_max_records
        # Backoff state (2026-07-28, Fable's full-log scan: 755 real GDELT
        # 429s found in the wild) -- consecutive rate-limit signals grow the
        # skip window exponentially (2, 4, 8... capped at 12 cycles, ~2h at
        # the default 10-min cadence), reset to 0 the moment a call succeeds.
        self._gdelt_consecutive_ratelimits = 0
        self._gdelt_backoff_cycles_remaining = 0
        self.newsapi_enabled = newsapi_enabled
        self.newsapi_keywords = newsapi_keywords
        self.newsapi_count = newsapi_count
        # Dial-back (2026-07-24): the user's Event Registry token quota was half
        # gone -- NewsAPI.ai was being called every cycle. Default stays 1
        # (unchanged behavior) but window.yaml now sets this higher so the
        # real deployment calls it far less often while Google News RSS
        # (free, no quota) takes over the beat-feed role day to day.
        self.newsapi_every_n_cycles = max(1, newsapi_every_n_cycles)
        self.googlenews_enabled = googlenews_enabled
        self.googlenews_count_per_keyword = googlenews_count_per_keyword
        self._cycle_count = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def run_one_fetch(self) -> None:
        """One news-fetch cycle. Public so tests/manual runs can trigger it
        synchronously without starting the thread."""
        self._cycle_count += 1
        try:
            items = rss.fetch_all(self.sources)
            self.state.record_feed_health("RSS", True)
        except (
            Exception
        ) as exc:  # noqa: BLE001 — keep previous headlines, don't blank the panel
            log.error("news fetch FAILED (%r)", exc)
            self.state.record_feed_health("RSS", False, repr(exc))
            return
        # GDELT widens coverage far past the RSS roster; its Items join the same
        # pipeline (dedup/geo/first_seen/curate). gdelt.fetch() returns None
        # specifically on a rate-limit signal (real 429 or the 200-plaintext
        # throttle notice) -- that drives real backoff instead of retrying
        # into the same limiter every cycle. Any OTHER failure still fails
        # soft to [] (a throttled/errored cycle just adds nothing).
        if self.gdelt_enabled:
            if self._gdelt_backoff_cycles_remaining > 0:
                self._gdelt_backoff_cycles_remaining -= 1
                log.info(
                    "GDELT backoff: skipping this cycle (%d more to go)",
                    self._gdelt_backoff_cycles_remaining,
                )
            else:
                result = gdelt.fetch(
                    query=self.gdelt_query,
                    timespan=self.gdelt_timespan,
                    max_records=self.gdelt_max_records,
                )
                if result is None:
                    self._gdelt_consecutive_ratelimits += 1
                    self._gdelt_backoff_cycles_remaining = min(
                        2**self._gdelt_consecutive_ratelimits, 12
                    )
                    self.state.record_feed_health(
                        "GDELT", False,
                        f"rate-limited, backing off {self._gdelt_backoff_cycles_remaining} cycles",
                    )
                else:
                    self._gdelt_consecutive_ratelimits = 0
                    items = items + result
                    self.state.record_feed_health("GDELT", True)
        # NewsAPI.ai (Event Registry) — the real-time beat feed; same pipeline,
        # fail-soft inside newsapi.fetch (no key / error -> nothing added).
        # Dialed back to every newsapi_every_n_cycles cycles (default 1 = old
        # behavior) so it stops burning through the user's token quota now that
        # Google News RSS (below) carries the beat-feed role day to day.
        if self.newsapi_enabled and self._cycle_count % self.newsapi_every_n_cycles == 0:
            items = items + newsapi.fetch(
                keywords=self.newsapi_keywords, count=self.newsapi_count
            )
            self.state.record_feed_health("NewsAPI", True)
        # Google News RSS — the free (no key, no quota) beat feed. One request
        # per keyword; fail-soft per keyword inside googlenews.fetch. Keywords
        # are loaded fresh each cycle (not captured at startup) so edits made
        # via the keyword management page (GET /keywords) take effect on the
        # very next fetch, no restart needed.
        if self.googlenews_enabled:
            items = items + googlenews.fetch(
                count_per_keyword=self.googlenews_count_per_keyword
            )
            self.state.record_feed_health("Google News", True)
        headlines = _headline_dicts(items)
        if self.dedup_enabled:
            headlines = dedup.cluster(headlines, threshold=self.dedup_similarity)
        headlines = headlines[: self.limit]
        # Persist into the small rolling window and stamp each headline with its
        # first_seen, so the board can tell a genuinely-new story from one that's
        # been scrolling for an hour. Fail-soft: a DB hiccup must not blank the
        # news panel — we just skip the first_seen stamp this cycle.
        self._stamp_first_seen(headlines)
        # Curation (pillar 3): one batched local-qwen scoring pass per cycle,
        # skipped entirely while ComfyUI renders (curate.comfyui_busy — renders
        # are sacred) OR during curation quiet hours (the user: doesn't need to
        # run 6pm-9am — nobody's reading fresh beat scores overnight, and
        # that's also when ComfyUI is most often mid-render anyway). None ->
        # this cycle's headlines just go out unscored.
        if self.curation_enabled:
            scores = curate.curate(
                headlines,
                model=self.curation_model,
                comfyui_url=self.comfyui_url,
                quiet_start_hour=self.curation_quiet_start_hour,
                quiet_end_hour=self.curation_quiet_end_hour,
            )
            if scores:
                fresh: dict[str, int] = {}
                for i, h in enumerate(headlines):
                    if i in scores:
                        h["beat_score"] = scores[i]
                        h["beat"] = scores[i] >= self.curation_min_score
                        if h.get("url"):
                            fresh[h["url"]] = scores[i]
                self._persist_beat(fresh)
        self.state.record_news(headlines)

    def _stamp_first_seen(self, headlines: list[dict]) -> None:
        try:
            con = db.connect()
            try:
                db.upsert_news(con, headlines)
                db.prune_news(con, keep_hours=self.retain_hours)
                seen = db.news_first_seen_map(con)
                beat = db.news_beat_map(con)
            finally:
                con.close()
        except Exception as exc:  # noqa: BLE001 — news must survive a bad DB
            log.error("news persist FAILED (%r)", exc)
            return
        for h in headlines:
            fs = seen.get(h.get("url"))
            if fs is not None:
                h["first_seen"] = fs
            # Re-apply the last known beat score, so a cycle where curation
            # SKIPPED (ComfyUI busy) keeps the map filtered instead of showing
            # everything unscored.
            bs = beat.get(h.get("url"))
            if bs is not None:
                h["beat_score"] = bs
                h["beat"] = bs >= self.curation_min_score

    def _persist_beat(self, scores: dict[str, int]) -> None:
        if not scores:
            return
        try:
            con = db.connect()
            try:
                db.set_beat_scores(con, scores)
            finally:
                con.close()
        except Exception as exc:  # noqa: BLE001 — best-effort
            log.error("beat persist FAILED (%r)", exc)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_one_fetch()
            except (
                Exception
            ) as exc:  # noqa: BLE001 — a bad cycle must NOT kill the thread
                log.error("news loop cycle FAILED (%r)", exc)
            self._stop.wait(self.interval_seconds)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="world-window-news", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()


class FlightLoop:
    """Background thread: the live "Skies" board (flights.fetch_flights) on its
    own short cadence. No LLM, no DB, no ComfyUI memory risk — safe to run at
    all times. Disabled by default (no location -> the panel just stays empty).
    Fail-soft: a failed fetch keeps the previously cached board."""

    def __init__(
        self,
        state: WindowState,
        lat: float | None,
        lon: float | None,
        radius_nm: float = 30,
        limit: int = 12,
        interval_seconds: int = 25,
    ):
        self.state = state
        self.lat = lat
        self.lon = lon
        self.radius_nm = radius_nm
        self.limit = limit
        self.interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def enabled(self) -> bool:
        return self.lat is not None and self.lon is not None

    def run_one_fetch(self) -> None:
        if not self.enabled:
            return
        aircraft = flights.fetch_flights(
            self.lat, self.lon, radius_nm=self.radius_nm, limit=self.limit
        )
        if aircraft is not None:
            # Enrich with airline + from/to route (adsbdb, cached). Fail-soft:
            # un-enriched flights just show a blank route.
            flightroute.enrich(aircraft)
            self.state.record_flights(aircraft)
            self.state.record_feed_health("Flights", True)
        else:
            self.state.record_feed_health(
                "Flights", False, "fetch_flights returned None"
            )

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_one_fetch()
            except (
                Exception
            ) as exc:  # noqa: BLE001 — a bad cycle must NOT kill the thread
                log.error("flight loop cycle FAILED (%r)", exc)
            self._stop.wait(self.interval_seconds)

    def start(self) -> None:
        if not self.enabled:
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="world-window-flights", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()


def check_health(
    loop: SweepLoop, news_loop=None, flight_loop=None
) -> tuple[bool, dict]:
    """GET /api/health contract: ok when the CORE loop threads (sweep + news)
    are alive AND the DB is reachable — a dead news/flight thread must not hide
    behind a green light (the "glance at a dead board and believe it" failure).
    Flights is optional context (only reported when it's enabled)."""
    threads = {"sweep": loop.is_alive()}
    if news_loop is not None:
        threads["news"] = news_loop.is_alive()
    if flight_loop is not None and getattr(flight_loop, "enabled", False):
        threads["flights"] = flight_loop.is_alive()

    db_ok = True
    db_error = None
    try:
        con = db.connect()
        con.execute("SELECT 1")
        con.close()
    except Exception as exc:  # noqa: BLE001 — reporting is the point here
        db_ok = False
        db_error = repr(exc)

    core_alive = threads["sweep"] and threads.get("news", True)
    detail = {"threads": threads, "db_reachable": db_ok}
    if db_error:
        detail["db_error"] = db_error
    return (core_alive and db_ok), detail


def _today_world_stats(con: sqlite3.Connection) -> tuple[int, float | None, str | None]:
    """deltas_today / highest_severity_today / top_event, computed from the
    shared `items` table (source_type='world') rather than an in-memory
    counter — both this window's sweeps and the daily pipeline write into
    that same table via db.upsert_items, so a DB query is correct regardless
    of which surface produced a given delta or whether this process just
    restarted. "Today" = UTC calendar day (SQLite's date('now') is UTC).

    Ranking: highest-severity item today wins top_event (a fires/quakes
    signal). If nothing today carries a severity (e.g. only OFAC sanctions
    deltas, which are presence/absence signals with severity=None), fall
    back to the most recently seen delta instead of leaving top_event null
    outright — the contract's example always shows a populated top_event,
    and "nothing changed" is already visible via deltas_today == 0."""
    rows = con.execute(
        "SELECT title, severity FROM items "
        "WHERE source_type = 'world' AND date(first_seen) = date('now') "
        "ORDER BY first_seen DESC"
    ).fetchall()
    deltas_today = len(rows)
    highest = None
    top_event = None
    for r in rows:
        sev = r["severity"]
        if sev is not None and (highest is None or sev > highest):
            highest = sev
            top_event = _strip_kind_prefix(r["title"])
    if top_event is None and rows:
        top_event = _strip_kind_prefix(rows[0]["title"])
    return deltas_today, highest, top_event


_FRESH_S, _STALE_S, _VERY_STALE_S = 15 * 60, 60 * 60, 6 * 60 * 60


def _feed_bucket(age_seconds: float | None, ok: bool, ever_succeeded: bool) -> str:
    """fresh (<15m) / stale (<1h) / very_stale (<6h) / error (>=6h, an
    explicit failure, or never having succeeded at all)."""
    if not ok or not ever_succeeded or age_seconds is None:
        return "error"
    if age_seconds < _FRESH_S:
        return "fresh"
    if age_seconds < _STALE_S:
        return "stale"
    if age_seconds < _VERY_STALE_S:
        return "very_stale"
    return "error"


def compute_feed_health(state: WindowState) -> list[dict]:
    """GET /api/feed_health payload — small per-source freshness chips,
    distinct from the full-board STALE overlay (that's a whole-connection
    watchdog; this is "which individual feed hasn't updated in a while").
    World feeds (USGS/OFAC/CISA-KEV) reuse the existing sweep's per-feed `ok`
    + shared `last_sweep_at` (they sweep together, so they share one age).
    RSS/GDELT/NewsAPI/Flights are tracked individually via
    WindowState.record_feed_health -- see NewsLoop/FlightLoop."""
    now = datetime.now(timezone.utc)
    out: list[dict] = []

    snap = state.snapshot()
    sweep_age = None
    sweep_at = _parse_iso(snap["last_sweep_at"])
    if sweep_at is not None:
        sweep_age = (now - sweep_at).total_seconds()
    for f in snap["feeds"]:
        ok = bool(f.get("ok"))
        out.append(
            {
                "name": f.get("name"),
                "age_seconds": sweep_age,
                "bucket": _feed_bucket(sweep_age, ok, sweep_at is not None),
            }
        )

    for name, entry in state.feed_health_snapshot().items():
        last_ok = _parse_iso(entry.get("last_ok_at"))
        age = (now - last_ok).total_seconds() if last_ok is not None else None
        out.append(
            {
                "name": name,
                "age_seconds": age,
                "bucket": _feed_bucket(
                    age, entry.get("error") is None, last_ok is not None
                ),
            }
        )
    return out


def build_status(
    state: WindowState, sweep_interval_seconds: int, loops: dict | None = None
) -> dict:
    """GET /api/status contract — field names/shape must match
     exactly. `loops` (optional, additive: no new
    field) folds core-thread liveness into `ok`, so the dashboard's health dot
    goes degraded when the sweep or news thread has died — not just when a
    sweep reported an error."""
    con = db.connect()
    try:
        deltas_today, highest_severity_today, top_event = _today_world_stats(con)
    finally:
        con.close()

    snap = state.snapshot()
    # Before the first sweep completes there's nothing to be "not ok" about
    # yet; ok tracks the last sweep's own outcome once one has happened.
    ok = snap["last_sweep_ok"] if snap["last_sweep_at"] else True
    if loops:
        sweep_alive = loops["sweep"].is_alive()
        news_alive = loops["news"].is_alive() if loops.get("news") else True
        ok = ok and sweep_alive and news_alive

    return {
        "service": "dispatch",
        "ok": ok,
        # Additive (wall-mode hardening): lets the always-on browser tab detect
        # a service restart and reload itself, so template deploys reach the
        # wall without anyone touching it.
        "started_at": snap["started_at"],
        "last_sweep_at": snap["last_sweep_at"],
        "sweep_interval_seconds": sweep_interval_seconds,
        "feeds": [
            {
                "name": f.get("name"),
                "ok": f.get("ok"),
                "records": f.get("records"),
                "deltas_last_sweep": f.get("deltas_last_sweep"),
            }
            for f in snap["feeds"]
        ],
        "deltas_today": deltas_today,
        "highest_severity_today": highest_severity_today,
        "top_event": top_event,
    }


def recent_deltas(limit: int = 200) -> list[dict]:
    """Feeds both the dashboard's delta list and its map pins (items with
    lat/lon). Not part of the fixed status contract — a v1 convenience
    endpoint for the page itself."""
    con = db.connect()
    try:
        rows = con.execute(
            "SELECT source_name, title, url, published_at, first_seen, "
            "severity, lat, lon, delta_kind "
            "FROM items WHERE source_type = 'world' "
            "ORDER BY first_seen DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        con.close()
    return [
        {
            "source_name": r["source_name"],
            "title": _strip_kind_prefix(r["title"]),
            "url": r["url"],
            "published_at": r["published_at"],
            "first_seen": r["first_seen"],
            "severity": r["severity"],
            "lat": r["lat"],
            "lon": r["lon"],
            "delta_kind": r["delta_kind"],
        }
        for r in rows
    ]


def current_state(state: WindowState, limit: int = 200) -> list[dict]:
    """GET /api/current payload (v2) — per-feed current records, e.g.
    `[{"name": "USGS Earthquakes", "total": 15, "records": [{"key","title",
    "lat","lon","severity","url"}, ...]}, ...]`. This is what makes the window
    "full" the moment it loads, instead of waiting for a delta.

    Records are capped to `limit` per feed: the OFAC feed carries ~19k rows and
    the dashboard polls this every 10s, so an uncapped payload was ~2.9MB per
    poll (too slow over anything but a fast local link). `total` carries the
    TRUE count so the board
    still reads "19217 current" while only the top `limit` rows cross the wire
    (quakes are already floored/sorted well under the cap; sanctions show a
    count + this-sweep's additions, which the cap comfortably covers)."""
    out: list[dict] = []
    for feed in state.current_snapshot():
        records = feed.get("records") or []
        out.append(
            {
                "name": feed.get("name"),
                "total": len(records),
                "records": records[:limit],
            }
        )
    return out


# ---------------------------------------------------------------------------
# dispatch-alerts-v1 — the LIVE breaking feed for voice consumers.
# Sibling of the DISPATCH-MD v1 block contract (
# §9): DISPATCH-MD is the once-a-morning digest handoff; THIS is the
# minutes-cadence "really important things" handoff. Same discipline — fixed,
# versioned shape; version-bump on any breaking change, never silently reshape.
#
# Significance is deterministic and LLM-free (poll-safe):
#   world: severity >= min_severity AND first_seen within 24h
#   news:  dupe_count >= min_sources (independent outlets on one story) AND
#          first_seen within fresh_minutes
#   surge: story count on one PLACE in the last hour is a z_threshold-sigma
#          outlier vs its hourly baseline (brief/window/surge.py)
# Each alert carries a ready-to-speak `speak` string so consumers never have
# to compose language; `id` is stable so a consumer can track what it already
# announced across polls (surge ids rotate hourly, so an ongoing surge
# re-announces at most once an hour).
# ---------------------------------------------------------------------------

ALERTS_CONTRACT = "dispatch-alerts-v1"


def _alert_id(kind: str, key: str) -> str:
    return hashlib.sha1(f"{kind}|{key}".encode()).hexdigest()[:16]


_MAG_RE = re.compile(r"\bM(\d(?:\.\d+)?)\b")
_KM_RE = re.compile(r"\b(\d+)\s*km\b", re.IGNORECASE)
# USGS phrasing is "90 km SW of X" — spell the bearing out, but only when it's
# followed by "of", so a stray "W" or "S" elsewhere isn't mangled.
_COMPASS = {
    "N": "north",
    "S": "south",
    "E": "east",
    "W": "west",
    "NE": "northeast",
    "NW": "northwest",
    "SE": "southeast",
    "SW": "southwest",
    "NNE": "north-northeast",
    "ENE": "east-northeast",
    "ESE": "east-southeast",
    "SSE": "south-southeast",
    "SSW": "south-southwest",
    "WSW": "west-southwest",
    "WNW": "west-northwest",
    "NNW": "north-northwest",
}
_COMPASS_RE = re.compile(
    r"\b(" + "|".join(sorted(_COMPASS, key=len, reverse=True)) + r")\b(?=\s+of\b)"
)


# Non-Latin script blocks: the voice (Kokoro bm_george/en-gb, or macOS `say`
# Daniel fallback) is English-only -- forcing it to phonemize Arabic/Farsi/
# Hebrew/Cyrillic/CJK/etc. text produces garbled, broken-sounding audio
# (the user, 2026-07-21: "one of the sources was in Arabic or Farsi, and the
# system really choked on that"). Headlines dominated by these scripts are
# excluded from SPEECH ONLY -- the board/ticker still show them fine, since
# rendering Unicode visually isn't the problem, reading it aloud is.
_NON_LATIN_SCRIPT_RANGES = [
    (0x0590, 0x05FF),  # Hebrew
    (0x0600, 0x06FF),  # Arabic (also covers Farsi/Urdu additions)
    (0x0750, 0x077F),  # Arabic Supplement
    (0x08A0, 0x08FF),  # Arabic Extended-A
    (0x0400, 0x04FF),  # Cyrillic
    (0x0900, 0x097F),  # Devanagari
    (0x0E00, 0x0E7F),  # Thai
    (0x3040, 0x30FF),  # Hiragana / Katakana
    (0x4E00, 0x9FFF),  # CJK Unified Ideographs
    (0xAC00, 0xD7A3),  # Hangul
]


def _is_speakable(text: str | None, threshold: float = 0.3) -> bool:
    """False if a large enough share of `text`'s letters fall in a non-Latin
    script that the English-only voice would mangle. Titles with just an
    occasional foreign proper noun still pass (threshold is a fraction, not
    "any non-Latin character")."""
    letters = [c for c in (text or "") if c.isalpha()]
    if not letters:
        return True
    non_latin = sum(
        1
        for c in letters
        if any(lo <= ord(c) <= hi for lo, hi in _NON_LATIN_SCRIPT_RANGES)
    )
    return (non_latin / len(letters)) < threshold


def _speechify(text: str | None, limit: int = 180) -> str:
    """Make a headline read naturally aloud: expand the abbreviations that
    sound wrong ("M6.2" -> "magnitude 6.2", "90 km" -> "90 kilometers",
    "SW of" -> "southwest of"), turn dashes into pauses, tidy punctuation, and
    trim over-long headlines at a word boundary so the voice doesn't monologue."""
    t = (text or "").strip()
    t = _MAG_RE.sub(r"magnitude \1", t)
    t = _KM_RE.sub(r"\1 kilometers", t)
    t = _COMPASS_RE.sub(lambda m: _COMPASS[m.group(1)], t)
    t = t.replace("—", ", ").replace(" – ", ", ").replace(" - ", ", ")
    t = re.sub(r"\s+", " ", t)
    t = re.sub(r"\s+([,.;:])", r"\1", t)  # no space before punctuation
    t = re.sub(r",\s*,", ",", t).strip(" ,")
    if len(t) > limit:
        t = t[:limit].rsplit(" ", 1)[0].rstrip(" ,") + "…"
    return t


def _top_headline_for_place(state: WindowState, place: str) -> dict | None:
    """The most-carried recent headline geolocated to `place` — gives a surge
    alert something to point at ("here's an example of what's driving it")
    instead of just a bare place+count. Not a claim that this ONE story
    explains the whole surge (many stories usually contribute); it's a
    representative example, phrased that way in the spoken text."""
    candidates = [h for h in state.news_snapshot() if h.get("place") == place]
    if not candidates:
        return None
    candidates.sort(key=lambda h: h.get("dupe_count") or 1, reverse=True)
    return candidates[0]


def _sqlite_ts_to_utc(ts: str | None) -> datetime | None:
    """SQLite datetime('now') is 'YYYY-MM-DD HH:MM:SS' in UTC with no zone
    marker; ISO strings with offsets also pass through here. None on any
    parse failure."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace(" ", "T"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


_CONVERGENCE_KIND_PHRASE = {
    "world": "a major world event",
    "news": "wide news coverage",
    "surge": "a coverage spike",
    "watchlist": "your professional beat",
}


def _geo_cell(lat: float, lon: float, cell_deg: float = 1.0) -> tuple[float, float]:
    """Round to the nearest cell_deg-degree grid point -- the ~1-degree
    "same place" bucket convergence detection bins signals into."""
    return (round(lat / cell_deg) * cell_deg, round(lon / cell_deg) * cell_deg)


def compute_convergence_alerts(
    world_rows,
    news_rows: list[dict],
    surges: list[dict],
    now: datetime,
    curation_min_score: int = 8,
    cell_deg: float = 1.0,
    min_kinds: int = 3,
) -> list[dict]:
    """The deepest intel signal ( / DISPATCH_INTEL_
    CENTER_DESIGN.md): when 3+ INDEPENDENT channels -- a structured world-feed
    event, general news volume, an anomalous coverage spike, and/or the user's
    own curated professional-beat judgment -- all point at the SAME ~1-degree
    patch of the map within the last 24h, that's a much stronger signal than
    any one alone. Bins each channel's geolocated items into cells and flags
    any cell with `min_kinds`+ distinct kinds present.

    `world_rows`: sqlite Row objects from the SAME 24h world-events query
    build_alerts() already runs (needs title/lat/lon). `news_rows`: dicts from
    a news_articles query over the same 24h window (title/place/lat/lon/
    beat_score) -- yields BOTH the "news" signal (any non-noise geolocated
    story) and the "watchlist" signal (beat_score at/above the curation bar)
    from one pass, since they're two properties of the same underlying data,
    not double-counted as more than 2 kinds. `surges`:
    surge.detect_place_surges()'s current output (place/lat/lon/count) --
    reuse the SAME call build_alerts() already makes for its own surge
    alerts, don't compute it twice.
    """
    cells: dict[tuple, dict] = {}

    def _bucket(lat, lon):
        if lat is None or lon is None:
            return None
        cell = _geo_cell(lat, lon, cell_deg)
        return cells.setdefault(
            cell, {"kinds": set(), "place": None, "lat": lat, "lon": lon}
        )

    for r in world_rows:
        b = _bucket(r["lat"], r["lon"])
        if b is not None:
            b["kinds"].add("world")

    for h in news_rows:
        if stoplist.is_noise(h.get("title") or ""):
            continue
        b = _bucket(h.get("lat"), h.get("lon"))
        if b is None:
            continue
        b["kinds"].add("news")
        if not b["place"] and h.get("place"):
            b["place"] = h["place"]
        if (h.get("beat_score") or 0) >= curation_min_score:
            b["kinds"].add("watchlist")

    for s in surges:
        b = _bucket(s.get("lat"), s.get("lon"))
        if b is None:
            continue
        b["kinds"].add("surge")
        if not b["place"] and s.get("place"):
            b["place"] = s["place"]

    out: list[dict] = []
    for cell, bucket in cells.items():
        kinds = bucket["kinds"]
        if len(kinds) < min_kinds:
            continue
        place = bucket["place"] or f"{cell[0]:.0f}°, {cell[1]:.0f}°"
        kind_names = sorted(kinds)
        phrases = [_CONVERGENCE_KIND_PHRASE.get(k, k) for k in kind_names]
        title = f"Convergence: {place} — " + " + ".join(phrases)
        speak = (
            f"Convergence alert. Multiple signals are converging on {place}: "
            + ", ".join(phrases)
            + "."
        )
        out.append(
            {
                # Hourly-rotating id, same convention as surge -- an ongoing
                # convergence re-announces at most once an hour, not every poll.
                "id": _alert_id("convergence", f"{cell}|{now:%Y-%m-%dT%H}"),
                "kind": "convergence",
                "first_seen": now.isoformat(),
                "title": title,
                "place": place,
                "kinds": kind_names,
                "lat": bucket["lat"],
                "lon": bucket["lon"],
                "speak": speak,
                # Rare and significant enough to always earn a word, same as
                # world/watchlist -- unless the place name itself would choke
                # the voice (non-Latin script).
                "speak_worthy": _is_speakable(place),
            }
        )
    return out


def build_alerts(
    state: WindowState,
    min_severity: float = 6.0,
    min_sources: int = 3,
    fresh_minutes: float = 30,
    surge_min_stories: int = 3,
    surge_z: float = 3.0,
    speak_min_sources: int = 8,
    speak_surge_min_stories: int = 6,
    curation_min_score: int = 8,
    convergence_min_kinds: int = 3,
    limit: int = 20,
    since: str | None = None,
) -> dict:
    """GET /api/alerts payload. `since` (ISO8601) filters to alerts whose
    first_seen is strictly after it — a consumer passes its last poll time and
    only ever sees what's new to it."""
    now = datetime.now(timezone.utc)
    since_dt = _sqlite_ts_to_utc(since)
    alerts: list[dict] = []

    con = db.connect()
    try:
        rows = con.execute(
            "SELECT source_name, title, url, first_seen, severity, lat, lon "
            "FROM items WHERE source_type = 'world' AND severity >= ? "
            "AND first_seen >= datetime('now', '-1 day') "
            "ORDER BY first_seen DESC",
            (min_severity,),
        ).fetchall()
        surge_rows = [
            {
                "place": r["place"],
                "first_seen": _sqlite_ts_to_utc(r["first_seen"]),
                "lat": r["lat"],
                "lon": r["lon"],
            }
            for r in con.execute(
                "SELECT place, title, first_seen, lat, lon FROM news_articles "
                "WHERE place IS NOT NULL"
            ).fetchall()
            # Sports/entertainment stories don't count toward a surge — a World
            # Cup night otherwise fires "Coverage surge: England".
            if not stoplist.is_noise(r["title"])
        ]
        # Convergence detection's own 24h news pool: needs beat_score (which
        # surge_rows above doesn't select) to derive the "watchlist" signal
        # alongside "news" from one pass. Noise-filtered in
        # compute_convergence_alerts, not here (mirrors surge_rows' pattern
        # of filtering after the fetch).
        convergence_news_rows = [
            dict(r)
            for r in con.execute(
                "SELECT title, place, lat, lon, beat_score FROM news_articles "
                "WHERE place IS NOT NULL AND first_seen >= datetime('now', '-1 day')"
            ).fetchall()
        ]
    finally:
        con.close()
    for r in rows:
        first_seen = _sqlite_ts_to_utc(r["first_seen"])
        if since_dt and (first_seen is None or first_seen <= since_dt):
            continue
        title = _strip_kind_prefix(r["title"])
        alerts.append(
            {
                # Key on the event URL (stable), NOT the title — otherwise every
                # USGS magnitude revision of the same quake re-fires the alert
                # (and the speaker re-announces it).
                "id": _alert_id(
                    "world", r["url"] or f"{r['source_name']}|{r['title']}"
                ),
                "kind": "world",
                "first_seen": first_seen.isoformat() if first_seen else None,
                "title": title,
                "severity": r["severity"],
                "source_name": r["source_name"],
                "lat": r["lat"],
                "lon": r["lon"],
                "speak": f"Heads up. {_speechify(title)}.",
                # The VOICE is rarer than the board: major world events always
                # qualify (they're already floored at alerts_min_severity) --
                # unless the title would be unreadable aloud (non-Latin script).
                "speak_worthy": _is_speakable(title),
            }
        )

    cutoff = now - timedelta(minutes=fresh_minutes)
    news_alerted_urls: set[str] = set()
    for h in state.news_snapshot():
        if (h.get("dupe_count") or 0) < min_sources:
            continue
        first_seen = _sqlite_ts_to_utc(h.get("first_seen"))
        if first_seen is None or first_seen < cutoff:
            continue
        if since_dt and first_seen <= since_dt:
            continue
        n = h["dupe_count"]
        news_alerted_urls.add(h.get("url") or "")
        alerts.append(
            {
                "id": _alert_id("news", h.get("url") or h.get("title") or ""),
                "kind": "news",
                "first_seen": first_seen.isoformat(),
                "title": h.get("title"),
                "sources": h.get("sources") or [],
                "dupe_count": n,
                "place": h.get("place"),
                "speak": f"{n} outlets are reporting. {_speechify(h.get('title'))}.",
                # Board shows every multi-source cluster; the voice only speaks
                # the ones a lot of outlets are carrying, in a script it can
                # actually pronounce.
                "speak_worthy": n >= speak_min_sources
                and _is_speakable(h.get("title")),
            }
        )

    # Watchlist alerts (curation, pillar 3): a FRESH story the local model
    # scored squarely on the user's beat. A story already alerted as a
    # multi-source cluster isn't announced twice.
    for h in state.news_snapshot():
        if not h.get("beat"):
            continue
        if (h.get("url") or "") in news_alerted_urls:
            continue
        first_seen = _sqlite_ts_to_utc(h.get("first_seen"))
        if first_seen is None or first_seen < cutoff:
            continue
        if since_dt and first_seen <= since_dt:
            continue
        alerts.append(
            {
                "id": _alert_id("watchlist", h.get("url") or h.get("title") or ""),
                "kind": "watchlist",
                "first_seen": first_seen.isoformat(),
                "title": h.get("title"),
                "beat_score": h.get("beat_score"),
                "place": h.get("place"),
                "source_name": h.get("source_name"),
                "speak": f"On your watchlist. {_speechify(h.get('title'))}.",
                # Your own beat always earns a word -- unless it's in a script
                # the voice would just mangle.
                "speak_worthy": _is_speakable(h.get("title")),
            }
        )

    # Surge alerts: id rotates hourly so an ongoing surge re-announces at most
    # once an hour; first_seen is "now" (the moment the surge was measured),
    # which also makes `since` behave naturally for pollers. Computed once and
    # reused below for convergence detection (don't run the z-score pass twice).
    surges = surge.detect_place_surges(
        surge_rows, now, min_stories=surge_min_stories, z_threshold=surge_z
    )
    for s in surges:
        if since_dt and now <= since_dt:
            continue
        # A surge is a place+count signal with no single story behind it (many
        # articles usually contribute) -- but "7 stories on Canada" with no
        # hint of WHAT was the original complaint (heard it, no idea what the
        # report was). Attach a representative headline so there's something
        # to actually say.
        top = _top_headline_for_place(state, s["place"])
        title = f"Coverage surge: {s['place']} — {s['count']} stories in the last hour"
        speak = f"Coverage is spiking on {s['place']}. {s['count']} stories in the last hour."
        if top and top.get("title"):
            title += f' — top story: "{top["title"]}"'
            # The board can show any script fine; the spoken clause only adds
            # the top story if the voice can actually pronounce it.
            if _is_speakable(top["title"]):
                speak += f" Top story: {_speechify(top['title'])}."
        alerts.append(
            {
                "id": _alert_id("surge", f"{s['place']}|{now:%Y-%m-%dT%H}"),
                "kind": "surge",
                "first_seen": now.isoformat(),
                "title": title,
                "place": s["place"],
                "count": s["count"],
                "z": s["z"],
                "lat": s["lat"],
                "lon": s["lon"],
                "top_story": top.get("title") if top else None,
                "top_story_url": top.get("url") if top else None,
                "speak": speak,
                "speak_worthy": s["count"] >= speak_surge_min_stories,
            }
        )

    # Convergence: the deepest signal, computed from what we've ALREADY
    # gathered above (the 24h world rows, the 24h news pool, this cycle's
    # surges) -- no new data source, just a cross-cutting view of it.
    if not (since_dt and now <= since_dt):
        alerts.extend(
            compute_convergence_alerts(
                rows,
                convergence_news_rows,
                surges,
                now,
                curation_min_score=curation_min_score,
                min_kinds=convergence_min_kinds,
            )
        )

    alerts.sort(key=lambda a: a.get("first_seen") or "", reverse=True)
    return {
        "contract": ALERTS_CONTRACT,
        "generated_at": now.isoformat(),
        "alerts": alerts[:limit],
    }


def current_flights(state: WindowState) -> list[dict]:
    """GET /api/flights payload — the cached live aircraft board."""
    return state.flights_snapshot()


def _parse_iso(ts: str | None) -> datetime | None:
    """Best-effort ISO8601 parse for a firehose item's published_at. Returns
    None for missing/empty/unparseable timestamps rather than raising — v3's
    2-hour filter treats those as excluded (see recent_news)."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def recent_news(
    state: WindowState, limit: int = 100, window_hours: float | None = None
) -> list[dict]:
    """GET /api/news payload (v2 base + v3 filter) — cached headlines,
    newest-first (the cache itself is already sorted by NewsLoop).

    v3, "Last-2-hours filter"):
    when `window_hours` is given (and > 0), only items whose published_at
    falls within the last `window_hours` are returned; items with no
    parseable timestamp are excluded outright — a live firehose only shows
    timestamped-recent items. `window_hours=None`/`0` skips the filter
    entirely (keeps the raw cache), for callers that want everything."""
    headlines = state.news_snapshot()
    if window_hours:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
        filtered = []
        for h in headlines:
            published = _parse_iso(h.get("published_at"))
            if published is not None and published >= cutoff:
                filtered.append(h)
        headlines = filtered
    return headlines[:limit]


def read_news_script(
    state: WindowState, beat_limit: int = 4, world_limit: int = 8, total_cap: int = 8
) -> list[str]:
    """Spoken lines for the board's on-demand "read the news" button (a small
    voice orb the user clicks at their desk) — a short round-up leading with
    anything fresh on his beat, then top world headlines, capped so the whole
    read stays ~45-75s (a catch-up, not a full newscast). Reuses the SAME
    news_digest() pool get_news/get_defense_news read from (CE6, "one news
    brain") -- one source, several ways to hear it. Empty when there's
    genuinely nothing to say (quiet news day) -- caller shows/plays nothing
    rather than a canned line with no content behind it."""
    beat = news_digest(state, kind="beat", limit=beat_limit)
    world = news_digest(state, kind="world", limit=world_limit)
    beat_urls = {b.get("url") for b in beat if b.get("url")}
    world = [w for w in world if w.get("url") not in beat_urls]  # no repeats
    combined = (beat + world)[:total_cap]
    if not combined:
        return []

    lines = ["Here's the news, sir."]
    for item in combined:
        title = _speechify(item.get("title"))
        if not title:
            continue
        n = item.get("dupe_count") or 1
        prefix = "On your beat: " if item.get("beat") else ""
        tag = f" — {n} outlets" if n > 1 else ""
        lines.append(f"{prefix}{title}{tag}.")
    return lines


def news_digest(
    state: WindowState, kind: str = "world", limit: int = 6, window_hours: float = 6.0
) -> list[dict]:
    """GET /api/news/digest payload — a small, pre-ranked round-up for ON-DEMAND
    voice consumers (e.g. an external agent's get_news tool). "One news brain":
    this reads the SAME curated pool the board's map/ticker use, so nothing stops
    re-fetching its own separate, uncurated RSS pull for the same request.

    kind="world" -> general round-up, any topic: most-carried (dupe_count) and
        freshest first, noise (stoplist sports/entertainment) excluded.
    kind="beat"  -> the user's professional beat ONLY (config/profile.yaml, scored
        by the curation pass — defense/IC/GovCon/AI-ML/PQC/M&A, a superset of
        a static "defense" feed list), highest-scored first. Empty when
        curation hasn't run yet or found nothing on-beat in the window — never
        fabricated; an honest "nothing new" beats a stale or invented answer.

    Everything returned here is meant to be SPOKEN (unlike /api/news, which
    also feeds the board's visual ticker) -- headlines in a non-Latin script
    (Arabic/Farsi/Hebrew/Cyrillic/CJK/etc.) are excluded entirely, since the
    English-only voice mangles them (the user, 2026-07-21: "the system really
    choked on that").
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    pool = []
    for h in state.news_snapshot():
        if h.get("noise"):
            continue
        if not _is_speakable(h.get("title")):
            continue
        published = _parse_iso(h.get("published_at"))
        if published is not None and published < cutoff:
            continue
        pool.append(h)

    if kind == "beat":
        pool = [h for h in pool if h.get("beat")]
        pool.sort(key=lambda h: (h.get("beat_score") or 0), reverse=True)
    else:
        pool.sort(
            key=lambda h: (h.get("dupe_count") or 1, h.get("published_at") or ""),
            reverse=True,
        )

    return [
        {
            "title": h.get("title"),
            "source_name": h.get("source_name"),
            "sources": h.get("sources") or [h.get("source_name")],
            "dupe_count": h.get("dupe_count") or 1,
            "place": h.get("place"),
            "beat": bool(h.get("beat")),
            "beat_score": h.get("beat_score"),
            "url": h.get("url"),
        }
        for h in pool[:limit]
    ]


def news_search(query: str, limit: int = 5) -> list[dict]:
    """GET /api/news/search payload — "read me the top news on <query>"
    (the user, 2026-07-28: "just a top news, reader"). LIVE on-demand Google
    News RSS search (free, no key — brief/ingest/googlenews.py, the same
    beat-feed adapter, called directly rather than through the persisted
    news-loop cache), NOT the curated board pool news_digest() reads —
    an arbitrary query is never going to already be sitting in that cache.
    Meant to be SPOKEN, same rule as news_digest: non-Latin-script titles
    excluded (the English-only voice mangles them), plus the same sports/
    entertainment noise filter. Empty query -> [] without a network call."""
    query = (query or "").strip()
    if not query:
        return []
    # Over-fetch a bit so filtering still leaves `limit` results most of the
    # time, without making this an expensive call (one Google News RSS
    # request either way — see googlenews._fetch_keyword).
    items = googlenews.fetch(keywords=[query], count_per_keyword=limit * 3, include_feeds=False)
    headlines = _headline_dicts(items)
    out = []
    for h in headlines:
        if h.get("noise") or not _is_speakable(h.get("title")):
            continue
        out.append(
            {
                "title": h.get("title"),
                "source_name": h.get("source_name"),
                "url": h.get("url"),
                "published_at": h.get("published_at"),
            }
        )
        if len(out) >= limit:
            break
    return out
