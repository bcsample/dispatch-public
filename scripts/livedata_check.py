"""HS-2: does the test suite leave the operator's live data untouched? (, , , )

The live engine and speaker write `data/` continuously, so "nothing changed" is the
wrong question. This asks two sharper ones over a window, and compares a window in
which the suite ran against a CONTROL window of the same length in which it did not:

1. Did anything change that no live process writes?  Files outside LIVE_WRITERS, and
   database tables outside LIVE_TABLES, must not change while the suite runs. A change
   seen in BOTH windows is an undeclared live writer: reported as WARN, not blamed on
   the suite.
2. Did any suite FINGERPRINT land anywhere?  Needles are harvested from the tests
   themselves (exception messages, fake URLs, markers) and searched for in the log
   lines appended during the window, in every text column of every table that changed,
   and in every changed file. Plus a structural rule: a `brief.test_*` logger anywhere,
   or the speaker's `brief.dispatch_speak` logger in the ENGINE's brief.log, is a test
   line by construction (the speaker logs to speaker.log since HS-5).

SQLite is compared LOGICALLY. `data/brief.db` is WAL: a committed write with the writer
still open can leave the .db bytes identical, and a mere reader creates -wal/-shm
sidecars (dispatch #1302). So each table is reduced to (row count, hash of its rows in
a stable order) through a read-only connection, and sidecars are excluded from file
hashes.

Usage:
  .venv/bin/python scripts/livedata_check.py run [--control SECONDS] -- <pytest args>
  .venv/bin/python scripts/livedata_check.py history
Exit 0 = clean, 1 = a violation, 2 = the check could not run (empty or unreadable
tree): a check that examined nothing did not pass (). It reads live data and
writes nothing to data/; the pytest run it wraps is the only thing that could.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import re
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
TESTS = ROOT / "tests"

SQLITE_SIDECARS = ("-wal", "-shm", "-journal")
# Files a RUNNING process legitimately writes (engine, speaker, keyword page).
LIVE_WRITERS = (
    "logs/",
    "speaker_stamp.json",
    "speaker_seen.json",
    "google_news_keywords.json",
    "google_news_feeds.json",
)
# Tables the running engine writes every cycle.
LIVE_TABLES = frozenset({"feed_snapshots", "items", "news_articles", "sqlite_sequence"})
# Loggers that can only appear in a given file if a test wrote them.
FOREIGN_LOGGERS = {
    "brief.log": re.compile(r"\sbrief\.(dispatch_speak|test_\w+)\s"),
    "speaker.log": re.compile(r"\sbrief\.test_\w+\s"),
}
# Contamination already recorded on the record (HELM_ITEMS HS-2, 2026-09-14), left in
# a file a running process appends to rather than edited out. Matched exactly; they
# report as KNOWN, never as clean, and drop out when brief.log rotates past them.
KNOWN_HITS = frozenset(
    {
        "2026-09-14 11:06:58  INFO    brief.dispatch_speak  bulletin skipped — in a "
        "meeting (microphone in use) (1 item(s))",
        "2026-09-14 11:07:19  ERROR   brief.gdelt  GDELT fetch FAILED "
        "(ConnectionError('gdelt down'))",
    }
)
_NEEDLE_MIN = 6
_FAKE_HOST = re.compile(r"^https?://([a-z0-9-]+|[^/]*\.(test|example|invalid))(/|$)")
_ANY_URL = re.compile(r"https?://[^\s'\")]+")


def _real_url(text: str) -> bool:
    """True if `text` carries a URL that is not a fake test host. A test that
    transcribes a REAL error (the Event Registry 403) must not become a needle:
    the same text is in the live log for real."""
    return any(not _FAKE_HOST.match(u) for u in _ANY_URL.findall(text))


def _needle_in(needle: str, line: str) -> bool:
    """URL needles must end at a token boundary, so `http://a` never matches
    inside `http://apnews.com`."""
    if not needle.startswith(("http://", "https://")):
        return needle in line
    start = line.find(needle)
    while start != -1:
        end = start + len(needle)
        if end == len(line) or not re.match(r"[A-Za-z0-9.\-_]", line[end]):
            return True
        start = line.find(needle, start + 1)
    return False


# --- needles ---------------------------------------------------------------------


def harvest_needles(tests_dir: Path = TESTS) -> set[str]:
    """Distinctive strings that only a test would write:
    - messages passed to an exception constructor (`ConnectionError("gdelt down")`),
    - URLs whose host is dotless or .test/.example/.invalid (`http://r/1`),
    - any literal containing "marker" or "fixture".
    Localhost URLs are excluded: the live processes use them."""
    needles: set[str] = set()
    for path in sorted(tests_dir.glob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", "")
                if name.endswith(("Error", "Exception")):
                    for arg in node.args:
                        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                            if len(arg.value) >= _NEEDLE_MIN and not _real_url(
                                arg.value
                            ):
                                needles.add(arg.value)
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                v = node.value
                if "localhost" in v or "127.0.0.1" in v:
                    continue
                if _FAKE_HOST.match(v) and len(v) >= _NEEDLE_MIN:
                    needles.add(v)
                elif ("marker" in v.lower() or "fixture" in v.lower()) and (
                    _NEEDLE_MIN <= len(v) <= 80 and "\n" not in v
                ):
                    needles.add(v)
    return needles


def scan_text(text: str, needles: set[str], log_name: str | None = None) -> list[str]:
    """Hits as 'line N: <why>: <line>'. `log_name` enables the foreign-logger rule."""
    hits: list[str] = []
    foreign = FOREIGN_LOGGERS.get(log_name or "")
    for n, line in enumerate(text.splitlines(), 1):
        if foreign is not None and foreign.search(line):
            hits.append(f"line {n}: foreign logger: {line.strip()[:160]}")
            continue
        for needle in needles:
            if _needle_in(needle, line):
                hits.append(f"line {n}: needle {needle!r}: {line.strip()[:160]}")
                break
    return hits


# --- snapshot --------------------------------------------------------------------


def _is_sidecar(path: Path) -> bool:
    return path.name.endswith(SQLITE_SIDECARS)


def _is_db(path: Path) -> bool:
    return path.suffix == ".db"


def table_state(db_path: Path) -> dict[str, list]:
    """{table: [row_count, sha256 of rows in a stable order]} via a read-only
    connection. Raises on an unreadable database: an error is not a clean read."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        out: dict[str, list] = {}
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        for (table,) in tables:
            ncols = len(conn.execute(f"SELECT * FROM [{table}] LIMIT 0").description)
            order = ", ".join(str(i) for i in range(1, ncols + 1))
            digest = hashlib.sha256()
            count = 0
            for row in conn.execute(f"SELECT * FROM [{table}] ORDER BY {order}"):
                digest.update(repr(row).encode())
                count += 1
            out[table] = [count, digest.hexdigest()[:16]]
        return out
    finally:
        conn.close()


def table_text(db_path: Path, table: str) -> str:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(f"SELECT * FROM [{table}]").fetchall()
    finally:
        conn.close()
    return "\n".join(
        " | ".join(str(v) for v in row if isinstance(v, str)) for row in rows
    )


@dataclass
class Snapshot:
    files: dict[str, str] = field(default_factory=dict)  # rel -> sha256
    sizes: dict[str, int] = field(default_factory=dict)  # rel -> bytes
    tables: dict[str, dict[str, list]] = field(default_factory=dict)  # db -> state


def snapshot(data_dir: Path = DATA) -> Snapshot:
    snap = Snapshot()
    for path in sorted(p for p in data_dir.rglob("*") if p.is_file()):
        rel = path.relative_to(data_dir).as_posix()
        if _is_sidecar(path):
            continue
        if _is_db(path):
            snap.tables[rel] = table_state(path)
            continue
        data = path.read_bytes()
        snap.files[rel] = hashlib.sha256(data).hexdigest()[:16]
        snap.sizes[rel] = len(data)
    return snap


# --- compare ---------------------------------------------------------------------


@dataclass
class Window:
    changed_files: list[str]
    changed_tables: list[str]  # "db:table"
    hits: list[str]
    bytes_scanned: int
    rows_scanned: int


def _live_writer(rel: str) -> bool:
    return any(
        rel == w or (w.endswith("/") and rel.startswith(w)) for w in LIVE_WRITERS
    )


def compare(
    before: Snapshot, after: Snapshot, data_dir: Path, needles: set[str]
) -> Window:
    changed_files = sorted(
        rel
        for rel in set(before.files) | set(after.files)
        if before.files.get(rel) != after.files.get(rel)
    )
    changed_tables = sorted(
        f"{db}:{t}"
        for db in set(before.tables) | set(after.tables)
        for t in set(before.tables.get(db, {})) | set(after.tables.get(db, {}))
        if before.tables.get(db, {}).get(t) != after.tables.get(db, {}).get(t)
    )
    hits: list[str] = []
    scanned = 0
    rows = 0
    for rel in changed_files:
        path = data_dir / rel
        if not path.exists():
            continue
        raw = path.read_bytes()
        start = 0
        # An append-only log that only grew: scan just what was appended. A file
        # that shrank or rotated is scanned whole.
        if rel.startswith("logs/") and after.sizes.get(rel, 0) >= before.sizes.get(
            rel, 0
        ):
            start = before.sizes.get(rel, 0)
        text = raw[start:].decode("utf-8", errors="replace")
        scanned += len(raw) - start
        log_name = Path(rel).name.split(".log")[0] + ".log" if ".log" in rel else None
        hits += [f"{rel}: {h}" for h in scan_text(text, needles, log_name)]
    for key in changed_tables:
        db, table = key.split(":", 1)
        if not (data_dir / db).exists():
            continue
        text = table_text(data_dir / db, table)
        rows += text.count("\n") + 1 if text else 0
        hits += [f"{key}: {h}" for h in scan_text(text, needles)]
    return Window(changed_files, changed_tables, hits, scanned, rows)


@dataclass
class Verdict:
    ok: bool
    failures: list[str]
    warnings: list[str]


def judge(control: Window, suite: Window) -> Verdict:
    failures: list[str] = list(suite.hits)
    warnings: list[str] = []
    for rel in suite.changed_files:
        if _live_writer(rel):
            continue
        if rel in control.changed_files:
            warnings.append(
                f"{rel}: changed in the control window too (undeclared live writer)"
            )
        else:
            failures.append(
                f"{rel}: changed during the suite and no live process writes it"
            )
    for key in suite.changed_tables:
        if key.split(":", 1)[1] in LIVE_TABLES:
            continue
        if key in control.changed_tables:
            warnings.append(
                f"{key}: changed in the control window too (undeclared live table)"
            )
        else:
            failures.append(
                f"{key}: table changed during the suite and the engine never writes it"
            )
    for h in control.hits:
        warnings.append(f"control window hit (not the suite): {h}")
    return Verdict(not failures, failures, warnings)


# --- CLI -------------------------------------------------------------------------


def _require_tree(snap: Snapshot) -> None:
    if not snap.files and not snap.tables:
        print(f"livedata: FAIL cannot run: {DATA} is empty or unreadable")
        sys.exit(2)


def cmd_run(control_seconds: float | None, pytest_args: list[str]) -> int:
    needles = harvest_needles()
    a = snapshot()
    _require_tree(a)
    t0 = time.monotonic()
    proc = subprocess.run([sys.executable, "-m", "pytest", *pytest_args], cwd=ROOT)
    suite_seconds = time.monotonic() - t0
    b = snapshot()
    # The control window comes AFTER the suite, same length (at least 10s, or the
    # value given), so it sees the same live writers at the same cadence.
    wait = max(10.0, suite_seconds) if control_seconds is None else control_seconds
    time.sleep(wait)
    c = snapshot()
    suite = compare(a, b, DATA, needles)
    control = compare(b, c, DATA, needles)
    verdict = judge(control, suite)
    tables = sum(len(t) for t in a.tables.values())
    rows = sum(v[0] for t in a.tables.values() for v in t.values())
    print(
        f"livedata: examined {len(a.files)} files + {tables} tables ({rows} rows) in "
        f"{DATA}; {len(needles)} needles; suite {suite_seconds:.1f}s "
        f"(pytest exit {proc.returncode}), control {wait:.1f}s"
    )
    print(
        f"livedata: suite window changed files {suite.changed_files or '[]'} tables "
        f"{suite.changed_tables or '[]'}; scanned {suite.bytes_scanned} appended "
        f"bytes, {suite.rows_scanned} rows"
    )
    print(
        f"livedata: control window changed files {control.changed_files or '[]'} "
        f"tables {control.changed_tables or '[]'}"
    )
    for w in verdict.warnings:
        print(f"WARN {w}")
    for f in verdict.failures:
        print(f"FAIL {f}")
    print(
        "livedata: OK"
        if verdict.ok
        else f"livedata: {len(verdict.failures)} violation(s)"
    )
    if proc.returncode != 0:
        print("livedata: the wrapped pytest run itself failed")
        return 1
    return 0 if verdict.ok else 1


def cmd_history() -> int:
    """Every needle and foreign-logger hit already sitting in live data, with where."""
    needles = harvest_needles()
    snap = snapshot()
    _require_tree(snap)
    hits: list[str] = []
    scanned = 0
    for rel in snap.files:
        text = (DATA / rel).read_text(encoding="utf-8", errors="replace")
        scanned += len(text)
        log_name = Path(rel).name.split(".log")[0] + ".log" if ".log" in rel else None
        hits += [f"{rel}: {h}" for h in scan_text(text, needles, log_name)]
    rows = 0
    for db, tables in snap.tables.items():
        for table in tables:
            text = table_text(DATA / db, table)
            rows += tables[table][0]
            hits += [f"{db}:{table}: {h}" for h in scan_text(text, needles)]
    print(
        f"livedata history: {len(snap.files)} files ({scanned} chars) + "
        f"{rows} rows scanned; {len(needles)} needles; {len(hits)} hit(s)"
    )
    known = [h for h in hits if h.split(": ", 3)[-1] in KNOWN_HITS]
    new = [h for h in hits if h not in known]
    for h in known:
        print(f"KNOWN {h}")
    for h in new:
        print(f"HIT {h}")
    print(f"livedata history: {len(new)} new, {len(known)} known (recorded in HS-2)")
    return 0 if not new else 1


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run")
    run.add_argument("--control", type=float, default=None)
    run.add_argument("pytest_args", nargs=argparse.REMAINDER)
    sub.add_parser("history")
    args = parser.parse_args(argv)
    if args.cmd == "history":
        return cmd_history()
    pytest_args = [a for a in args.pytest_args if a != "--"]
    return cmd_run(args.control, pytest_args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
