"""Lightweight place-name geolocation for news headlines (v4.2 stage 1, no LLM).

Loads a bundled gazetteer (major world cities + countries-via-capital + a few
common aliases) and matches place names in a headline title to lat/lon, so a
news story can drop a pin on the map. Runs off the live path (news-fetch
cadence, ~10 min), never the 10s poll. Deliberately mechanical and imperfect:
high-confidence whole-word matches only, most-specific (longest name) wins; a
headline with no recognized place simply gets no pin. The Ollama NER upgrade
(v4.2 proper) is the later precision pass — this is the cheap first cut.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

_GAZ_PATH = Path(__file__).parent / "static" / "gazetteer.json"
_ENTRIES: list[tuple[str, re.Pattern, list]] | None = None  # longest-name-first


def _load() -> list[tuple[str, re.Pattern, list]]:
    global _ENTRIES
    if _ENTRIES is not None:
        return _ENTRIES
    try:
        gaz = json.loads(_GAZ_PATH.read_text(encoding="utf-8"))
    except Exception:
        gaz = {}
    # Longest names first so "south china sea" beats "china", "new york" beats "york".
    ordered = sorted(gaz.items(), key=lambda kv: len(kv[0]), reverse=True)
    _ENTRIES = [
        (name, re.compile(r"\b" + re.escape(name) + r"\b", re.IGNORECASE), coords)
        for name, coords in ordered
    ]
    return _ENTRIES


def locate(text: str | None) -> dict | None:
    """Return {'lat','lon','place'} for the first (most-specific) gazetteer place
    found as a whole word in `text`, else None. Fail-soft: any error -> None."""
    if not text:
        return None
    try:
        for name, pattern, (lat, lon) in _load():
            if pattern.search(text):
                return {"lat": lat, "lon": lon, "place": name.title()}
    except Exception:
        return None
    return None
