"""Surge detection over the rolling news window (dispatch-alerts-v1, kind
"surge") — the "coverage is spiking on X" signal.

Statistical but deliberately simple: for each geolocated place, compare the
story count in the last `window_minutes` against hourly baseline bins built
from the rest of the 48h `news_articles` retention window, and flag when the
z-score clears `z_threshold` AND the raw count clears `min_stories` (tiny-count
noise floor — 2 stories is never a surge no matter what the baseline says).

Zero-history places degrade correctly: mean 0 / sigma floored at 1 makes
z == count, so a brand-new hot spot (3+ stories on a place we've never seen)
still triggers. All pure functions over rows the caller fetched — no I/O, no
LLM, cheap enough for the poll path.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta


def _hourly_bins(times: list[datetime], start: datetime, end: datetime) -> list[int]:
    """Story counts per whole hour in [start, end) — zeros included, so quiet
    hours pull the baseline mean/σ down like they should."""
    hours = max(1, int((end - start).total_seconds() // 3600))
    bins = [0] * hours
    for t in times:
        if start <= t < end:
            idx = int((t - start).total_seconds() // 3600)
            if 0 <= idx < hours:
                bins[idx] += 1
    return bins


def detect_place_surges(
    rows: list[dict],
    now: datetime,
    window_minutes: float = 60,
    min_stories: int = 3,
    z_threshold: float = 3.0,
    baseline_hours: int = 47,
) -> list[dict]:
    """`rows`: dicts with place / first_seen (aware datetime) / lat / lon.
    Returns one dict per surging place: {place, count, mean, z, lat, lon,
    window_minutes}, strongest z first."""
    window_start = now - timedelta(minutes=window_minutes)
    baseline_start = window_start - timedelta(hours=baseline_hours)

    by_place: dict[str, list[dict]] = {}
    for r in rows:
        if r.get("place") and r.get("first_seen"):
            by_place.setdefault(r["place"], []).append(r)

    surges: list[dict] = []
    for place, items in by_place.items():
        recent = [r for r in items if r["first_seen"] >= window_start]
        n = len(recent)
        if n < min_stories:
            continue
        bins = _hourly_bins(
            [r["first_seen"] for r in items], baseline_start, window_start
        )
        mean = sum(bins) / len(bins)
        variance = sum((b - mean) ** 2 for b in bins) / len(bins)
        sigma = max(math.sqrt(variance), 1.0)  # floor: no div-by-zero, no
        # tiny-sample blowups
        z = (n - mean) / sigma
        if z < z_threshold:
            continue
        newest = max(recent, key=lambda r: r["first_seen"])
        surges.append(
            {
                "place": place,
                "count": n,
                "mean": round(mean, 2),
                "z": round(z, 2),
                "lat": newest.get("lat"),
                "lon": newest.get("lon"),
                "window_minutes": window_minutes,
            }
        )

    surges.sort(key=lambda s: s["z"], reverse=True)
    return surges
