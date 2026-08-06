"""Live aircraft near a point — the "Skies" departure-board panel (inspired by
theflightwall.com). Reads the free, no-auth adsb.fi open ADS-B API, so unlike
curation this touches no LLM and carries zero ComfyUI memory risk — safe to run
live at all times.

Cloud DATA, local compute (consistent with the news feeds): the only external
call is the ADS-B fetch. Fail-soft end to end — a bad fetch just leaves the
previously cached board in place; the panel never blocks the rest of the loop.
"""

from __future__ import annotations

import os

import requests

# adsb.fi open data: GET /v2/lat/{lat}/lon/{lon}/dist/{nm} -> {"ac": [...]}.
ADSB_BASE = os.environ.get("ADSB_BASE_URL", "https://opendata.adsb.fi/api/v2").rstrip(
    "/"
)
_UA = "dispatch-skies/1.0"
_KNOTS_TO_MPH = 1.15078


def _int(v) -> int | None:
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return None


def _normalize(ac: dict) -> dict | None:
    """One adsb.fi aircraft record -> the panel shape. None if it has no usable
    identity."""
    callsign = (ac.get("flight") or ac.get("r") or ac.get("hex") or "").strip()
    if not callsign:
        return None
    alt_raw = ac.get("alt_baro")
    on_ground = alt_raw == "ground"
    gs = ac.get("gs")
    return {
        "callsign": callsign,
        "on_ground": on_ground,
        "altitude": None if on_ground else _int(alt_raw),  # feet
        "speed": _int(float(gs) * _KNOTS_TO_MPH) if gs is not None else None,  # mph
        "heading": _int(ac.get("track")),  # degrees
        "climb": _int(ac.get("baro_rate")),  # feet/min (+ up, - down)
        "lat": ac.get("lat"),
        "lon": ac.get("lon"),
    }


def fetch_flights(
    lat: float, lon: float, radius_nm: float = 30, limit: int = 12, timeout: float = 12
) -> list[dict] | None:
    """Aircraft within `radius_nm` of (lat, lon), airborne first then by
    altitude descending, capped to `limit`. Returns None on any fetch/parse
    error (caller keeps the prior board)."""
    try:
        resp = requests.get(
            f"{ADSB_BASE}/lat/{lat}/lon/{lon}/dist/{radius_nm}",
            headers={"User-Agent": _UA},
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        aircraft = data.get("ac") or data.get("aircraft") or []
        # Parse INSIDE the try — one malformed field must not escape and kill
        # the FlightLoop thread; the whole fetch just fails soft to None.
        out = [n for ac in aircraft if (n := _normalize(ac))]
        # Airborne aircraft lead; among them the highest fly first (a stable,
        # glanceable order). On-ground traffic sinks to the bottom.
        out.sort(key=lambda a: (a["on_ground"], -(a["altitude"] or 0)))
        return out[:limit]
    except Exception:  # noqa: BLE001 — fail-soft: keep the cached board
        return None
