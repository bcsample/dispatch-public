"""Sports / entertainment stoplist — a deterministic backstop for signal
quality. Local-model curation already downranks this material, but curation is
best-effort (skips during ComfyUI renders), so a keyword stoplist keeps a World
Cup or celebrity story from spiking a surge alert or labeling the map.

Deliberately phrase-level and word-bounded to avoid false positives on real
news (e.g. "match" alone is too broad; "penalty shootout" is safe).
"""

from __future__ import annotations

import re

_TERMS = [
    # sport
    "world cup",
    "premier league",
    "la liga",
    "serie a",
    "bundesliga",
    "champions league",
    "europa league",
    "uefa",
    "fifa",
    "nfl",
    "nba",
    "mlb",
    "nhl",
    "super bowl",
    "world series",
    "stanley cup",
    "playoff",
    "playoffs",
    "hat-trick",
    "hat trick",
    "top scorers",
    "all-time scorer",
    "knockout stage",
    "group stage",
    "penalty shootout",
    "grand slam",
    "wimbledon",
    "formula 1",
    "grand prix",
    "transfer window",
    "test match",
    "t20",
    "ipl cricket",
    # entertainment / celebrity
    "box office",
    "red carpet",
    "grammy",
    "oscars",
    "academy award",
    "taylor swift",
    "kardashian",
    "netflix series",
    "wwe",
    "wrestlemania",
    "andrew tate",
    "tristan tate",
    "tate brothers",
    "reality tv",
    "met gala",
    "billboard chart",
    "tour dates",
]
_RE = re.compile(
    r"\b(" + "|".join(re.escape(t) for t in _TERMS) + r")\b", re.IGNORECASE
)


def is_noise(text: str | None) -> bool:
    """True when the text matches the sports/entertainment stoplist."""
    return bool(text and _RE.search(text))
