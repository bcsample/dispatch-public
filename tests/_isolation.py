"""Shared isolation checks for tests/conftest.py (HS-2), kept importable so their
own birth tests exercise the exact code the autouse fixtures run."""

from __future__ import annotations

import threading


def leftover_threads(before: set[threading.Thread], grace: float = 2.0) -> list[str]:
    """Names of threads started since `before` that are still alive after `grace`
    seconds of joining. Empty = nothing outlived the test."""
    leftovers = [t for t in threading.enumerate() if t not in before and t.is_alive()]
    for t in leftovers:
        t.join(timeout=grace)
    return [f"{t.name} (daemon={t.daemon})" for t in leftovers if t.is_alive()]
