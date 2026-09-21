"""the host monitor model-role resolution (Helm 2c, 2026-07-27 — ML1 adoption).

Ask the host monitor which model handles a ROLE (e.g. "chat.small", "embed")
instead of hardcoding a model name — see
the render host/ECOSYSTEM_HELM_PLAN_2026-07-27.md §1.2/§2c. Donor: this is
a direct port of the voice assistant project's modelroles.py (jarvis already adopted the
"vision" role this same way) — same contract, same fail-soft guarantees, so
a future consumer copying either file gets identical behavior.

Deliberately NOT a runtime lease: resolved once (lazily, on first use per
role), cached for the rest of the process's lifetime, and falls back to the
caller's own default on ANY failure. the host monitor being down must never stop
the brief from starting or working — caching the FALLBACK too, not just a
successful resolve, is what keeps that true: without it, every call while
the host monitor is down would re-pay the timeout.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

_BASE_URL_DEFAULT = "http://100.115.16.42:7870"
_API_KEY_FILE_DEFAULT = "/PATH/TO/the render host/api_key.token"

_cache: dict[str, str] = {}


def _base_url() -> str:
    return os.environ.get("BRIEF_ENGINEROOM_URL", _BASE_URL_DEFAULT).rstrip("/")


def _api_key() -> str | None:
    path = os.environ.get("BRIEF_ENGINEROOM_KEY_FILE", _API_KEY_FILE_DEFAULT)
    try:
        key = Path(path).read_text(encoding="utf-8").strip()
        return key or None
    except OSError:
        return None


def resolve(role: str, default: str, *, timeout: float = 3.0) -> str:
    """The model the host monitor says should handle `role`, or `default` if the host monitor is unreachable, doesn't know the role, or errors in any way."""
    if role in _cache:
        return _cache[role]
    model = default
    headers = {}
    if key := _api_key():
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(f"{_base_url()}/api/models/{role}", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        resolved = data.get("model")
        if isinstance(resolved, str) and resolved:
            model = resolved
    except (
        urllib.error.URLError,
        TimeoutError,
        OSError,
        json.JSONDecodeError,
        ValueError,
    ):
        pass
    _cache[role] = model
    return model
