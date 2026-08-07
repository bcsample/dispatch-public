"""The Dispatch's voice: poll /api/alerts (dispatch-alerts-v1) and speak the
genuinely important things aloud.

NEVER interrupts a meeting. Before speaking it checks brief.window.quiet:
manual mute file > microphone in use (any call app) > Focus/Do Not Disturb >
quiet hours. Anything suppressed is marked seen rather than queued, so you
never get a monologue when a meeting ends — the board still shows it all.

Reads on a SCHEDULE, not at random: a short bulletin at the top and bottom of
the hour (speak_schedule_minutes). Speak-worthy alerts accumulate between slots
and are delivered as one batch, so the wall is predictable.

The lead-in ("Top of the hour") used to play instantly, then the news paused
for a beat while Kokoro rendered it. maybe_prerender() synthesizes the whole
bulletin ~30s early (PRELOAD_SECONDS) and caches it in kokoro_tts, so at the
slot itself it's cache-hit playback the whole way through — no mid-bulletin
gap. A cache miss (config changed, or one more alert snuck in during that last
30s) just falls back to live synthesis for that one clip, same as before.

Other manners:
- First run says nothing (no backlog replay on restart).
- At most MAX_UTTERANCES per bulletin; the rest collapse into "plus N more".
- A bulletin missed because you're in a meeting is SKIPPED, not stacked onto
  the next one.
- Speaks with Kokoro (`bm_george`, a LOCAL neural model — see
  brief/window/kokoro_tts.py). Falls back to macOS `say` (Daniel) if the
  model is disabled/absent, so the wall is never muted.

Run directly or via launchd (com.local.dispatch-speaker.plist):
    .venv/bin/python scripts/dispatch_speak.py
Silence it any time:   touch data/speaker_mute      (rm to re-enable)
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))  # so `brief.window.quiet` imports when run as a script

from brief.window import kokoro_tts, quiet  # noqa: E402

BASE_URL = os.environ.get("DISPATCH_URL", "http://localhost:8808")
POLL_SECONDS = int(os.environ.get("DISPATCH_SPEAK_INTERVAL", "60"))
# How long before a bulletin slot to synthesize its audio ahead of time, so
# "Top of the hour" flows straight into the news instead of pausing on
# Kokoro's render time. Purely a latency optimization — see maybe_prerender().
PRELOAD_SECONDS = int(os.environ.get("DISPATCH_SPEAK_PRELOAD_SECONDS", "30"))
# Scheduling granularity for the prerender check — fine enough to reliably
# land inside the PRELOAD_SECONDS window every hour without polling alerts
# any more often than POLL_SECONDS.
TICK_SECONDS = 10
# `say` fallback voice — Daniel (en_GB) is a reasonable match for Kokoro's
# default British voice when Kokoro isn't available. Kept as the safety
# net, never the primary.
VOICE = os.environ.get("DISPATCH_VOICE", "Daniel")
RATE = int(os.environ.get("DISPATCH_VOICE_RATE", "172"))  # wpm; ~175 is default
MAX_UTTERANCES = 3
STATE_PATH = ROOT / "data" / "speaker_seen.json"
MUTE_PATH = ROOT / "data" / "speaker_mute"
# An external voice agent (if you run one) touches this while it's up and
# speaking; we defer to it and resume automatically when it goes stale.
# If you don't run one, this file never exists and the check is always False.
VOICE_CLAIM_PATH = ROOT / "data" / "voice_claim_external"
SEEN_CAP = 500


def load_state() -> tuple[list[str], datetime | None, list[dict]]:
    """(seen ids, last bulletin slot handled, pending worthy alerts awaiting the
    next bulletin). Tolerates older file shapes."""
    try:
        d = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        slot = d.get("last_slot")
        return (
            d.get("ids", []),
            (datetime.fromisoformat(slot) if slot else None),
            d.get("pending", []),
        )
    except Exception:
        return [], None, []


def save_state(ids: list[str], last_slot: datetime | None, pending: list[dict]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps(
            {
                "pending": pending[-50:],
                "ids": ids[-SEEN_CAP:],
                "last_slot": last_slot.isoformat() if last_slot else None,
            }
        ),
        encoding="utf-8",
    )


def _schedule() -> tuple[list[int], float]:
    """(minutes past the hour to read at, how long after each slot a read may
    still happen). From window.yaml so the cadence lives with the other knobs."""
    try:
        from brief import config

        cfg = config.load_window_config()
        mins = [int(m) for m in cfg.get("speak_schedule_minutes", [0, 30])]
        window = float(cfg.get("speak_schedule_window_minutes", 2))
        return (sorted(mins) or [0, 30]), window
    except Exception:
        return [0, 30], 2.0


def _quiet_hours() -> tuple[int, int, bool, bool, bool, float]:
    """(quiet-start hour, quiet-end hour, weekdays-only, check-calendar,
    skip-when-display-idle, display-idle-minutes) — the voice is silent
    OUTSIDE [end, start), and all weekend when weekdays_only. Read live from
    window.yaml (one source of truth, shared with /api/voice). Default
    17->9, weekdays-only, calendar-check on = speaks only 09:00-17:00
    Mon-Fri and not during a calendar meeting. Display-idle defaults ON,
    20 min (the user, 2026-08-04: "I don't need to hear odd voices from the
    basement" — only this scheduled path opts in, never the manual button;
    see quiet.quiet_reason's respect_display_idle docstring)."""
    try:
        from brief import config

        cfg = config.load_window_config()
        return (
            int(cfg.get("speak_quiet_start_hour", 17)),
            int(cfg.get("speak_quiet_end_hour", 9)),
            bool(cfg.get("speak_weekdays_only", True)),
            bool(cfg.get("speak_check_calendar", True)),
            bool(cfg.get("speak_skip_when_display_idle", True)),
            float(cfg.get("speak_display_idle_minutes", 20.0)),
        )
    except Exception:
        return 17, 9, True, True, True, 20.0


def current_slot(
    now: datetime, minutes: list[int], window_minutes: float
) -> datetime | None:
    """The bulletin slot `now` falls in (e.g. 10:30 when it's 10:30:45), or
    None if we're between slots. Only the `window_minutes` right after a slot
    count, so a missed slot is skipped rather than read late."""
    for m in minutes:
        slot = now.replace(minute=m, second=0, microsecond=0)
        if slot <= now < slot + timedelta(minutes=window_minutes):
            return slot
    return None


def _lead_in(slot: datetime, n: int) -> str:
    when = "Top of the hour" if slot.minute == 0 else "Half past"
    return f"{when}. {n} update{'s' if n != 1 else ''}."


# Which of a bulletin's pending items get read (top MAX_UTTERANCES) vs.
# collapsed into "plus N more" -- ranked by kind first (convergence is "the
# deepest intel upgrade", world events "always qualify" at high severity,
# watchlist is "your own beat" -- news/surge are broader-carried but less
# targeted), then within a kind by that kind's own magnitude metric.
_KIND_RANK = {"convergence": 0, "world": 1, "watchlist": 2, "news": 3, "surge": 4}

# Alert kinds that never earn an unprompted bulletin. Env-overridable as a
# comma list -- if you run an external voice agent with its own equivalent
# setting, matching the two keeps both halves of an optional handoff
# configured the same way; empty string restores the old speak-everything
# behaviour. Kept as a function, not a module constant, so the env var can be
# changed and the service restarted without a code edit.
_DEFAULT_SILENT_KINDS = "news,surge"


def _silent_kinds() -> frozenset:
    raw = os.environ.get("DISPATCH_SILENT_KINDS", _DEFAULT_SILENT_KINDS)
    return frozenset(k.strip().lower() for k in raw.split(",") if k.strip())


def _importance(entry: dict) -> tuple[int, float]:
    """Sort key for a pending entry -- LOWER sorts first (more important)."""
    kind = entry.get("kind")
    tier = _KIND_RANK.get(kind, len(_KIND_RANK))
    magnitude = {
        "world": entry.get("severity"),
        "watchlist": entry.get("beat_score"),
        "news": entry.get("dupe_count"),
        "surge": entry.get("count"),
    }.get(kind)
    return (tier, -(magnitude or 0))


def _ranked(pending: list[dict]) -> list[dict]:
    """`pending` ordered most-important-first, so the top MAX_UTTERANCES read
    aloud are the biggest items, not just whichever arrived first -- ties
    (including entries with no rankable kind) keep arrival order (stable
    sort)."""
    return sorted(pending, key=_importance)


def _next_slot(now: datetime, minutes: list[int]) -> datetime:
    """The next upcoming bulletin slot at or after `now` (rolling into the
    next hour if every slot this hour has already passed)."""
    candidates = []
    for m in minutes:
        cand = now.replace(minute=m, second=0, microsecond=0)
        if cand <= now:
            cand += timedelta(hours=1)
        candidates.append(cand)
    return min(candidates)


def _bulletin_texts(slot: datetime, pending: list[dict]) -> list[str]:
    """Exactly the utterances a bulletin at `slot` would speak for this
    pending list, in order — shared by the pre-render step and run_once's
    real speak-time loop so a cached clip lines up with what's actually said.
    The MAX_UTTERANCES read aloud are the most IMPORTANT ones (_ranked), not
    just whichever arrived first — "plus N more" is what's left after that."""
    ranked = _ranked(pending)
    texts = [_lead_in(slot, len(pending))]
    texts += [p["speak"] for p in ranked[:MAX_UTTERANCES]]
    extra = len(pending) - MAX_UTTERANCES
    if extra > 0:
        texts.append(f"Plus {extra} more on the board.")
    return texts


def maybe_prerender(
    now: datetime, pending: list[dict], prerendered_for: datetime | None
) -> datetime | None:
    """Ahead of the next scheduled slot (PRELOAD_SECONDS default 30s),
    synthesize the bulletin's audio early and cache it in kokoro_tts, so at
    the slot itself the lead-in flows straight into the news instead of
    pausing on Kokoro's render time. Returns the slot now prerendered for
    (unchanged if there was nothing new to do). A snapshot taken this early
    can miss an alert that arrives in the final PRELOAD_SECONDS — that item
    just carries over to the NEXT bulletin instead, same as any other
    between-slots arrival; nothing is ever lost, only delayed one slot."""
    minutes, _window = _schedule()
    nxt = _next_slot(now, minutes)
    if prerendered_for == nxt or not pending:
        return prerendered_for
    if (nxt - now).total_seconds() > PRELOAD_SECONDS:
        return prerendered_for
    engine, kvoice, klang, kspeed = _voice_engine()
    if engine == "kokoro":
        for text in _bulletin_texts(nxt, pending):
            kokoro_tts.prerender(text, voice=kvoice, lang=klang, speed=kspeed)
    return nxt


def _voice_engine() -> tuple[str, str, str, float]:
    """(engine, kokoro voice, kokoro lang, kokoro speed) from window.yaml.
    engine "kokoro" uses the local neural voice; anything else (or a
    missing model) uses macOS `say`. Read live so a config change lands without
    a code edit."""
    try:
        from brief import config

        cfg = config.load_window_config()
        return (
            str(cfg.get("speak_engine", "kokoro")),
            str(cfg.get("speak_kokoro_voice", "bm_george")),
            str(cfg.get("speak_kokoro_lang", "en-gb")),
            float(cfg.get("speak_kokoro_speed", 1.0)),
        )
    except Exception:
        return "kokoro", "bm_george", "en-gb", 1.0


def speak(text: str) -> None:
    """Say `text` aloud. Prefers the local neural voice (Kokoro); falls
    back to macOS `say` if Kokoro is disabled, unavailable, or errors — so the
    wall is never silenced by a voice problem. Thin wrapper over
    kokoro_tts.speak_or_say, the ONE fallback code path shared with the
    board's on-demand "read the news" button (brief/window/app.py)."""
    engine, kvoice, klang, kspeed = _voice_engine()
    kokoro_tts.speak_or_say(
        text,
        engine=engine,
        voice=kvoice,
        lang=klang,
        speed=kspeed,
        say_voice=VOICE,
        say_rate=RATE,
    )


def fetch_alerts() -> list[dict]:
    resp = requests.get(f"{BASE_URL}/api/alerts", timeout=10)
    resp.raise_for_status()
    body = resp.json()
    if body.get("contract") != "dispatch-alerts-v1":
        raise ValueError(f"unexpected contract: {body.get('contract')!r}")
    return body.get("alerts") or []


def run_once(
    seen: list[str],
    first_run: bool,
    now: datetime | None = None,
    last_slot: datetime | None = None,
    pending: list[dict] | None = None,
) -> tuple[list[str], datetime | None, list[dict]]:
    """One poll cycle. Returns (seen ids, last slot handled, pending alerts).

    The voice reads on a SCHEDULE — a bulletin at the top and bottom of the
    hour. As speak-worthy alerts appear between slots they ACCUMULATE into
    `pending` (captured with their speak text), so a story that hits the bar at
    :12 and ages out of /api/alerts before :30 is still read at :30. Split out
    (pure on its inputs besides I/O) so tests can drive cycles synchronously."""
    now = now or datetime.now()
    pending = list(pending or [])
    pending_ids = {p["id"] for p in pending}
    alerts = fetch_alerts()

    seen_set = set(seen)
    newly_seen: list[str] = []
    for a in alerts:
        aid = a.get("id")
        if not aid or aid in seen_set or aid in pending_ids:
            continue
        if (a.get("kind") or "").lower() in _silent_kinds():
            # the user, 2026-07-31: "only read news when I ask." The board mixes
            # things that concern YOU (a delivery, a calendar collision) with
            # OSINT digest items -- kinds "news" and "surge" ("Coverage surge:
            # Morocco -- 4 stories in the last hour"). Only the former earns an
            # unprompted bulletin at :00/:30.
            #
            # This speaker is one half of an optional handoff -- it reads the
            # board whenever an external voice agent (if you run one, see
            # quiet.external_agent_has_voice) isn't holding the claim file. Getting this
            # right matters: fixing only one side would silence news exactly when
            # the other agent is running, or leave both talking over each other
            # when it isn't -- the harder bug to notice and the more annoying one
            # to live with.
            #
            # Marked seen, not left pending: news must never accumulate into a
            # later bulletin. The board still shows it, and the on-demand "read the
            # news" button is untouched.
            newly_seen.append(aid)
        elif a.get("speak_worthy"):
            # Capture it NOW (id + speak text + the fields _importance() needs
            # to rank it) so it survives aging out of the feed before the
            # bulletin, and still ranks correctly even after that.
            pending.append(
                {
                    "id": aid,
                    "speak": a.get("speak") or a.get("title") or "",
                    "kind": a.get("kind"),
                    "severity": a.get("severity"),
                    "beat_score": a.get("beat_score"),
                    "dupe_count": a.get("dupe_count"),
                    "count": a.get("count"),
                }
            )
            pending_ids.add(aid)
        else:
            newly_seen.append(aid)  # never spoken -> mark seen so it can't pile up

    minutes, window = _schedule()
    slot = current_slot(now, minutes, window)

    if first_run:
        # Seed silently on restart: don't replay a backlog.
        newly_seen += [p["id"] for p in pending]
        pending = []
    elif slot is not None and slot != last_slot and pending:
        qs, qe, wd, check_cal, check_idle, idle_minutes = _quiet_hours()
        reason = quiet.quiet_reason(
            now,
            MUTE_PATH,
            qs,
            qe,
            voice_claim_path=VOICE_CLAIM_PATH,
            weekdays_only=wd,
            respect_calendar=check_cal,
            respect_display_idle=check_idle,
            display_idle_minutes=idle_minutes,
        )
        if reason:
            # Skip this bulletin rather than stacking it — board still shows all.
            print(f"  bulletin skipped — {reason} ({len(pending)} item(s))")
        else:
            # Same helper the pre-render step used, so a cached clip lines up
            # with what's actually said (and the 3 read aloud are the most
            # IMPORTANT ones, not just first-arrived — see _ranked()).
            for text in _bulletin_texts(slot, pending):
                speak(text)
        newly_seen += [p["id"] for p in pending]
        pending = []
        last_slot = slot
    # else: between bulletins (or already delivered) — keep accumulating pending.

    return seen + newly_seen, last_slot, pending


def main() -> None:
    seen, last_slot, pending = load_state()
    first_run = not STATE_PATH.exists()
    prerendered_for: datetime | None = None
    next_fetch = 0.0  # monotonic deadline; 0 forces an immediate first fetch
    while True:
        if time.monotonic() >= next_fetch:
            try:
                prev_slot = last_slot
                updated, new_slot, pending = run_once(
                    seen, first_run, last_slot=last_slot, pending=pending
                )
                save_state(updated, new_slot, pending)
                seen, last_slot = updated, new_slot
                first_run = False
                if new_slot != prev_slot:
                    # The slot just got handled (spoken or skipped) — drop any
                    # leftover prerendered clips (unclaimed or now-stale).
                    kokoro_tts.clear_prerendered()
                    prerendered_for = None
            except Exception as exc:  # noqa: BLE001 — daemon must outlive bad polls
                print(f"  speak poll FAILED ({exc!r})")
            next_fetch = time.monotonic() + POLL_SECONDS
        try:
            prerendered_for = maybe_prerender(datetime.now(), pending, prerendered_for)
        except Exception as exc:  # noqa: BLE001 — never let prerender sink the daemon
            print(f"  prerender FAILED ({exc!r})")
        time.sleep(TICK_SECONDS)


if __name__ == "__main__":
    main()
