"""The spine: ingest → dedupe → score/select → generate → store.
Each stage is a separate, swappable function — new sources plug in here without
disturbing the rest.

Delivery was retired per the Dispatch Integration decision (README § Dispatch
Integration, architecture review ADR 2026-07-16): the Apps Script Morning Digest owns email
permanently, and this batch pipeline is an on-demand deep-dive tool — never
scheduled, never mailing anything."""

from __future__ import annotations

from datetime import date

from . import applog, config, db, dedupe, generate, score
from .ingest import rss, world

log = applog.get(__name__)


def run_daily_brief() -> dict:
    profile = config.load_profile()
    sources = config.load_sources()
    world_feeds = config.load_world_feeds()

    con = db.connect()

    # This is an interactive, on-demand CLI tool (python -m brief) -- the
    # print()s here are the progress feedback a human watches while it runs,
    # kept as-is; log.info alongside gives the same milestones a persistent
    # record for anyone who ran it non-interactively.
    print("Fetching sources…")
    log.info("fetching sources")
    raw = rss.fetch_all(sources)
    if world_feeds:
        raw += world.fetch_all(
            world_feeds, con
        )  # World Delta: state changes, not articles
    print(f"  → {len(raw)} raw items")
    log.info("%d raw items", len(raw))

    fresh = dedupe.dedupe(raw, db.seen_hashes(con))
    print(f"  → {len(fresh)} new after dedup")
    log.info("%d new after dedup", len(fresh))

    selected = score.select(fresh, profile)
    print(
        f"  → {len(selected)} cleared score ≥ {profile.get('min_score')} "
        f"(top {profile.get('max_items_to_llm')})"
    )
    log.info(
        "%d cleared score >= %s (top %s)",
        len(selected),
        profile.get("min_score"),
        profile.get("max_items_to_llm"),
    )

    print(f"Generating brief with the local model ({generate.llm.MODEL})…")
    log.info("generating brief with %s", generate.llm.MODEL)
    text = generate.generate_brief(selected, profile)

    today = date.today().isoformat()
    db.upsert_items(
        con, fresh
    )  # remember everything we saw (so we don't re-surface it)
    brief_id = db.save_brief(con, today, text, len(selected))

    out_dir = config.DATA_DIR / "briefs"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{today}.md"
    out_path.write_text(text, encoding="utf-8")

    return {
        "brief_id": brief_id,
        "selected": len(selected),
        "raw": len(raw),
        "path": str(out_path),
        "text": text,
    }
