"""Enrich the Skies board with airline + from/to route, via adsbdb.com (free,
no key). adsb.fi gives position/speed but not route or airline; adsbdb maps a
callsign -> {airline, origin IATA, destination IATA}.

Cached hard: a route doesn't change mid-flight, so each callsign is looked up
at most once every few hours; unknown callsigns (GA/military/no-route) are
negative-cached so they aren't retried every cycle. Per-cycle lookup budget
caps the network work — uncached flights just fill in on a later cycle. No LLM,
no ComfyUI memory concern; runs in the FlightLoop, off the poll. Fail-soft:
any error leaves a flight un-enriched (blank route), never raises.
"""

from __future__ import annotations

import time

import requests

ADSBDB_URL = "https://api.adsbdb.com/v0/callsign/"
_UA = "dispatch/1.0"
POS_TTL = 6 * 3600  # known route: cache 6h
NEG_TTL = 30 * 60  # unknown callsign: recheck after 30 min
_MAX_CACHE = 2000

# callsign -> (data|None, expires_at)
_CACHE: dict[str, tuple[dict | None, float]] = {}


def _fetch(callsign: str) -> dict | None:
    try:
        resp = requests.get(
            f"{ADSBDB_URL}{callsign}", headers={"User-Agent": _UA}, timeout=6
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        fr = (resp.json().get("response") or {}).get("flightroute")
        if not fr:
            return None
        return {
            "airline": (fr.get("airline") or {}).get("name"),
            "from": (fr.get("origin") or {}).get("iata_code"),
            "to": (fr.get("destination") or {}).get("iata_code"),
        }
    except Exception:  # noqa: BLE001 — fail-soft: no enrichment this time
        return None


def _prune(now: float) -> None:
    if len(_CACHE) <= _MAX_CACHE:
        return
    for cs in [cs for cs, (_, exp) in _CACHE.items() if exp <= now]:
        _CACHE.pop(cs, None)


def enrich(aircraft: list[dict], max_lookups: int = 6, _now=None) -> list[dict]:
    """Add airline / from / to to each aircraft in place (and return the list).
    At most `max_lookups` uncached callsigns hit adsbdb per call; the rest fill
    in on a later cycle from cache. Cached results apply with no network."""
    now = time.time() if _now is None else _now
    _prune(now)
    budget = max_lookups
    for a in aircraft:
        cs = (a.get("callsign") or "").strip()
        if not cs:
            continue
        entry = _CACHE.get(cs)
        if entry and entry[1] > now:
            data = entry[0]
        elif budget > 0:
            data = _fetch(cs)
            _CACHE[cs] = (data, now + (POS_TTL if data else NEG_TTL))
            budget -= 1
        else:
            data = entry[0] if entry else None  # stale/unknown -> fill next cycle
        if data:
            a["airline"] = data.get("airline")
            a["from"] = data.get("from")
            a["to"] = data.get("to")
    return aircraft
