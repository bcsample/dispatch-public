"""Entry point: `python -m brief.window`.

Starts the Open Window (v1) service — the sweep loop over the existing World
Delta engine plus the FastAPI dashboard. Binds loopback and relies on
`a private-network proxy --https=8808` for private network reach (project bind convention,
architecture review /#539, 2026-08-16) -- never 0.0.0.0. v1 is read-only, so no
auth token is needed for the loopback bind itself (see
WORLD_DELTA_BUILD_PLAN.md's "Open Window (v1)" section).
"""

from __future__ import annotations

import os

import uvicorn

from .. import config
from .app import create_app


def main() -> None:
    world_feeds = config.load_world_feeds()
    # v3 — the window's own broad firehose roster (config/news_firehose.yaml),
    # not the curated AM-digest sources.yaml (see WORLD_DELTA_BUILD_PLAN.md's
    # "v3" section: "Window-specific roster, don't touch the AM brief").
    news_sources = config.load_news_firehose()
    window_cfg = config.load_window_config()
    app = create_app(world_feeds, window_cfg, news_sources=news_sources)
    # Typed-out escape hatch only -- never arrived at by a failed lookup
    # (same reasoning as a sibling project's OVERWATCH_BIND). config/window.yaml's
    # own default is loopback; this just lets a deliberate operator choice
    # override it without editing YAML.
    host = os.environ.get("DISPATCH_BIND_HOST") or window_cfg.get("host", "127.0.0.1")
    port = int(window_cfg.get("port", 8808))
    # the architecture review's full-log scan (2026-07-28): dispatch.out was growing ~24MB
    # largely from per-poll uvicorn access-log INFO lines (the board polls
    # several endpoints every 10s) -- redundant now that brief.log (Helm 3b)
    # carries the real diagnostics. Off by default; DISPATCH_ACCESS_LOG=1 to
    # turn it back on for debugging without a code change.
    access_log = os.environ.get("DISPATCH_ACCESS_LOG", "0") == "1"
    uvicorn.run(app, host=host, port=port, log_level="info", access_log=access_log)


if __name__ == "__main__":
    main()
