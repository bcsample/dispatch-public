"""Entry point: `python -m brief.window`.

Starts the dashboard service — the sweep loop over the World Delta engine
plus the FastAPI dashboard — bound to 0.0.0.0 by default so it's reachable
from other devices on your network if you want that (see host/port in
config/window.yaml). Read-only: no auth token, since there's nothing here
that mutates state.
"""

from __future__ import annotations

import os

import uvicorn

from .. import config
from .app import create_app


def main() -> None:
    world_feeds = config.load_world_feeds()
    # v3 — the window's own broad firehose roster (config/news_firehose.yaml),
    # not the curated AM-digest sources.yaml's
    # "v3" section: "Window-specific roster, don't touch the AM brief").
    news_sources = config.load_news_firehose()
    window_cfg = config.load_window_config()
    app = create_app(world_feeds, window_cfg, news_sources=news_sources)
    host = window_cfg.get("host", "0.0.0.0")
    port = int(window_cfg.get("port", 8808))
    # Fable's full-log scan (2026-07-28): dispatch.out was growing ~24MB
    # largely from per-poll uvicorn access-log INFO lines (the board polls
    # several endpoints every 10s) -- redundant now that brief.log (Helm 3b)
    # carries the real diagnostics. Off by default; DISPATCH_ACCESS_LOG=1 to
    # turn it back on for debugging without a code change.
    access_log = os.environ.get("DISPATCH_ACCESS_LOG", "0") == "1"
    uvicorn.run(app, host=host, port=port, log_level="info", access_log=access_log)


if __name__ == "__main__":
    main()
