"""RSS ingestion — the first ingestor. Each entry normalizes into an Item. New
ingestors (SAM.gov, Gmail, calendar) just return Items the same way."""

from __future__ import annotations

import re
from datetime import datetime, timezone

import feedparser
import requests

from .. import applog
from ..models import Item

log = applog.get(__name__)

# feedparser.parse(url) fetches with NO timeout — one hung feed server would
# block the caller's thread forever (the Dispatch news loop found this the
# hard way: headlines silently freeze until a restart). So fetch ourselves
# with a hard timeout and hand feedparser the bytes.
_FETCH_TIMEOUT = 20
_UA = "Mozilla/5.0 (compatible; dispatch-brief/1.0)"

_TAGS = re.compile(r"<[^>]+>")


def _clean(html: str) -> str:
    return _TAGS.sub("", html or "").strip()


def _published(entry) -> str:
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            return datetime(*t[:6], tzinfo=timezone.utc).isoformat()
    return ""


def fetch_source(source: dict, max_entries: int = 30) -> list[Item]:
    resp = requests.get(
        source["rss"], timeout=_FETCH_TIMEOUT, headers={"User-Agent": _UA}
    )
    resp.raise_for_status()
    feed = feedparser.parse(resp.content)
    items: list[Item] = []
    for e in feed.entries[:max_entries]:
        items.append(
            Item(
                source_name=source["name"],
                source_type=source.get("type", "news"),
                trust=source.get("trust", "medium"),
                title=_clean(e.get("title", "")),
                url=e.get("link", ""),
                published_at=_published(e),
                raw_text=_clean(e.get("summary", ""))[:2000],
            )
        )
    return items


def fetch_all(sources: list[dict]) -> list[Item]:
    items: list[Item] = []
    for src in sources:
        try:
            got = fetch_source(src)
            items.extend(got)
            log.info("%s: %d items", src["name"], len(got))
        except Exception as exc:  # noqa: BLE001 — one bad feed shouldn't kill the run
            log.error("%s: FAILED (%r)", src["name"], exc)
    return items
