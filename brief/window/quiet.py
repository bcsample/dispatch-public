"""When the Dispatch voice must stay silent.

the operator's rule: it must never read anything out while he's in a meeting. Layered
signals, cheapest and most reliable first:

1. **Manual mute** — a file (data/speaker_mute). An explicit "shut up" switch
   that always wins: `touch data/speaker_mute` / `rm` it to re-enable.
2. **Microphone in use** — the strongest meeting signal, via CoreAudio's
   kAudioDevicePropertyDeviceIsRunningSomewhere on the default input device.
   Catches Zoom / Teams / Meet / Webex / FaceTime / phone calls alike, with no
   per-app allowlist and no special permission.
3. **Focus / Do Not Disturb active** — if he's set any Focus, respect it
   (~/Library/DoNotDisturb/DB/Assertions.json holds active assertions).
4. **A calendar meeting is in progress** — Google Calendar via Dispatch's OWN
   read-only credential (P1-C, calendar_check.py; it used to borrow
   the voice assistant project's tokens over a sys.path shim, and this line still said so
   for a while after that stopped being true),
   for the cases mic-in-use can't see: call audio routed through a different
   device, and the exact instant a scheduled meeting starts (which the
   bulletin's own :00/:30 cadence collides with by construction).
5. **Quiet hours** — the existing overnight window.

Every check is fail-soft: if a signal can't be read we assume it is NOT
triggered, so a broken check never silences the board permanently — except the
mute file, which is a deliberate user action.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import json
import subprocess
import time
from datetime import datetime
from pathlib import Path

# --- 1. manual mute ---------------------------------------------------------


def manual_mute(mute_path: Path) -> bool:
    try:
        return mute_path.exists()
    except OSError:
        return False


# --- 2. microphone in use (CoreAudio) ---------------------------------------


def _fourcc(s: str) -> int:
    return int.from_bytes(s.encode(), "big")


class _AOPA(ctypes.Structure):
    _fields_ = [
        ("mSelector", ctypes.c_uint32),
        ("mScope", ctypes.c_uint32),
        ("mElement", ctypes.c_uint32),
    ]


def mic_in_use() -> bool:
    """True when the default input device is running somewhere — i.e. some app
    has the mic open, which in practice means a call/meeting is live."""
    try:
        lib = ctypes.util.find_library("CoreAudio")
        if not lib:
            return False
        ca = ctypes.cdll.LoadLibrary(lib)
        glob = _fourcc("glob")

        dev = ctypes.c_uint32(0)
        size = ctypes.c_uint32(4)
        addr = _AOPA(_fourcc("dIn "), glob, 0)
        # 1 == kAudioObjectSystemObject
        if (
            ca.AudioObjectGetPropertyData(
                ctypes.c_uint32(1),
                ctypes.byref(addr),
                0,
                None,
                ctypes.byref(size),
                ctypes.byref(dev),
            )
            != 0
            or dev.value == 0
        ):
            return False

        running = ctypes.c_uint32(0)
        size2 = ctypes.c_uint32(4)
        addr2 = _AOPA(_fourcc("gone"), glob, 0)  # DeviceIsRunningSomewhere
        if (
            ca.AudioObjectGetPropertyData(
                dev,
                ctypes.byref(addr2),
                0,
                None,
                ctypes.byref(size2),
                ctypes.byref(running),
            )
            != 0
        ):
            return False
        return bool(running.value)
    except Exception:  # noqa: BLE001 — fail-soft: never silence on a broken check
        return False


# --- 3. Focus / Do Not Disturb ----------------------------------------------

_DND_ASSERTIONS = Path.home() / "Library/DoNotDisturb/DB/Assertions.json"


def focus_active(path: Path | None = None) -> bool:
    """True when a Focus/DND mode is currently asserted. The file keeps both
    active assertions (storeAssertionRecords) and historical invalidations;
    only a non-empty active list means Focus is ON right now."""
    p = path or _DND_ASSERTIONS
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        for entry in data.get("data") or []:
            if entry.get("storeAssertionRecords"):
                return True
        return False
    except Exception:  # noqa: BLE001 — fail-soft
        return False


# --- 4. a calendar meeting is in progress ------------------------------------


def calendar_meeting_active() -> bool:
    """True if the operator's Google Calendar shows a meeting covering right now.
    Thin wrapper over calendar_check.in_meeting_now() (its own module so the
    cross-repo sys.path reuse of the voice assistant project's Google auth stays isolated
    and independently testable). Fail-soft: see calendar_check's docstring."""
    from . import calendar_check

    return calendar_check.in_meeting_now()


# --- 5. Jarvis has the voice -------------------------------------------------


def jarvis_has_voice(claim_path: Path, max_age_seconds: float = 120) -> bool:
    """True when the voice assistant project is currently claiming the voice.

    Jarvis is intentionally intermittent (it must never fight ComfyUI for
    memory), so it can't be the only voice. The handoff is a heartbeat file:
    while Jarvis is up and speaking it touches `claim_path` at least once a
    minute; this light `say` speaker defers while that file is fresh and
    automatically resumes when it goes stale. No ports, no coupling, survives
    a restart on either side. See JARVIS_VOICE_HANDOFF.md."""
    try:
        age = time.time() - claim_path.stat().st_mtime
        return age <= max_age_seconds
    except OSError:
        return False


# --- 6. nobody's at the machine (display asleep) -----------------------------


def display_idle_seconds() -> float:
    """Seconds since the last keyboard/mouse/trackpad input, system-wide --
    the standard macOS HIDIdleTime trick via ioreg (reported in
    nanoseconds). Doesn't depend on which app has focus or whether the
    display has literally powered off (IOKit's display-power class names
    vary across macOS/hardware generations, so idle time is the portable
    signal, not a raw power-state query). Fail-soft: unreadable/unparseable
    -> 0.0, same rule as every other signal in this module -- a broken
    check must never silence the board."""
    try:
        out = subprocess.run(
            ["ioreg", "-c", "IOHIDSystem"], capture_output=True, text=True, timeout=3
        ).stdout
        for line in out.splitlines():
            if "HIDIdleTime" in line:
                return int(line.rsplit("=", 1)[-1].strip()) / 1_000_000_000
    except Exception:  # noqa: BLE001
        pass
    return 0.0


def display_probably_off(idle_minutes: float) -> bool:
    """True once the machine has been genuinely untouched long enough that
    the monitor has almost certainly gone to sleep -- same idea as
    dispatch-shell's DAYTIME_IDLE_LIMIT_SECONDS (20 min default there),
    just a separate process/language so it can't share state directly.
    the operator, 2026-08-04: "stop wasting compute... I don't need to hear odd
    voices from the basement" -- nobody's there to hear a scheduled
    bulletin if nobody's touched the machine in a while."""
    return display_idle_seconds() >= idle_minutes * 60


# --- 7. quiet hours + the combined decision ---------------------------------


def in_quiet_hours(now: datetime, start_hour: int, end_hour: int) -> bool:
    """Overnight window, e.g. 23 -> 8 wraps midnight."""
    if start_hour == end_hour:
        return False
    if start_hour > end_hour:  # wraps midnight
        return now.hour >= start_hour or now.hour < end_hour
    return start_hour <= now.hour < end_hour


def quiet_reason(
    now: datetime,
    mute_path: Path,
    quiet_start: int = 23,
    quiet_end: int = 8,
    respect_focus: bool = True,
    respect_mic: bool = True,
    jarvis_claim_path: Path | None = None,
    weekdays_only: bool = False,
    respect_calendar: bool = True,
    respect_display_idle: bool = False,
    display_idle_minutes: float = 20.0,
) -> str | None:
    """Why THIS speaker should stay silent right now, or None if it may speak."""
    if manual_mute(mute_path):
        return "muted"
    if respect_mic and mic_in_use():
        return "in a meeting (microphone in use)"
    if respect_focus and focus_active():
        return "Focus/Do Not Disturb is on"
    if respect_calendar and calendar_meeting_active():
        return "a calendar meeting is in progress"
    if jarvis_claim_path is not None and jarvis_has_voice(jarvis_claim_path):
        return "Jarvis has the voice"
    if weekdays_only and now.weekday() >= 5:  # 5=Sat, 6=Sun
        return "weekend"
    if in_quiet_hours(now, quiet_start, quiet_end):
        return "quiet hours"
    # Deliberately checked LAST: the cheaper/more-certain meeting/mute/focus
    # signals above should win on their own merits before this coarser one
    # even runs a subprocess. Off by default (respect_display_idle=False) --
    # only the scheduled bulletin opts in; a manual click on the board
    # already proves someone's at the machine, so this must never gate that
    # path (see api_voice_read_news in app.py).
    if respect_display_idle and display_probably_off(display_idle_minutes):
        return "no one's at the machine"
    return None
