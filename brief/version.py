"""What code this process is actually running (weekly hygiene leg 4).

HS-1 + HS-4 (2026-09-14). This replaces the W39 sha proxy (architecture review ,
2026-09-06), which compared the sha captured at import with `.git`'s HEAD. That
proxy was wrong in both directions: it read `true` on a docs-only commit (this
repo's own specimen, 36fc552 running against 472197a with zero runtime files
changed, 2026-09-13) and `false` on a process started from a dirty tree.

Three readings are CAPTURED AT IMPORT, by the process about to run the code, and
never refreshed. That timing is the mechanism:

- `STARTED_FP`: the  +  +  fingerprint (brief/runtime_stamp.py) of the
  declared RUNTIME code only: the tracked .py files under `RUNTIME_ROOTS` (`brief/`
  plus each editable dependency's package root) and the `RUNTIME_FILES` a live process
  runs from outside a package (`scripts/dispatch_speak.py`, the speaker's entry point).
  **This is the check.** `source_stale` compares it with the same hash of disk now, so
  a tests-only or tooling-only commit reads false (HS-1c; before it, HS-2's commit
  81bb7ca read true on both live processes for 45 minutes). Both are published.
- `GIT_SHA`: the commit at spawn. **Provenance only**, what a hygiene line quotes.
  It is no longer compared to anything that decides staleness.
- `VERSION`: the repo's one semver surface, read from the `VERSION` file. Minor per
  landed milestone, patch per fix, never for a hygiene pass.

Unknown is never false. `source_stale` is None whenever either fingerprint cannot
be taken, and `source_hash` then reads "unverifiable". A False there would read as
"verified fresh" on the host monitor's dial: the zero-versus-unknown collapse this
project keeps re-finding (//).

/api/health is polled continuously and the live hash costs a `git ls-files` plus a
read of every tracked .py file, so the LIVE reading is cached for `_LIVE_TTL`
seconds. A change can take that long to show. This never restarts anything:
launchd owns respawn, the host monitor is the deliberate-restart authority; this reports.
"""

from __future__ import annotations

import hashlib
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from . import runtime_stamp

_REPO = Path(__file__).resolve().parents[1]
_GIT = _REPO / ".git"
_VERSION_FILE = _REPO / "VERSION"
_SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
_LIVE_TTL = 15.0


def _read_head() -> str | None:
    """The working tree's current HEAD sha, or None if it can't be determined."""
    try:
        head = (_GIT / "HEAD").read_text().strip()
    except OSError:
        return None
    if not head.startswith("ref: "):
        return head or None  # detached HEAD is the sha itself
    ref = head[5:].strip()
    try:
        return (_GIT / ref).read_text().strip()
    except OSError:
        pass
    # A ref that has been packed has no loose file -- normal after `git gc`,
    # and missing it would report "unknown" on a perfectly healthy repo.
    try:
        for line in (_GIT / "packed-refs").read_text().splitlines():
            if line.startswith("#"):
                continue
            parts = line.split(maxsplit=1)
            if len(parts) == 2 and parts[1].strip() == ref:
                return parts[0]
    except OSError:
        pass
    return None


def read_version(path: Path | None = None) -> str | None:
    """The semver in VERSION, or None if missing or not MAJOR.MINOR.PATCH."""
    try:
        text = (path or _VERSION_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text if _SEMVER.match(text) else None


# Captured at import: what the running process actually loaded, and when.
# pid | started_at | git_sha is the stale-runtime spec's identity triple (fable #1394).
STARTED_AT = datetime.now(timezone.utc).isoformat(timespec="seconds")
GIT_SHA = _read_head()
VERSION = read_version()
EDITABLE_ROOTS: list[Path] = runtime_stamp.editable_roots()
# . The package comes FIRST: runtime_fingerprint requires the first root to hold
# tracked code. The speaker's entry script is a single file inside an otherwise
# tooling directory, so it is declared as a file, not by hashing all of scripts/.
RUNTIME_ROOTS: list[Path] = [_REPO / "brief", *EDITABLE_ROOTS]
RUNTIME_FILES: list[Path] = [_REPO / "scripts" / "dispatch_speak.py"]


def fingerprint(
    roots: list[Path] | None = None, files: list[Path] | None = None
) -> str | None:
    """runtime_stamp.runtime_fingerprint over `roots` (the vendored function,
    unchanged), folded with the content of each runtime entry file. None if any
    part cannot be read: never a partial hash presented as a whole one."""
    roots = RUNTIME_ROOTS if roots is None else roots
    files = RUNTIME_FILES if files is None else files
    base = runtime_stamp.runtime_fingerprint(roots)
    if base is None:
        return None
    digest = hashlib.sha256(base.encode())
    for path in files:
        try:
            content = path.read_bytes()
        except OSError:
            return None
        digest.update(b"\0file:" + str(path).encode())
        digest.update(hashlib.sha256(content).digest())
    return digest.hexdigest()[:16]


STARTED_FP: str | None = fingerprint()

_live_cache: tuple[float, str | None] | None = None


def live_fingerprint() -> str | None:
    """The same fingerprint taken of disk now, cached for `_LIVE_TTL` seconds."""
    global _live_cache
    now = time.monotonic()
    if _live_cache is not None and now - _live_cache[0] < _LIVE_TTL:
        return _live_cache[1]
    fp = fingerprint()
    _live_cache = (now, fp)
    return fp


def stale(started_fp: str | None, live_fp: str | None) -> bool | None:
    """None, never False, unless BOTH readings exist."""
    if started_fp is None or live_fp is None:
        return None
    return started_fp != live_fp


def source_state() -> dict[str, object]:
    started = STARTED_FP
    live = None if started is None else live_fingerprint()
    return {
        "pid": os.getpid(),
        "started_at": STARTED_AT,
        "version": VERSION,
        "git_sha": GIT_SHA,  # provenance: what a hygiene line quotes, not the check
        "head_sha": _read_head(),
        "source_hash": started if started is not None else "unverifiable",
        "source_stale": stale(started, live),
        # : exactly what the fingerprint covers, so a reader sees what "stale"
        # is about. editable_roots () is the subset from -e installs; empty today.
        "runtime_roots": [str(r) for r in (*RUNTIME_ROOTS, *RUNTIME_FILES)],
        "editable_roots": [str(r) for r in EDITABLE_ROOTS],
    }
