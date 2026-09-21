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
# Light by default (~6.6GB) so the brief doesn't co-load a 17GB model next to
# ComfyUI and trigger a macOS memory-pressure kill. Set BRIEF_MODEL=qwen3.5:27b
# for more reasoning horsepower when ComfyUI isn't loaded. qwen3.5:9b replaced
# qwen2.5:7b as the default 2026-07-27 (the operator, via the voice assistant project: newer model,
# tests better on 2026 tool-calling benchmarks at a similar footprint).
# Helm 2c: BRIEF_MODEL always wins outright if set (never even asks the host monitor); otherwise resolve role "chat.small" (cached, falls back to the
# qwen3.5:9b default below if the host monitor's unreachable).
_MODEL_DEFAULT = "qwen3.5:9b"
MODEL = os.environ.get("BRIEF_MODEL") or modelroles.resolve(
    "chat.small", _MODEL_DEFAULT
)


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
