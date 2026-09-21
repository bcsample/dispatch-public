"""HS-5: what is the speaker daemon actually running? One line for the hygiene report.

The speaker has no endpoint. It writes data/speaker_stamp.json at start and on
every poll (scripts/dispatch_speak.py). This reads that stamp and checks it against
launchd's own row, because a stamp alone can outlive its process:

  * launchd row missing           -> FAIL (not installed / label wrong)
  * launchd pid "-" (not running) -> "stopped", exit 0: window-gated, healthy
  * stamp missing or unreadable   -> FAIL (a running speaker that never stamped
                                     is running pre-HS-5 code)
  * stamp pid != launchd pid      -> FAIL (stamp is from an earlier run)
  * last poll older than 3 polls  -> FAIL (alive pid, dead loop)
  * stamp source_hash != disk now -> reported as source_stale true (C77b fingerprint,
                                     HS-1; not a failure by itself). Unknown on either
                                     side reads `none`, never false. git_sha is shown
                                     as provenance only.

launchctl list columns are TABS (T-51, 2026-09-13: a space-separated grep matched
nothing, parsed an empty pid and printed STABLE). The row is found by exact label
on a tab split, and an empty pid is a hard failure, never a match.
Read-only: never starts, stops or signals anything.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from brief import version  # noqa: E402

LABEL = "com.local.dispatch-speaker"
STAMP_PATH = ROOT / "data" / "speaker_stamp.json"
STALE_POLLS = 3


def launchd_row(label: str, listing: str) -> tuple[str, str] | None:
    """(pid, last exit status) for `label` from `launchctl list` output, or None.
    pid is "-" when the job is loaded but not running."""
    for line in listing.splitlines():
        cols = line.split("\t")
        if len(cols) == 3 and cols[2] == label:
            return cols[0].strip(), cols[1].strip()
    return None


def _launchctl_list() -> str:
    return subprocess.run(
        ["launchctl", "list"], capture_output=True, text=True, check=True
    ).stdout


def status(
    listing: str,
    stamp_path: Path,
    live_fp: str | None,
    now: datetime | None = None,
) -> tuple[bool, str]:
    """(ok, one-line report). Pure on its inputs so tests drive every branch."""
    now = now or datetime.now(timezone.utc)
    row = launchd_row(LABEL, listing)
    if row is None:
        return False, f"speaker FAIL: no launchd row for {LABEL}"
    pid, last_exit = row
    if pid == "-":
        return (
            True,
            f"speaker stopped (launchd loaded, not running; last exit {last_exit})",
        )
    if not pid.isdigit():
        return False, f"speaker FAIL: unparseable launchd pid {pid!r}"
    try:
        stamp = json.loads(stamp_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return False, (
            f"speaker FAIL: pid {pid} running but stamp unreadable "
            f"({exc.__class__.__name__}); pre-HS-5 code or a write failure"
        )
    if str(stamp.get("pid")) != pid:
        return False, (
            f"speaker FAIL: launchd pid {pid} but stamp pid {stamp.get('pid')} "
            "(stamp is from an earlier run)"
        )
    sha = stamp.get("git_sha")
    stale = version.stale(stamp.get("source_hash"), live_fp)
    poll_seconds = int(stamp.get("poll_seconds") or 60)
    last = stamp.get("last_poll_at")
    if last is None:
        started = datetime.fromisoformat(stamp["started_at"])
        age = (now - started).total_seconds()
        if age > STALE_POLLS * poll_seconds:
            return (
                False,
                f"speaker FAIL: pid {pid} started {age:.0f}s ago and never polled",
            )
        poll_text = "no poll yet"
    else:
        age = (now - datetime.fromisoformat(last)).total_seconds()
        if age > STALE_POLLS * poll_seconds:
            return (
                False,
                f"speaker FAIL: pid {pid} last poll {age:.0f}s ago (loop hung)",
            )
        ok_text = (
            "ok" if stamp.get("last_poll_ok") else f"FAILED {stamp.get('last_error')}"
        )
        poll_text = f"last poll {age:.0f}s ago {ok_text}"
    line = (
        f"speaker pid {pid} started {stamp.get('started_at')} version "
        f"{stamp.get('version') or 'unknown'} running {(sha or 'unknown')[:7]} "
        f"source_hash {stamp.get('source_hash') or 'unverifiable'} "
        f"source_stale {str(stale).lower()} | {poll_text} | "
        f"python {stamp.get('python_version')} sqlite {stamp.get('sqlite_version')}"
    )
    return True, line


def main() -> int:
    # The reader runs under the same .venv interpreter as the daemon, so its
    # editable roots are the daemon's; the live hash is of disk now.
    ok, line = status(_launchctl_list(), STAMP_PATH, version.live_fingerprint())
    print(line)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
