"""Run an on-demand deep-dive brief:  python -m brief  (generate + print + save).

Never scheduled, never emails: the Apps Script Morning Digest owns daily delivery
(README § Dispatch Integration, architecture review ADR 2026-07-16)."""

import sys

from . import pipeline

if __name__ == "__main__":
    if "--deliver" in sys.argv:
        sys.exit(
            "--deliver was retired: the Apps Script Morning Digest owns email delivery "
            "(see README § Dispatch Integration)."
        )
    result = pipeline.run_daily_brief()
    print("\n" + "=" * 70)
    print(result["text"])
    print("=" * 70)
    print(
        f"\nsaved: {result['path']}  "
        f"({result['selected']} items from {result['raw']} fetched)"
    )
