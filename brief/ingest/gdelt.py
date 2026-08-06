"""GDELT DOC 2.0 as a firehose source — a free, no-key window onto global news
coverage far wider than the RSS roster (thousands of outlets, worldwide).

Returns Items in the exact shape rss.fetch_source does, so GDELT articles flow
through the SAME news pipeline: dedup (nomic-embed), gazetteer geolocation,
first_seen store, curation, surge, alerts. English-filtered by default
(sourcelang:eng) so titles geolocate and dedup against the English RSS feeds.

GDELT rate-limits to ~1 request / 5s and answers throttling with either a
real HTTP 429 or a 200 with a plaintext notice (not JSON) — the news loop
calls this once per ~10-min cycle, so day-to-day this shouldn't hit that
limit, but Fable's full-log scan (2026-07-28) found 755 429s in the wild
(GDELT is a free shared-IP endpoint; something else can trip its limiter).
fetch() returns None (not []) specifically for a rate-limit signal so
NewsLoop can back off calling GDELT for a few cycles instead of retrying
into the same limiter every 10 minutes — genuine "fetched OK, zero
articles" and any OTHER error (network blip, timeout, bad JSON shape)
still fail soft to [], no backoff needed for those.
"""

from __future__ import annotations

from datetime import datetime, timezone

import requests

from .. import applog
from ..models import Item

log = applog.get(__name__)

DOC_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
_UA = "dispatch/1.0 (world-news dashboard)"

# Broad world-events default; override via config (gdelt_query). English-only so
# the shared gazetteer/dedup work; the parenthesised OR is GDELT query syntax.
DEFAULT_QUERY = (
    "(war OR sanctions OR military OR election OR protest OR diplomacy OR "
    "attack OR crisis OR treaty OR coup) sourcelang:eng"
)


def _published(seendate: str) -> str:
    """GDELT seendate 'YYYYMMDDTHHMMSSZ' -> ISO8601 UTC; '' if unparseable."""
    try:
        dt = datetime.strptime(seendate, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        return dt.isoformat()
    except (ValueError, TypeError):
        return ""


def fetch(
    query: str = DEFAULT_QUERY,
    timespan: str = "1h",
    max_records: int = 75,
    timeout: float = 25,
) -> list[Item] | None:
    """Recent GDELT articles for `query` as Items, newest-first. Returns None
    specifically when GDELT is signaling rate-limiting (a real 429, or the
    200-with-plaintext throttle notice) so the caller can back off; any OTHER
    failure (network error, timeout, unexpected JSON shape) fails soft to []
    same as before -- only a rate-limit signal is worth backing off for."""
    try:
        resp = requests.get(
            DOC_URL,
            params={
                "query": query,
                "mode": "artlist",
                "format": "json",
                "timespan": timespan,
                "maxrecords": max_records,
                "sort": "datedesc",
            },
            headers={"User-Agent": _UA},
            timeout=timeout,
        )
        if resp.status_code == 429:
            log.warning("GDELT rate-limited (HTTP 429)")
            return None
        resp.raise_for_status()
        # Throttling can also come back as a 200 with text or HTML, not JSON.
        if "json" not in resp.headers.get("Content-Type", "").lower():
            log.warning("GDELT skipped (non-JSON response — likely rate-limited)")
            return None
        articles = resp.json().get("articles") or []
    except Exception as exc:  # noqa: BLE001 — fail-soft: fall back to RSS only
        log.error("GDELT fetch FAILED (%r)", exc)
        return []

    items: list[Item] = []
    for a in articles:
        title = (a.get("title") or "").strip()
        url = a.get("url") or ""
        if not title or not url:
            continue
        items.append(
            Item(
                source_name=a.get("domain") or "GDELT",
                source_type="news",
                trust="medium",
                title=title,
                url=url,
                published_at=_published(a.get("seendate", "")),
            )
        )
    return items
