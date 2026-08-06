"""The common item schema — every ingested thing normalizes into this, whatever the
source (RSS now; SAM.gov, email, calendar later). Getting this right once is what
lets new ingestors bolt on without touching the rest of the pipeline."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Item:
    source_name: str
    source_type: str  # news | gov | contract | email | calendar
    title: str
    url: str = ""
    published_at: str = ""  # ISO8601
    retrieved_at: str = field(default_factory=_utcnow)
    raw_text: str = ""  # summary/snippet or full text
    trust: str = "medium"  # high | medium | low
    # filled by the pipeline:
    summary: str = ""
    topics: list[str] = field(default_factory=list)
    entities: list[str] = field(default_factory=list)
    relevance_score: int = 0
    user_reason: str = ""
    recommended_action: str = ""
    # world-delta fields (optional; blank/None for every non-"world" source_type):
    lat: float | None = None
    lon: float | None = None
    severity: float | None = None
    delta_kind: str = ""  # new | changed | gone

    @property
    def content_hash(self) -> str:
        """Stable id for dedup + 'have I processed this already'.

        World-delta items share a feed URL — every OFAC row points at the same
        SDN search page — so keying on url alone collapsed all sanctions deltas
        onto ONE row via INSERT OR REPLACE (a whole day of changes -> 1). World
        items therefore key on source+title (the title carries the unique
        entity/CVE/quake identity). News keeps url-based dedup so the same story
        from one outlet stays one item."""
        if self.source_type == "world":
            basis = (self.source_name + "|" + self.title).strip().lower()
        else:
            basis = (self.url or (self.title + self.source_name)).strip().lower()
        return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]

    def to_row(self) -> dict:
        d = asdict(self)
        d["content_hash"] = self.content_hash
        d["topics"] = ",".join(self.topics)
        d["entities"] = ",".join(self.entities)
        return d
