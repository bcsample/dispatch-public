"""Load the editable config (profile + sources). Sources and the interest profile
are data, not code — so tuning never means a redeploy."""

from __future__ import annotations

import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
PROMPTS_DIR = ROOT / "prompts"

# Helm 2c (2026-07-27): one place instead of three (brief/llm.py,
# brief/window/curate.py, brief/window/dedup.py all had their own identical
# copy of this line).
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")


def load_dotenv(path: Path | None = None) -> None:
    """Zero-dependency .env loader: read KEY=VALUE lines from ROOT/.env into
    os.environ (without overriding anything already set), so secrets like
    NEWSAPI_KEY reach the launchd service. Missing file / bad lines are
    ignored — never a crash, never a committed secret (.env is gitignored)."""
    env_path = path or (ROOT / ".env")
    try:
        text = env_path.read_text(encoding="utf-8")
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


load_dotenv()


def load_profile() -> dict:
    """Your interests (config/profile.yaml, gitignored -- copy it from
    profile.yaml.example and edit). Absent file -> {} (no profile, no
    curation bias) rather than a crash, same "data file is optional"
    pattern as the loaders below -- a fresh checkout should run before
    you've set anything up, not demand it first."""
    path = CONFIG_DIR / "profile.yaml"
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def load_sources() -> list[dict]:
    data = yaml.safe_load((CONFIG_DIR / "sources.yaml").read_text(encoding="utf-8"))
    return data.get("sources", [])


def load_news_firehose() -> list[dict]:
    """The dashboard's broad, window-specific news roster
    (`config/news_firehose.yaml`).
    Same {name,type,trust,rss} schema as sources.yaml, deliberately a
    separate file so the curated AM digest (config/sources.yaml) is never
    touched. Falls back to load_sources() if the firehose file doesn't
    exist, so the window still has *something* to fetch."""
    path = CONFIG_DIR / "news_firehose.yaml"
    if not path.exists():
        return load_sources()
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data.get("sources", []) or []


def load_world_feeds() -> list[dict]:
    """World-delta feeds are optional: if the file is absent or empty, the
    pipeline behaves exactly as it did before this feature existed."""
    path = CONFIG_DIR / "world_feeds.yaml"
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data.get("world_feeds", []) or []


def load_prompt(name: str) -> str:
    return (PROMPTS_DIR / name).read_text(encoding="utf-8")


_WINDOW_DEFAULTS = {
    "sweep_interval_seconds": 900,
    "port": 8808,
    "host": "0.0.0.0",
    "recent_deltas_limit": 200,
    "news_interval_seconds": 600,  # v2 — how often the news loop polls RSS
    "recent_news_limit": 100,  # v2 — how many cached headlines /api/news returns by default
    "news_window_hours": 2,  # v3 — /api/news only shows items published within this window
    # v4.1 — near-duplicate headline clustering via nomic-embed-text (Ollama).
    # Calibrated 2026-07-16: known-duplicate pairs measured 0.81-0.91 cosine
    # similarity, unrelated pairs measured 0.35-0.41 — 0.80 sits cleanly
    # between them. Runs on the news-fetch cadence only, never the 10s poll.
    "dedup_enabled": True,
    "dedup_similarity": 0.80,
}


def load_window_config() -> dict:
    """Dashboard service config — sweep cadence, bind host/port. Data, not code:
    an absent file or absent keys fall back to the documented defaults."""
    path = CONFIG_DIR / "window.yaml"
    if not path.exists():
        return dict(_WINDOW_DEFAULTS)
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {**_WINDOW_DEFAULTS, **data}
