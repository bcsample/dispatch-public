"""Optional model-role resolution against a local model-registry service.

If you run your own local orchestrator that hands out "which model handles
this role" (e.g. "chat.small", "embed") over HTTP, point BRIEF_ENGINEROOM_URL
at it and this will use it. Nobody has to run anything extra: with no such
service reachable, every call fails soft to the caller's own hardcoded
default model name (see brief/llm.py and brief/window/curate.py) — this
module is entirely optional plumbing, not a dependency.

Deliberately NOT a runtime lease: resolved once (lazily, on first use per
role), cached for the rest of the process's lifetime, and falls back to the
caller's own default on ANY failure -- including caching the fallback itself,
so a registry that's down doesn't re-pay a timeout on every call.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

# No local registry by default -- these only matter if you point
# BRIEF_ENGINEROOM_URL / BRIEF_ENGINEROOM_KEY_FILE at your own service.
_BASE_URL_DEFAULT = "http://localhost:7870"
_API_KEY_FILE_DEFAULT = ""

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
    """The model your registry says should handle `role`, or `default` if the registry
    Room is unreachable, doesn't know the role, or errors in any way."""
    if role in _cache:
        return _cache[role]
    model = default
    headers = {}
    if (key := _api_key()):
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(f"{_base_url()}/api/models/{role}", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        resolved = data.get("model")
        if isinstance(resolved, str) and resolved:
            model = resolved
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, ValueError):
        pass
    _cache[role] = model
    return model
