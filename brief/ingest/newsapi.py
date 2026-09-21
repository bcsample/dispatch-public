"""NewsAPI.ai (Event Registry) as a firehose source — real-time, high-quality
global news with clean outlet names, tuned to the operator's beat.

Distinct role vs. the other two news sources: RSS is the curated live roster,
GDELT is the broad-world firehose, and this is the BEAT feed — keyword-focused
on defense/intelligence/GovCon so the professionally-relevant world gets extra
coverage. Returns Items in rss.fetch_source shape, so it flows through the same
pipeline (dedup/geo/first_seen/curate/surge/alerts).

Real-time (unlike newsapi.org's 24h-delayed free tier). Fail-soft: no key or
any error -> []. Duplicates flagged by Event Registry are dropped up front
(our own dedup still runs on what remains).
"""

from __future__ import annotations

import os

import requests

from .. import applog
from ..models import Item
from .report import FetchReport

log = applog.get(__name__)

GET_ARTICLES_URL = "https://eventregistry.org/api/v1/article/getArticles"

# Beat-focused defaults (Event Registry OR-matches this keyword list). Override
# via config (newsapi_keywords) to refocus what this feed pulls.
DEFAULT_KEYWORDS = [
    "defense contract",
    "intelligence agency",
    "national security",
    "Pentagon",
    "cyberattack",
    "artificial intelligence",
    "sanctions",
    "military",
]


def api_key() -> str:
    return os.environ.get("NEWSAPI_KEY", "").strip()


def fetch(
    keywords: list[str] | None = None,
    count: int = 40,
    key: str | None = None,
    timeout: float = 20,
    report: FetchReport | None = None,
) -> list[Item]:
    """Recent Event Registry articles matching `keywords` (OR) as Items,
    newest-first. Returns [] with no key or on any error; `report`, when
    given, records the attempt and the error so the caller can tell the two
    apart (N19)."""
    token = (key if key is not None else api_key()).strip()
    if not token:
        return []
    if report is not None:
        report.attempt()
    body = {
        "action": "getArticles",
        "keyword": keywords or DEFAULT_KEYWORDS,
        "keywordOper": "or",
        "lang": "eng",
        "articlesCount": count,
        "articlesSortBy": "date",
        "resultType": "articles",
        "includeArticleConcepts": False,
        "isDuplicateFilter": "skipDuplicates",
        "apiKey": token,
    }
    try:
        resp = requests.post(GET_ARTICLES_URL, json=body, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            log.warning("NewsAPI.ai error (%s)", data["error"])
            if report is not None:
                report.fail(f"error payload: {data['error']}")
            return []
        results = data.get("articles", {}).get("results") or []
    except Exception as exc:  # noqa: BLE001 — fail-soft: fall back to other feeds
        log.error("NewsAPI.ai fetch FAILED (%r)", exc)
        if report is not None:
            report.fail(repr(exc))
        return []

    items: list[Item] = []
    for a in results:
        title = (a.get("title") or "").strip()
        url = a.get("url") or ""
        if not title or not url or a.get("isDuplicate"):
            continue
        source = a.get("source") or {}
        items.append(
            Item(
                source_name=source.get("title") or source.get("uri") or "NewsAPI.ai",
                source_type="news",
                trust="medium",
                title=title,
                url=url,
                published_at=a.get("dateTime") or "",
            )
        )
    return items
