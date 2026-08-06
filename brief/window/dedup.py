"""Embeddings-based dedup/clustering for the news firehose. Collapses
near-duplicate headlines ("the same story from 5 sources") into one
representative row with the sources aggregated, so the firehose reads as
signal.

Uses `nomic-embed-text` via Ollama — small (274MB) and cheap enough to run
on the ~10min news-fetch cadence without significant memory pressure. Off
the 10s poll entirely: only `NewsLoop.run_one_fetch` (service.py) calls
into this module. `/api/status`, `/api/health`, `/api/deltas`, `/api/current`
never touch Ollama.

Fail-soft end to end: any embed() failure (Ollama down, timeout, bad JSON)
returns None, and cluster() falls back to passthrough singletons — the window
keeps working, just unclustered.
"""

from __future__ import annotations

import math
import os

import requests

from .. import modelroles
from ..config import OLLAMA_HOST

HOST = OLLAMA_HOST
# DEDUP_EMBED_MODEL always wins outright if set (never even asks a model
# registry); otherwise resolve role "embed" via modelroles (optional --
# falls back to the nomic-embed-text default below if none is configured).
_MODEL_DEFAULT = "nomic-embed-text"
MODEL = os.environ.get("DEDUP_EMBED_MODEL") or modelroles.resolve("embed", _MODEL_DEFAULT)

# Calibrated 2026-07-16 against nomic-embed-text: known-duplicate headline
# pairs measured 0.81-0.91 cosine similarity; unrelated pairs measured
# 0.35-0.41. 0.80 sits cleanly in the gap between them.
DEFAULT_SIMILARITY_THRESHOLD = 0.80


def embed(texts: list[str]) -> list[list[float]] | None:
    """One batched POST /api/embed call for all `texts`. Returns a list of
    vectors (same order as `texts`), or None on ANY error — Ollama down,
    timeout, malformed response. Never raises."""
    if not texts:
        return []
    try:
        resp = requests.post(
            f"{HOST}/api/embed",
            json={"model": MODEL, "input": texts, "keep_alive": 0},
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
        vectors = data.get("embeddings")
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            return None
        return vectors
    except Exception:  # noqa: BLE001 — fail-soft is the whole point here
        return None


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _singleton(headline: dict) -> dict:
    out = dict(headline)
    out["sources"] = [headline.get("source_name")]
    out["dupe_count"] = 1
    return out


def cluster(
    headlines: list[dict], threshold: float = DEFAULT_SIMILARITY_THRESHOLD
) -> list[dict]:
    """Greedily group `headlines` (each with a `title`) by cosine similarity
    of their embedded titles. Each cluster collapses to ONE representative —
    the newest by `published_at` (so its lat/lon/place, if any, is what the
    map pins) — enriched with `sources` (unique source_names across the
    cluster, representative first) and `dupe_count` (cluster size).
    Newest-first order of representatives is preserved (mirrors the input
    order, which `_headline_dicts` already sorts newest-first).

    If `embed()` returns None (Ollama down/erroring), every headline passes
    through unchanged as its own singleton cluster — the window still works,
    just unclustered."""
    if not headlines:
        return []

    titles = [h.get("title") or "" for h in headlines]
    vectors = embed(titles)
    if vectors is None:
        return [_singleton(h) for h in headlines]

    n = len(headlines)
    assigned = [False] * n
    clusters: list[list[int]] = []
    for i in range(n):
        if assigned[i]:
            continue
        assigned[i] = True
        group = [i]
        for j in range(i + 1, n):
            if assigned[j]:
                continue
            if _cosine(vectors[i], vectors[j]) >= threshold:
                assigned[j] = True
                group.append(j)
        clusters.append(group)

    out: list[dict] = []
    for group in clusters:
        members = [headlines[idx] for idx in group]
        # Newest by published_at wins as representative (preserves its
        # lat/lon/place so the map still gets one pin per story).
        representative = max(members, key=lambda h: h.get("published_at") or "")
        sources: list[str] = [representative.get("source_name")]
        for m in members:
            name = m.get("source_name")
            if m is not representative and name not in sources:
                sources.append(name)
        row = dict(representative)
        row["sources"] = sources
        row["dupe_count"] = len(members)
        out.append(row)

    # Preserve newest-first order of representatives (input order already is
    # newest-first; sort the collapsed rows the same way for consistency
    # after the greedy grouping potentially reordered them).
    out.sort(key=lambda h: h.get("published_at") or "", reverse=True)
    return out
