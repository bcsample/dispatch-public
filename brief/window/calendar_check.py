"""An extra "is the operator in a meeting" signal: his Google Calendar, via
Dispatch's own read-only OAuth credential (P1-C, 2026-08-16).

Why this exists (the operator, 2026-07-21): the voice's primary meeting signal is
mic-in-use (CoreAudio, quiet.py) -- reliable for calls made THROUGH this
machine, but blind to two real cases: (1) call audio routed through a
different device (phone, a headset paired to another machine), and (2) the
exact moment a scheduled meeting starts, which the scheduled bulletin's own
:00/:30 cadence collides with by construction. Calendar events are known in
ADVANCE, so this catches both.

Originally borrowed the voice assistant project's OAuth token via a sys.path shim into its
src/ (see git history) rather than making the operator re-authorize a second app.
Replaced with Dispatch's own separate OAuth client + encrypted token
(brief/window/google_sync.py, brief/secretbox.py, own Keychain entry) so
Dispatch no longer depends on the voice assistant project's repo layout or credentials --
least privilege, and one less cross-project coupling.

Fail-soft like every other quiet.py signal: ANY problem (no token yet, deps
not installed, network/API error) returns False -- a broken calendar check
must never permanently silence the board. mic-in-use remains the primary,
always-available meeting signal regardless of this one's health.
"""

from __future__ import annotations


def in_meeting_now() -> bool:
    """True if the operator's Google Calendar shows a meeting covering right now.
    False on ANY failure -- see module docstring."""
    try:
        from . import google_sync

        return google_sync.is_in_meeting_now()
    except Exception:  # noqa: BLE001 — fail-soft: never silence permanently on a broken check
        return False
