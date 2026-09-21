"""SQLite storage — the right choice for one user, local: zero-config, durable,
and it won't need replacing at this scale. Holds processed items (for dedup +
'seen before'), the generated briefs, and (later) an embeddings table for memory.
"""

from __future__ import annotations

import sqlite3

from . import applog
from .config import DATA_DIR
from .models import Item

log = applog.get(__name__)

DB_PATH = DATA_DIR / "brief.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    content_hash TEXT PRIMARY KEY,
    source_name TEXT, source_type TEXT, title TEXT, url TEXT,
    published_at TEXT, retrieved_at TEXT, raw_text TEXT, trust TEXT,
    summary TEXT, topics TEXT, entities TEXT,
    relevance_score INTEGER, user_reason TEXT, recommended_action TEXT,
    first_seen TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS briefs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    brief_date TEXT, created_at TEXT DEFAULT (datetime('now')),
    item_count INTEGER, text TEXT
);
CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content_hash TEXT, signal TEXT, created_at TEXT DEFAULT (datetime('now'))
);
-- embeddings table reserved for the memory layer (next increment)
CREATE TABLE IF NOT EXISTS embeddings (
    content_hash TEXT PRIMARY KEY,
    vector BLOB,
    created_at TEXT DEFAULT (datetime('now'))
);
-- world delta: last-seen state per feed record, so the ingestor can diff sweeps
CREATE TABLE IF NOT EXISTS feed_snapshots (
    feed_name TEXT, record_key TEXT, value_hash TEXT, payload TEXT,
    snapshot_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (feed_name, record_key)
);
-- Dispatch news: a SMALL rolling window of headlines (not an archive). Keyed by
-- url so re-seeing a story keeps its original first_seen; that first_seen is the
-- whole point — it's how the board knows a story is genuinely NEW vs. still
-- scrolling from an hour ago. prune_news() trims it to a short retention each
-- fetch so it never grows into a warehouse.
CREATE TABLE IF NOT EXISTS news_articles (
    url TEXT PRIMARY KEY,
    title TEXT, source_name TEXT, published_at TEXT,
    place TEXT, lat REAL, lon REAL,
    beat_score INTEGER,
    first_seen TEXT DEFAULT (datetime('now'))
);
-- the status poll filters world items by (source_type, first_seen) every 10s
CREATE INDEX IF NOT EXISTS idx_items_type_seen ON items (source_type, first_seen);
"""

# Additive migration for columns added to `items` after the table already existed
# in the wild. Guarded by PRAGMA table_info so re-running is a no-op; an
# unexpected ALTER failure (not "already exists") is logged and re-raised —
# never silently swallowed.
_ITEM_COLUMN_ADDITIONS = {
    "lat": "REAL",
    "lon": "REAL",
    "severity": "REAL",
    "delta_kind": "TEXT",
}


def _migrate_columns(con: sqlite3.Connection, table: str, cols: dict) -> None:
    existing = {row[1] for row in con.execute(f"PRAGMA table_info({table})").fetchall()}
    for col, coltype in cols.items():
        if col in existing:
            continue
        try:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")
        except sqlite3.OperationalError as exc:
            log.error("migration FAILED adding %s.%s: %r", table, col, exc)
            raise
    con.commit()


def _migrate_items_columns(con: sqlite3.Connection) -> None:
    _migrate_columns(con, "items", _ITEM_COLUMN_ADDITIONS)
    # beat_score persists per-URL so a skipped curation cycle (ComfyUI busy)
    # doesn't un-filter the map — the last known score sticks.
    _migrate_columns(con, "news_articles", {"beat_score": "INTEGER"})


# Schema is created once per DB path, not on every connect (uvicorn's
# threadpool + the sweep loop open many connections per 10s poll). Keyed on the
# path string so tests, which point DB_PATH at a fresh tmp dir each test, still
# get their schema created.
_SCHEMA_READY: set[str] = set()


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    # WAL lets readers (the 10s poll) proceed while the sweep holds a long write
    # (the OFAC diff writes ~19k snapshot rows before one commit); busy_timeout
    # waits instead of raising "database is locked".
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=5000")
    key = str(DB_PATH)
    if key not in _SCHEMA_READY:
        con.executescript(_SCHEMA)
        _migrate_items_columns(con)
        _SCHEMA_READY.add(key)
    return con


def seen_hashes(con: sqlite3.Connection) -> set[str]:
    return {r[0] for r in con.execute("SELECT content_hash FROM items")}


def upsert_items(con: sqlite3.Connection, items: list[Item]) -> None:
    rows = [it.to_row() for it in items]
    if not rows:
        return
    cols = list(rows[0].keys())
    placeholders = ",".join(f":{c}" for c in cols)
    con.executemany(
        f"INSERT OR REPLACE INTO items ({','.join(cols)}) VALUES ({placeholders})", rows
    )
    con.commit()


def upsert_news(con: sqlite3.Connection, articles: list[dict]) -> None:
    """Insert headlines into the rolling news window. INSERT OR IGNORE (not
    REPLACE): a story we've seen before keeps its original first_seen, so
    "new-ness" is preserved across fetches. Skips rows with no url (the primary
    key). Fail-soft is the caller's job — the news loop must survive a bad DB."""
    rows = [
        {
            "url": a.get("url"),
            "title": a.get("title"),
            "source_name": a.get("source_name"),
            "published_at": a.get("published_at"),
            "place": a.get("place"),
            "lat": a.get("lat"),
            "lon": a.get("lon"),
        }
        for a in articles
        if a.get("url")
    ]
    if not rows:
        return
    con.executemany(
        "INSERT OR IGNORE INTO news_articles "
        "(url, title, source_name, published_at, place, lat, lon) "
        "VALUES (:url, :title, :source_name, :published_at, :place, :lat, :lon)",
        rows,
    )
    con.commit()


def prune_news(con: sqlite3.Connection, keep_hours: float = 48) -> int:
    """Trim the news window to the last `keep_hours` (by first_seen). Keeps the
    table small and rolling — this is a live window, not a historical archive.
    Returns the row count deleted."""
    cur = con.execute(
        "DELETE FROM news_articles WHERE first_seen < datetime('now', ?)",
        (f"-{keep_hours} hours",),
    )
    con.commit()
    return cur.rowcount


def news_first_seen_map(con: sqlite3.Connection) -> dict[str, str]:
    """url -> first_seen for everything currently in the rolling window, so the
    news loop can stamp each headline with when we first saw it."""
    return {
        r["url"]: r["first_seen"]
        for r in con.execute("SELECT url, first_seen FROM news_articles")
    }


def set_beat_scores(con: sqlite3.Connection, scores: dict[str, int]) -> None:
    """Persist per-URL curation scores so they survive a skipped/failed cycle."""
    rows = [(int(s), url) for url, s in scores.items() if url and s is not None]
    if not rows:
        return
    con.executemany("UPDATE news_articles SET beat_score = ? WHERE url = ?", rows)
    con.commit()


def news_beat_map(con: sqlite3.Connection) -> dict[str, int]:
    """url -> last known beat_score, so the news loop can re-apply scores when
    curation skipped this cycle."""
    return {
        r["url"]: r["beat_score"]
        for r in con.execute(
            "SELECT url, beat_score FROM news_articles WHERE beat_score IS NOT NULL"
        )
    }


def save_brief(
    con: sqlite3.Connection, brief_date: str, text: str, item_count: int
) -> int:
    cur = con.execute(
        "INSERT INTO briefs (brief_date, item_count, text) VALUES (?,?,?)",
        (brief_date, item_count, text),
    )
    con.commit()
    return cur.lastrowid


def latest_brief(con: sqlite3.Connection) -> str | None:
    row = con.execute("SELECT text FROM briefs ORDER BY id DESC LIMIT 1").fetchone()
    return row[0] if row else None
