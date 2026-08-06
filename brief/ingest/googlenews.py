"""Google News RSS keyword search as a firehose source — the free NewsAPI.ai
replacement (the user: "I don't want to pay for anything... build google news
rss"). No key, no token accounting, no rate-limit risk — the exact technique
the user's own Apps Script morning digest already uses for company-watchlist
searches, just pointed at the same defense/intel/GovCon beat keywords
newsapi.py covered.

Returns Items in the exact shape rss.fetch_source does, so results flow
through the SAME pipeline (dedup/geo/first_seen/curate/surge/alerts).

Two independent sources, both fetched every cycle:
- KEYWORDS (data/google_news_keywords.json) — plain search queries. Works
  great for the beat terms AND for hyper-local area names (a city/neighborhood
  name verified live to return genuinely local results) — but NOT for
  "breaking" or "world" as literal search words, which just match articles
  that happen to use those words.
- FEEDS (data/google_news_feeds.json) — Google's own curated feeds (top
  stories, topic sections) for exactly that "breaking"/"world" need. Topic
  sections use an opaque per-topic token (not a human name) that changed
  from older Google News RSS docs; see WORLD_TOPIC_TOKEN.

Both lists are loaded fresh every news-loop cycle (not captured at startup),
so edits from the keyword management page (GET /keywords) land on the NEXT
fetch with no service restart.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

import feedparser
import requests

from .. import applog
from ..config import DATA_DIR
from ..models import Item

log = applog.get(__name__)

SEARCH_URL = "https://news.google.com/rss/search"
TOP_STORIES_URL = "https://news.google.com/rss"
WORLD_TOPIC_TOKEN = "CAAqJggKIiBDQkFTRWdvSUwyMHZNRGx1YlY4U0FtVnVHZ0pWVXlnQVAB"
# Google News' own curated section feeds use an opaque per-topic token, not a
# human-readable name (an old "?topic=WORLD"-style URL 404s/returns empty --
# found live 2026-07-24). Extracted from news.google.com's own nav links for
# the en-US/US locale; stable for that locale, not per-session. Offered as
# presets on the keyword management page so the user never has to hunt one down.
TOPIC_TOKENS = {
    "World": WORLD_TOPIC_TOKEN,
    "U.S.": "CAAqIggKIhxDQkFTRHdvSkwyMHZNRGxqTjNjd0VnSmxiaWdBUAE",
    "Business": "CAAqJggKIiBDQkFTRWdvSUwyMHZNRGx6TVdZU0FtVnVHZ0pWVXlnQVAB",
    "Technology": "CAAqJggKIiBDQkFTRWdvSUwyMHZNRGRqTVhZU0FtVnVHZ0pWVXlnQVAB",
    "Entertainment": "CAAqJggKIiBDQkFTRWdvSUwyMHZNREpxYW5RU0FtVnVHZ0pWVXlnQVAB",
    "Sports": "CAAqJggKIiBDQkFTRWdvSUwyMHZNRFp1ZEdvU0FtVnVHZ0pWVXlnQVAB",
    "Science": "CAAqJggKIiBDQkFTRWdvSUwyMHZNRFp0Y1RjU0FtVnVHZ0pWVXlnQVAB",
    "Health": "CAAqIQgKIhtDQkFTRGdvSUwyMHZNR3QwTlRFU0FtVnVLQUFQAQ",
}
_UA = "Mozilla/5.0 (compatible; dispatch-brief/1.0)"
_FETCH_TIMEOUT = 10
_TAGS = re.compile(r"<[^>]+>")

KEYWORDS_PATH = DATA_DIR / "google_news_keywords.json"
FEEDS_PATH = DATA_DIR / "google_news_feeds.json"

# The seed list a fresh install starts with -- edit config/profile.yaml for
# your own interests, or use the keyword management page (GET /keywords) to
# add/remove these live, no restart needed. Local-area terms (a city/region
# name) work fine as plain search here, verified live to return genuinely
# hyper-local results -- unlike "breaking"/"world", which need Google's own
# curated feeds (below), not a literal keyword search for those words.
DEFAULT_KEYWORDS = [
    "defense contract",
    "intelligence agency",
    "national security",
    "Pentagon",
    "cyberattack",
    "artificial intelligence",
    "sanctions",
    "military",
    "Washington, DC",
]

# Named Google-curated feeds (top stories, topic sections) -- distinct from
# keyword search: these are Google's own editorial feeds, not an OR-query.
# {"type": "top"} -> TOP_STORIES_URL; {"type": "topic", "value": <token>} ->
# that section's feed.
DEFAULT_FEEDS = [
    {"label": "Breaking / Top Stories", "type": "top"},
    {"label": "World News", "type": "topic", "value": WORLD_TOPIC_TOKEN},
]


def _clean(html: str) -> str:
    return _TAGS.sub("", html or "").strip()


def _published(entry) -> str:
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            return datetime(*t[:6], tzinfo=timezone.utc).isoformat()
    return ""


def load_keywords() -> list[str]:
    """Current keyword list, fail-soft to DEFAULT_KEYWORDS if the file is
    missing/corrupt (a bad edit must never silence this feed)."""
    try:
        data = json.loads(KEYWORDS_PATH.read_text(encoding="utf-8"))
        keywords = [str(k).strip() for k in data if str(k).strip()]
        return keywords or list(DEFAULT_KEYWORDS)
    except Exception:  # noqa: BLE001 — missing file, bad JSON, wrong shape
        return list(DEFAULT_KEYWORDS)


def save_keywords(keywords: list[str]) -> None:
    KEYWORDS_PATH.parent.mkdir(parents=True, exist_ok=True)
    KEYWORDS_PATH.write_text(json.dumps(keywords, indent=2), encoding="utf-8")


def add_keyword(keyword: str) -> list[str]:
    """Add `keyword` (case-insensitive dedup) and persist. Returns the new
    list. Blank input is a no-op (still returns the current list)."""
    keyword = keyword.strip()
    keywords = load_keywords()
    if keyword and keyword.lower() not in {k.lower() for k in keywords}:
        keywords.append(keyword)
        save_keywords(keywords)
    return keywords


def remove_keyword(keyword: str) -> list[str]:
    keywords = [k for k in load_keywords() if k.lower() != keyword.strip().lower()]
    save_keywords(keywords)
    return keywords


def load_feeds() -> list[dict]:
    """Current named-feed list (top stories, topic sections), fail-soft to
    DEFAULT_FEEDS if the file is missing/corrupt."""
    try:
        data = json.loads(FEEDS_PATH.read_text(encoding="utf-8"))
        feeds = [f for f in data if isinstance(f, dict) and f.get("type")]
        return feeds or list(DEFAULT_FEEDS)
    except Exception:  # noqa: BLE001 — missing file, bad JSON, wrong shape
        return list(DEFAULT_FEEDS)


def save_feeds(feeds: list[dict]) -> None:
    FEEDS_PATH.parent.mkdir(parents=True, exist_ok=True)
    FEEDS_PATH.write_text(json.dumps(feeds, indent=2), encoding="utf-8")


def _feed_key(feed: dict) -> tuple:
    return (feed.get("type"), feed.get("value", ""))


def add_feed(label: str, feed_type: str, value: str = "") -> list[dict]:
    """Add a named feed ("top" needs no value; "topic" needs a topic token).
    De-dupes on (type, value). Returns the new list."""
    value = value.strip()
    entry = {"label": label.strip() or value or feed_type, "type": feed_type}
    if value:
        entry["value"] = value
    feeds = load_feeds()
    if _feed_key(entry) not in {_feed_key(f) for f in feeds}:
        feeds.append(entry)
        save_feeds(feeds)
    return feeds


def remove_feed(feed_type: str, value: str = "") -> list[dict]:
    key = (feed_type, value.strip())
    feeds = [f for f in load_feeds() if _feed_key(f) != key]
    save_feeds(feeds)
    return feeds


def _feed_url(feed: dict) -> str | None:
    t = feed.get("type")
    if t == "top":
        return TOP_STORIES_URL
    if t == "topic" and feed.get("value"):
        return f"https://news.google.com/rss/topics/{feed['value']}"
    return None


def _parse_entries(feed, count: int) -> list[Item]:
    items: list[Item] = []
    for e in feed.entries[:count]:
        title = _clean(e.get("title", ""))
        url = e.get("link", "")
        if not title or not url:
            continue
        # Google News RSS titles are "Headline - Outlet Name" -- split it out
        # so the board shows a real outlet, matching RSS/GDELT's source_name
        # instead of a generic "Google News" label on every row.
        source_name = "Google News"
        if " - " in title:
            title, _, outlet = title.rpartition(" - ")
            source_name = outlet.strip() or source_name
        items.append(
            Item(
                source_name=source_name,
                source_type="news",
                trust="medium",
                title=title.strip(),
                url=url,
                published_at=_published(e),
                raw_text=_clean(e.get("summary", ""))[:2000],
            )
        )
    return items


def _fetch_url(url: str, params: dict, count: int, timeout: float, label: str) -> list[Item]:
    try:
        resp = requests.get(
            url, params=params, timeout=timeout, headers={"User-Agent": _UA}
        )
        resp.raise_for_status()
        feed = feedparser.parse(resp.content)
    except Exception as exc:  # noqa: BLE001 — one bad feed/keyword shouldn't cost the rest
        log.error("Google News RSS (%r) FAILED (%r)", label, exc)
        return []
    return _parse_entries(feed, count)


def _fetch_keyword(keyword: str, count: int, timeout: float) -> list[Item]:
    return _fetch_url(
        SEARCH_URL,
        {"q": keyword, "hl": "en-US", "gl": "US", "ceid": "US:en"},
        count,
        timeout,
        keyword,
    )


def _fetch_feed(feed: dict, count: int, timeout: float) -> list[Item]:
    url = _feed_url(feed)
    if not url:
        return []
    return _fetch_url(
        url,
        {"hl": "en-US", "gl": "US", "ceid": "US:en"},
        count,
        timeout,
        feed.get("label") or feed.get("type", "feed"),
    )


def fetch(
    keywords: list[str] | None = None,
    count_per_keyword: int = 8,
    timeout: float = _FETCH_TIMEOUT,
    include_feeds: bool = True,
) -> list[Item]:
    """Recent Google News RSS results for each keyword PLUS each named feed
    (top stories, topic sections) as Items. One request per keyword/feed
    (Google News RSS has no multi-keyword OR syntax like GDELT/Event
    Registry did) -- fail-soft per source, so one bad query just contributes
    nothing while the rest still run."""
    items: list[Item] = []
    for kw in (keywords if keywords is not None else load_keywords()):
        items.extend(_fetch_keyword(kw, count_per_keyword, timeout))
    if include_feeds:
        for feed in load_feeds():
            items.extend(_fetch_feed(feed, count_per_keyword, timeout))
    return items
