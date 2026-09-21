"""FastAPI app for the Open Window dashboard (v1 deltas/map + v2 current-state
board and news panel).

CORRECTED 2026-09-21 (architecture review #1657 item 2). This docstring used to claim: "LLM-free
live path: this module and everything it imports (brief.window.*, brief.ingest.world,
brief.ingest.rss, brief.db) never touch Ollama or brief/generate.py". That is false and
has been since curation landed — this module imports .service, which imports .curate,
which POSTs headlines to a local Ollama model (curate.py:44,158) when
config/window.yaml `curation_enabled` is true. It is true today.

What the iron rule actually protects, and what still holds:
  * no CLOUD model is ever called on the live path — curation is local Ollama only;
  * RENDERING takes no model: the board draws from the DB, so a dead Ollama
    degrades scoring, it does not blank the wall;
  * renders are sacred — curation is skipped entirely while ComfyUI is working
    (curate.comfyui_busy), because the box is the operator's.
See WORLD_DELTA_BUILD_PLAN.md's "Iron rule" section, read with this correction.
"""

from __future__ import annotations

import re
import threading
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.staticfiles import StaticFiles

from .. import applog, version
from . import dedup, service

log = applog.get(__name__)

_TEMPLATE_PATH = Path(__file__).parent / "templates" / "dashboard.html"
_KEYWORDS_TEMPLATE_PATH = Path(__file__).parent / "templates" / "keywords.html"
_STATIC_DIR = Path(__file__).parent / "static"

# Cache-busting for the numbered JS modules: StaticFiles sends Last-Modified
# but no Cache-Control, so absent an explicit signal browsers fall back to
# HEURISTIC freshness and can keep serving a pre-deploy script for a while
# after a real edit landed (bit us verifying the map-zoom fix live,
# 2026-07-23 -- a fresh tab still ran old code). Stamping each script tag's
# URL with that file's own mtime means an edited file is a genuinely NEW URL
# next load, so there's nothing to cache stale in the first place.
_JS_SRC_RE = re.compile(r'src="/static/js/([\w.-]+\.js)"')


def _cache_bust_js(html: str) -> str:
    def repl(m: re.Match) -> str:
        fname = m.group(1)
        try:
            mtime = int((_STATIC_DIR / "js" / fname).stat().st_mtime)
        except OSError:
            return m.group(0)
        return f'src="/static/js/{fname}?v={mtime}"'

    return _JS_SRC_RE.sub(repl, html)


# Held for the duration of one read-news speak (see _speak_news_lines /
# api_voice_read_news below). Real incident, 2026-08-05: repeated clicks
# while a previous read was still talking spawned a NEW competing thread
# each time -- multiple concurrent Kokoro (onnxruntime) inference sessions
# fighting for the same CPU cores turned what should be a few-second read
# into one that took two full minutes. Blocking-false acquire means a
# second click while one is in flight is refused outright, never queued or
# stacked on top.
_speak_lock = threading.Lock()


def _speak_news_lines(
    lines: list[str], engine: str, voice: str, lang: str, speed: float
) -> None:
    """Runs OFF the request thread (spawned by the read_news endpoint below) --
    speaks each line of an on-demand news read via kokoro_tts.speak_or_say, in
    order. A module-level function (not a closure) so it's independently
    testable and so the background thread has a real, inspectable target.
    Releases _speak_lock (acquired by the caller before spawning this thread)
    when done, success or not.

    Leads with an instant `say`-engine acknowledgement (forced, regardless of
    the configured engine) before the actual digest -- the operator: "I would even
    be ok with an instant 'sir let me curate and fetch it' so I knew it was
    received." A cold Kokoro model load can take real time; macOS `say` has
    none, so the click never sits in silence wondering if it registered."""
    from . import kokoro_tts

    try:
        kokoro_tts.speak_or_say("One moment, sir -- pulling up the news.", engine="say")
        for line in lines:
            kokoro_tts.speak_or_say(
                line, engine=engine, voice=voice, lang=lang, speed=speed
            )
    finally:
        _speak_lock.release()


def create_app(
    world_feeds: list[dict], window_cfg: dict, news_sources: list[dict] | None = None
) -> FastAPI:
    """Build the FastAPI app. Kept as a factory (not a module-level `app`)
    so tests can construct one against an isolated feed list/config without
    import-time side effects.

    `news_sources` is additive (v2): the RSS roster (config/sources.yaml) for
    the headlines panel. Defaults to an empty list so existing callers/tests
    that only pass world_feeds/window_cfg keep working — the news loop just
    becomes a no-op (nothing to fetch)."""
    interval = int(window_cfg.get("sweep_interval_seconds", 900))
    default_limit = int(window_cfg.get("recent_deltas_limit", 200))
    news_interval = int(window_cfg.get("news_interval_seconds", 600))
    news_limit = int(window_cfg.get("recent_news_limit", 100))
    current_limit = int(window_cfg.get("current_records_limit", 200))
    news_window_hours = float(window_cfg.get("news_window_hours", 2))
    news_digest_window_hours = float(window_cfg.get("news_digest_window_hours", 6))
    alerts_min_severity = float(window_cfg.get("alerts_min_severity", 6.0))
    alerts_min_sources = int(window_cfg.get("alerts_min_sources", 3))
    alerts_fresh_minutes = float(window_cfg.get("alerts_fresh_minutes", 30))
    surge_min_stories = int(window_cfg.get("surge_min_stories", 3))
    surge_z = float(window_cfg.get("surge_z", 3.0))
    speak_min_sources = int(window_cfg.get("speak_min_sources", 8))
    speak_surge_min_stories = int(window_cfg.get("speak_surge_min_stories", 6))
    quiet_start = int(window_cfg.get("speak_quiet_start_hour", 23))
    quiet_end = int(window_cfg.get("speak_quiet_end_hour", 8))
    weekdays_only = bool(window_cfg.get("speak_weekdays_only", False))
    speak_check_calendar = bool(window_cfg.get("speak_check_calendar", True))
    speak_engine = str(window_cfg.get("speak_engine", "kokoro"))
    speak_kokoro_voice = str(window_cfg.get("speak_kokoro_voice", "bm_george"))
    speak_kokoro_lang = str(window_cfg.get("speak_kokoro_lang", "en-gb"))
    speak_kokoro_speed = float(window_cfg.get("speak_kokoro_speed", 1.0))
    curation_enabled = bool(window_cfg.get("curation_enabled", False))
    # The default RESOLVES a role (the host monitor's `chat.small`) rather than
    # naming a tag. It used to be the bare literal "qwen3.5:9b" in both places
    # -- here and window.yaml -- so when that model was renamed with the -er16k
    # context suffix, every curation call 404'd, curate() swallowed it by
    # design, and the beat digest returned [] forever while the board still
    # looked healthy. A role survives a rename; a literal does not.
    curation_model = str(
        window_cfg.get("curation_model") or service.curate.DEFAULT_MODEL
    )
    curation_min_score = int(window_cfg.get("curation_min_score", 8))
    curation_quiet_start_hour = int(
        window_cfg.get(
            "curation_quiet_start_hour", service.curate.DEFAULT_QUIET_START_HOUR
        )
    )
    curation_quiet_end_hour = int(
        window_cfg.get("curation_quiet_end_hour", service.curate.DEFAULT_QUIET_END_HOUR)
    )
    comfyui_url = str(window_cfg.get("comfyui_url", "http://localhost:8000"))
    flights_lat = window_cfg.get("flights_lat")
    flights_lon = window_cfg.get("flights_lon")
    flights_radius_nm = float(window_cfg.get("flights_radius_nm", 30))
    flights_limit = int(window_cfg.get("flights_limit", 12))
    flights_interval = int(window_cfg.get("flights_interval_seconds", 25))
    dedup_enabled = bool(window_cfg.get("dedup_enabled", True))
    dedup_similarity = float(
        window_cfg.get("dedup_similarity", dedup.DEFAULT_SIMILARITY_THRESHOLD)
    )
    gdelt_enabled = bool(window_cfg.get("gdelt_enabled", False))
    gdelt_query = window_cfg.get("gdelt_query") or service.gdelt.DEFAULT_QUERY
    gdelt_timespan = str(window_cfg.get("gdelt_timespan", "1h"))
    gdelt_max_records = int(window_cfg.get("gdelt_max_records", 75))
    # NewsAPI.ai: on only when both configured AND a key is present in the env.
    newsapi_enabled = bool(window_cfg.get("newsapi_enabled", False)) and bool(
        service.newsapi.api_key()
    )
    newsapi_keywords = window_cfg.get("newsapi_keywords") or None
    newsapi_count = int(window_cfg.get("newsapi_count", 40))
    newsapi_every_n_cycles = int(window_cfg.get("newsapi_every_n_cycles", 1))
    googlenews_enabled = bool(window_cfg.get("googlenews_enabled", False))
    googlenews_count_per_keyword = int(
        window_cfg.get("googlenews_count_per_keyword", 8)
    )
    v1_feeds = service.select_v1_feeds(world_feeds)
    news_sources = news_sources or []

    state = service.WindowState(sweep_interval_seconds=interval)
    loop = service.SweepLoop(v1_feeds, state, interval)
    news_loop = service.NewsLoop(
        news_sources,
        state,
        news_interval,
        limit=news_limit,
        dedup_enabled=dedup_enabled,
        dedup_similarity=dedup_similarity,
        curation_enabled=curation_enabled,
        curation_model=curation_model,
        curation_min_score=curation_min_score,
        curation_quiet_start_hour=curation_quiet_start_hour,
        curation_quiet_end_hour=curation_quiet_end_hour,
        comfyui_url=comfyui_url,
        gdelt_enabled=gdelt_enabled,
        gdelt_query=gdelt_query,
        gdelt_timespan=gdelt_timespan,
        gdelt_max_records=gdelt_max_records,
        newsapi_enabled=newsapi_enabled,
        newsapi_keywords=newsapi_keywords,
        newsapi_count=newsapi_count,
        newsapi_every_n_cycles=newsapi_every_n_cycles,
        googlenews_enabled=googlenews_enabled,
        googlenews_count_per_keyword=googlenews_count_per_keyword,
    )

    flight_loop = service.FlightLoop(
        state,
        lat=None if flights_lat is None else float(flights_lat),
        lon=None if flights_lon is None else float(flights_lon),
        radius_nm=flights_radius_nm,
        limit=flights_limit,
        interval_seconds=flights_interval,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        loop.start()
        news_loop.start()
        flight_loop.start()
        yield
        loop.stop()
        news_loop.stop()
        flight_loop.stop()

    app = FastAPI(title="The Dispatch", lifespan=lifespan)
    # v3 — serves brief/window/static/ne_110m_land.json (bundled Natural Earth
    # land polygons) at /static/ne_110m_land.json. No new dep: starlette ships
    # with FastAPI. The dashboard fetches this ONCE on page load, never on the
    # 10s poll (see WORLD_DELTA_BUILD_PLAN.md's "v3" section, V3.1).
    if _STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    @app.middleware("http")
    async def _no_heuristic_static_cache(request, call_next):
        # StaticFiles sends Last-Modified but no Cache-Control, so browsers
        # fall back to HEURISTIC freshness and can keep serving a pre-deploy
        # JS/CSS file for a while after a real edit landed -- bit us verifying
        # the map-zoom fix live (2026-07-23). no-cache forces a conditional
        # GET (If-Modified-Since) on every load -- a cheap 304 when nothing
        # changed, but a guaranteed-fresh 200 the moment it has.
        response = await call_next(request)
        if request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    app.state.window_state = state
    app.state.sweep_loop = loop
    app.state.news_loop = news_loop
    app.state.v1_feeds = v1_feeds
    app.state.news_sources = news_sources
    app.state.sweep_interval_seconds = interval

    _loops = {"sweep": loop, "news": news_loop, "flights": flight_loop}

    @app.get("/api/health")
    def api_health():
        ok, detail = service.check_health(loop, news_loop, flight_loop)
        # W39 shape (architecture review ): which source this process actually
        # loaded, and whether disk has moved since. Deliberately not folded
        # into the ok/503 status -- a stale-but-running process is still
        # answering, same reasoning as a sibling project's own health endpoint.
        body = {"ok": ok, **detail, **version.source_state()}
        return JSONResponse(body, status_code=200 if ok else 503)

    @app.get("/api/status")
    def api_status():
        return service.build_status(state, interval, loops=_loops)

    @app.get("/api/deltas")
    def api_deltas(limit: int = default_limit):
        return service.recent_deltas(limit=limit)

    @app.get("/api/current")
    def api_current(limit: int = current_limit):
        return service.current_state(state, limit=limit)

    @app.get("/api/feed_health")
    def api_feed_health():
        """Small per-source freshness chips (fresh/stale/very_stale/error) --
        distinct from the full-board STALE overlay. See
        service.compute_feed_health."""
        return {"feeds": service.compute_feed_health(state)}

    @app.get("/api/news")
    def api_news(limit: int = news_limit):
        return service.recent_news(state, limit=limit, window_hours=news_window_hours)

    @app.get("/api/news/digest")
    def api_news_digest(kind: str = "world", limit: int = 6):
        """A small, pre-ranked round-up for ON-DEMAND voice consumers (project-
        jarvis's get_news/get_defense_news) — "one news brain": the same
        curated pool the board uses, ranked server-side so Jarvis stops running
        its own separate, uncurated RSS pull for the same request. kind=world
        (general) or kind=beat (the operator's professional beat via curation)."""
        return {
            "kind": kind,
            "items": service.news_digest(
                state, kind=kind, limit=limit, window_hours=news_digest_window_hours
            ),
        }

    @app.get("/api/news/search")
    def api_news_search(q: str = "", limit: int = 5):
        """ "Read me the top news on <query>" — LIVE on-demand search (the operator,
        2026-07-28: "just a top news, reader"), distinct from /api/news/digest's
        pre-curated pool. See service.news_search for why this can't reuse
        that cache (an arbitrary query is never already sitting in it)."""
        return {"query": q, "items": service.news_search(q, limit=limit)}

    @app.get("/api/flights")
    def api_flights():
        """Live "Skies" board — cached aircraft near the configured point
        (adsb.fi, no auth, LLM-free). Carries center/radius so the client can
        draw the local map; aircraft empty when flights_lat/lon aren't set."""
        return {
            "center": (
                None
                if flights_lat is None
                else [float(flights_lat), float(flights_lon)]
            ),
            "radius_nm": flights_radius_nm,
            "aircraft": service.current_flights(state),
        }

    @app.get("/api/alerts")
    def api_alerts(since: str | None = None):
        """dispatch-alerts-v1 — the live 'really important things' feed for
        voice consumers (Jarvis). Deterministic and LLM-free; see
        service.build_alerts for the significance rules."""
        return service.build_alerts(
            state,
            min_severity=alerts_min_severity,
            min_sources=alerts_min_sources,
            fresh_minutes=alerts_fresh_minutes,
            surge_min_stories=surge_min_stories,
            surge_z=surge_z,
            speak_min_sources=speak_min_sources,
            speak_surge_min_stories=speak_surge_min_stories,
            curation_min_score=curation_min_score,
            since=since,
        )

    @app.get("/api/voice")
    def api_voice():
        """Is the voice live right now, and if not, why? Drives the board's
        Jarvis orb and mute button, so the wall never implies it's listening
        when it's muted. `muted` is the MANUAL switch specifically (the button
        toggles that); `reason` covers every cause including meetings."""
        from . import quiet as quiet_mod

        mute_path = service.db.DATA_DIR / "speaker_mute"
        reason = quiet_mod.quiet_reason(
            datetime.now(),
            mute_path,
            quiet_start,
            quiet_end,
            weekdays_only=weekdays_only,
            respect_calendar=speak_check_calendar,
        )
        return {
            "can_speak": reason is None,
            "reason": reason,
            "muted": quiet_mod.manual_mute(mute_path),
        }

    @app.post("/api/voice/mute")
    def api_voice_mute(on: bool, request: Request):
        """The board's mute button. Creates/removes the same data/speaker_mute
        file the speaker daemon honours, so the button and `touch`/`rm` are the
        one switch. Explicit on=true/false (not a toggle) so a double-click
        can't race itself.

        SEC-2 (2026-09-21): every flip is logged with its ORIGIN, at WARNING.

        The wild specimen: on 2026-09-21 the mute came off between 01:01 and
        09:44 and no log anywhere could say what moved it. The owner confirmed
        it was him, so nothing was wrong -- but a switch guarded by a standing
        absolute rule changed state and the record could not answer. That is the
        defect, and it is a record defect, not an auth one. Filed separately
        from the token gate (SEC-1) on purpose: the gate changes a surface he
        uses from his phone and needs his word first, while this half needs
        nobody's permission and is the half that actually answers the question.

        Logged even when the state does not change, because "something pushed
        mute=off while it was already off" is exactly the trace that
        distinguishes a stuck client from a person. Never logs a token, a cookie
        or a header value -- peer address and user-agent only.
        """
        mute_path = service.db.DATA_DIR / "speaker_mute"
        was = mute_path.exists()
        client = request.client.host if request.client else "unknown"
        agent = (request.headers.get("user-agent") or "unknown")[:120]
        try:
            if on:
                mute_path.parent.mkdir(parents=True, exist_ok=True)
                mute_path.touch()
            else:
                mute_path.unlink(missing_ok=True)
        except OSError as exc:
            log.warning(
                "VOICE MUTE flip FAILED: %s -> %s from %s (%s): %r",
                "on" if was else "off",
                "on" if on else "off",
                client,
                agent,
                exc,
            )
            return JSONResponse({"error": repr(exc)}, status_code=500)
        now = mute_path.exists()
        log.warning(
            "VOICE MUTE %s: %s -> %s from %s (%s)",
            "changed" if now != was else "re-asserted",
            "on" if was else "off",
            "on" if now else "off",
            client,
            agent,
        )
        return {"muted": now}

    @app.post("/api/voice/read_news")
    def api_voice_read_news():
        """The board's on-demand "read the news" button (the operator: a small
        Jarvis orb he clicks at his desk) -- speaks a short round-up in the
        Jarvis voice RIGHT NOW instead of waiting for the next scheduled
        bulletin.

        Respects mute / mic-in-use / Focus / quiet hours / weekdays -- an
        earlier version let this override manual mute ("he clicked, he means
        it") and that was wrong: the operator hit mute mid-call and it spoke anyway.
        Mute and mic-in-use are direct signals he's actually unavailable
        RIGHT NOW; there's no overriding those, full stop.

        The ONE gate this explicitly does NOT check: the calendar-meeting
        signal (the operator, 2026-07-21: "I'm OK with a calendar hold, but if I
        click the button manually.. I'd like it to read the news"). Unlike
        mute/mic-in-use, a calendar hold is only a PREDICTION he's busy, not
        evidence he actually is -- an explicit click is stronger, more direct
        evidence than a calendar guess, so it wins. The scheduled bulletin
        still respects the calendar signal (respect_calendar=speak_check_
        calendar) -- only this manual trigger skips it.

        Speaking runs in a background thread so this request returns
        instantly; the board's 10s poll must never stall for the ~60s of
        audio playback."""
        from . import quiet as quiet_mod

        mute_path = service.db.DATA_DIR / "speaker_mute"
        reason = quiet_mod.quiet_reason(
            datetime.now(),
            mute_path,
            quiet_start,
            quiet_end,
            weekdays_only=weekdays_only,
            respect_calendar=False,
        )
        if reason:
            return {"ok": False, "reason": reason}

        lines = service.read_news_script(state)
        if not lines:
            return {"ok": False, "reason": "nothing to report right now"}

        if not _speak_lock.acquire(blocking=False):
            return {"ok": False, "reason": "already reading the news"}

        threading.Thread(
            target=_speak_news_lines,
            args=(
                lines,
                speak_engine,
                speak_kokoro_voice,
                speak_kokoro_lang,
                speak_kokoro_speed,
            ),
            daemon=True,
        ).start()
        return {"ok": True, "lines": len(lines)}

    @app.get("/", response_class=HTMLResponse)
    def dashboard():
        return _cache_bust_js(_TEMPLATE_PATH.read_text(encoding="utf-8"))

    # --- Google News RSS keyword management (free NewsAPI.ai replacement) --
    # Not on the wall board itself (that's meant to be glanceable/hands-off);
    # a small standalone page the operator visits occasionally from his own machine
    # to add/remove beat terms. Edits are picked up by the NEXT news-loop
    # cycle with no restart (googlenews.fetch() reads the file fresh).

    @app.get("/api/google_news/keywords")
    def api_google_news_keywords_list():
        return {"keywords": service.googlenews.load_keywords()}

    @app.post("/api/google_news/keywords/add")
    def api_google_news_keywords_add(keyword: str):
        return {"keywords": service.googlenews.add_keyword(keyword)}

    @app.post("/api/google_news/keywords/remove")
    def api_google_news_keywords_remove(keyword: str):
        return {"keywords": service.googlenews.remove_keyword(keyword)}

    @app.get("/api/google_news/feeds")
    def api_google_news_feeds_list():
        return {"feeds": service.googlenews.load_feeds()}

    @app.get("/api/google_news/topics")
    def api_google_news_topics():
        """Preset topic-section names -> tokens, for the feeds page's picker
        -- so the operator never has to hunt down Google's opaque topic tokens."""
        return {"topics": service.googlenews.TOPIC_TOKENS}

    @app.post("/api/google_news/feeds/add")
    def api_google_news_feeds_add(label: str, feed_type: str, value: str = ""):
        return {"feeds": service.googlenews.add_feed(label, feed_type, value)}

    @app.post("/api/google_news/feeds/remove")
    def api_google_news_feeds_remove(feed_type: str, value: str = ""):
        return {"feeds": service.googlenews.remove_feed(feed_type, value)}

    @app.get("/keywords", response_class=HTMLResponse)
    def keywords_page():
        return _KEYWORDS_TEMPLATE_PATH.read_text(encoding="utf-8")

    return app
