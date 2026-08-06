"""Dedup — within this run (same url/content) and against what we've already
processed (the DB). Cheap, deterministic, keeps the same story from a dozen feeds
from drowning the brief."""

from __future__ import annotations

from .models import Item


def dedupe(items: list[Item], seen: set[str]) -> list[Item]:
    out: list[Item] = []
    batch: set[str] = set()
    for it in items:
        h = it.content_hash
        if h in seen or h in batch:
            continue
        batch.add(h)
        out.append(it)
    return out
