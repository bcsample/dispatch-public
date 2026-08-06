"""Deterministic relevance scoring — pure code, no LLM. This is what makes the brief
*yours* and keeps cost/noise down: we score everything cheaply here and only send the
top-N to the model. Tunable entirely from profile.yaml."""

from __future__ import annotations

from datetime import datetime, timezone

from .models import Item

_TRUST_BONUS = {"high": 10, "medium": 4, "low": 0}


def _recent(published_at: str, hours: int) -> bool:
    if not published_at:
        return False
    try:
        dt = datetime.fromisoformat(published_at)
    except ValueError:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds() <= hours * 3600


def score_item(item: Item, profile: dict) -> int:
    text = f"{item.title} {item.raw_text}".lower()
    score = 0
    matched_topics, matched_entities = [], []

    for entity in profile.get("watch_entities", []):
        if entity.lower() in text:
            score += 30
            matched_entities.append(entity)
    for topic in profile.get("primary_topics", []):
        if topic.lower() in text:
            score += 20
            matched_topics.append(topic)
    for kw in profile.get("opportunity_keywords", []):
        if kw.lower() in text:
            score += 15
            matched_topics.append(kw)
    for bad in profile.get("negative_keywords", []):
        if bad.lower() in text:
            score -= 20

    if item.source_type in ("contract", "gov"):
        score += 10
    score += _TRUST_BONUS.get(item.trust, 0)
    if _recent(item.published_at, profile.get("recency_hours", 36)):
        score += 5
    # World Delta: a change's severity (quake magnitude, fire FRP, ...) bumps
    # the score, so "M6 near a watch-area" outranks "M3 nowhere". Watch-areas
    # are just geographic watch_entities/primary_topics already above.
    score += int((item.severity or 0) * profile.get("world_severity_weight", 5))

    item.relevance_score = max(0, min(score, 100))
    item.topics = sorted(set(matched_topics))
    item.entities = sorted(set(matched_entities))
    return item.relevance_score


def select(items: list[Item], profile: dict) -> list[Item]:
    """Score all, keep those clearing min_score, return the top max_items_to_llm."""
    for it in items:
        score_item(it, profile)
    kept = [i for i in items if i.relevance_score >= profile.get("min_score", 35)]
    kept.sort(key=lambda i: i.relevance_score, reverse=True)
    return kept[: profile.get("max_items_to_llm", 25)]
