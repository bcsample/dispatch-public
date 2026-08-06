"""LLM behind a thin abstraction. Default = local Ollama (free, private). The rest
of the system never imports a provider directly, so escalating a single step to a
cloud model later is a config change, not a rewrite."""

from __future__ import annotations

import json
import os
import urllib.request

from . import modelroles
from .config import OLLAMA_HOST

HOST = OLLAMA_HOST
# Light by default (~6.6GB) so this plays nicely alongside other local models
# or GPU-heavy apps you might be running. Set BRIEF_MODEL to any Ollama model
# name you have pulled for more reasoning horsepower.
# BRIEF_MODEL always wins outright if set (never even asks a model registry);
# otherwise resolve role "chat.small" via modelroles (optional -- falls back
# to the default below if none is configured).
_MODEL_DEFAULT = "qwen3.5:9b"
MODEL = os.environ.get("BRIEF_MODEL") or modelroles.resolve("chat.small", _MODEL_DEFAULT)


def chat(
    system: str,
    user: str,
    model: str = "",
    temperature: float = 0.3,
    timeout: float = 240.0,
) -> str:
    """One non-streaming completion. Returns the assistant text."""
    payload = {
        "model": model or MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": False,
        "options": {"temperature": temperature},
    }
    req = urllib.request.Request(
        f"{HOST}/api/chat",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        out = json.loads(resp.read())
    return (out.get("message", {}).get("content") or "").strip()
