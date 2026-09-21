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
    return yaml.safe_load((CONFIG_DIR / "profile.yaml").read_text(encoding="utf-8"))


def load_sources() -> list[dict]:
    data = yaml.safe_load((CONFIG_DIR / "sources.yaml").read_text(encoding="utf-8"))
    return data.get("sources", [])


def load_news_firehose() -> list[dict]:
    """v3 — the Open Window's broad, window-specific news roster
    (`config/news_firehose.yaml`, WORLD_DELTA_BUILD_PLAN.md's "v3" section).
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
    # Loopback + `a private-network proxy`, never 0.0.0.0 -- project bind convention
    # (architecture review /#539, 2026-08-16). Dispatch was the ONLY service in the
    # project bound to every interface: reachable from any device on the
    # LAN, not merely the private network, for a daily intel brief. DISPATCH_BIND_HOST
    # is a typed-out escape hatch (never arrived at by a failed lookup, same
    # reasoning as a sibling project's OVERWATCH_BIND).
    "host": "127.0.0.1",
    "recent_deltas_limit": 200,
    "news_interval_seconds": 600,  # v2 — how often the news loop polls RSS
    # v2 — how many cached headlines /api/news returns by default
    "recent_news_limit": 100,
    # v3 — /api/news only shows items published within this window
    "news_window_hours": 2,
    # v4.1 — near-duplicate headline clustering via nomic-embed-text (Ollama).
    # Calibrated 2026-07-16: known-duplicate pairs measured 0.81-0.91 cosine
    # similarity, unrelated pairs measured 0.35-0.41 — 0.80 sits cleanly
    # between them. Runs on the news-fetch cadence only, never the 10s poll.
    "dedup_enabled": True,
    "dedup_similarity": 0.80,
}


def load_window_config() -> dict:
    """Open Window (v1) service config — sweep cadence, bind host/port (see
    WORLD_DELTA_BUILD_PLAN.md's "Open Window (v1)" section). Data, not code:
    an absent file or absent keys fall back to the documented v1 defaults."""
    path = CONFIG_DIR / "window.yaml"
    if not path.exists():
        return dict(_WINDOW_DEFAULTS)
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {**_WINDOW_DEFAULTS, **data}
