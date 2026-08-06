"""An OPTIONAL extra "am I in a meeting" signal via Google Calendar.

The voice's primary meeting signal is mic-in-use (CoreAudio, quiet.py) --
reliable for calls made THROUGH this machine, but blind to two real cases:
(1) call audio routed through a different device (phone, a headset paired to
another machine), and (2) the exact moment a scheduled meeting starts, which
the scheduled bulletin's own :00/:30 cadence collides with by construction.
Calendar events are known in ADVANCE, so this catches both.

This module does NOT include a Google OAuth flow of its own -- wiring that up
is genuinely out of scope for a small local project. Instead, if you already
have (or build) a separate script/module exposing a read-only
`is_in_meeting_now() -> bool` (e.g. via the Google Calendar API's
calendar.readonly scope), point DISPATCH_JARVIS_SRC at the directory
containing it and this will call in. Nothing to do otherwise: with no such
module present, this is a harmless no-op and mic-in-use remains the primary,
always-available meeting signal.

Fail-soft like every other quiet.py signal: ANY problem (module missing, deps
not installed, no tokens, network/API error) returns False -- a broken
calendar check must never permanently silence the board.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_JARVIS_SRC = Path(
    os.environ.get(
        "DISPATCH_JARVIS_SRC", str(Path.home() / "path/to/your/calendar-integration/src")
    )
)


def in_meeting_now() -> bool:
    """True if the user's Google Calendar shows a meeting covering right now,
    across every account your integration has been authorized for. False on
    ANY failure -- see module docstring."""
    try:
        if str(_JARVIS_SRC) not in sys.path:
            sys.path.insert(0, str(_JARVIS_SRC))
        from jarvis import google_sync

        return google_sync.is_in_meeting_now()
    except (
        Exception
    ):  # noqa: BLE001 — fail-soft: never silence permanently on a broken check
        return False
