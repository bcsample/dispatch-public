"""HS-2: the live-data guard (scripts/livedata_check.py), birth-tested on a tmp COPY
of the live data (T-49). The live tree is only READ (a SQLite backup through a
read-only connection, plain file copies); every plant lands in tmp_path.
"""

from __future__ import annotations

import hashlib
import importlib.util
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
LIVE = REPO / "data"


def _load():
    spec = importlib.util.spec_from_file_location(
        "livedata_check", REPO / "scripts" / "livedata_check.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["livedata_check"] = mod  # dataclasses resolve their module by name
    spec.loader.exec_module(mod)
    return mod


lc = _load()
NEEDLES = lc.harvest_needles()


@pytest.fixture
def data_copy(tmp_path):
    if not (LIVE / "brief.db").exists():
        pytest.skip("no live data on this machine to copy")
    dst = tmp_path / "data"
    for src in LIVE.rglob("*"):
        if not src.is_file() or lc._is_sidecar(src):
            continue
        rel = src.relative_to(LIVE)
        (dst / rel).parent.mkdir(parents=True, exist_ok=True)
        if lc._is_db(src):
            live = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
            copy = sqlite3.connect(dst / rel)
            live.backup(copy)
            copy.close()
            live.close()
        else:
            shutil.copy2(src, dst / rel)
    return dst


def _window(before, data_dir):
    return lc.compare(before, lc.snapshot(data_dir), data_dir, NEEDLES)


# --- silent control ---------------------------------------------------------------


def test_silent_control_on_an_untouched_copy(data_copy):
    before = lc.snapshot(data_copy)
    assert before.files and before.tables  # a check that examined nothing did not pass
    window = _window(before, data_copy)
    assert window.changed_files == [] and window.changed_tables == []
    assert window.hits == []
    assert lc.judge(window, window).ok


# --- planted leaks, each detected and attributed ---------------------------------


def test_planted_db_row_in_a_live_table_is_caught_by_its_fingerprint(data_copy):
    before = lc.snapshot(data_copy)
    conn = sqlite3.connect(data_copy / "brief.db")
    cols = [r[1] for r in conn.execute("PRAGMA table_info(news_articles)")]
    values = dict.fromkeys(cols)
    values.update({"title": "Beat story", "url": "http://na/1"})
    values = {k: v for k, v in values.items() if k in cols}
    conn.execute(
        f"INSERT INTO news_articles ({', '.join(values)}) VALUES "
        f"({', '.join('?' for _ in values)})",
        list(values.values()),
    )
    conn.commit()
    conn.close()
    suite = _window(before, data_copy)
    assert "brief.db:news_articles" in suite.changed_tables
    verdict = lc.judge(lc.compare(before, before, data_copy, NEEDLES), suite)
    assert not verdict.ok
    assert any(
        f.startswith("brief.db:news_articles") and "http://na/1" in f
        for f in verdict.failures
    )


def test_planted_change_to_a_table_the_engine_never_writes_fails(data_copy):
    before = lc.snapshot(data_copy)
    conn = sqlite3.connect(data_copy / "brief.db")
    cols = [r[1] for r in conn.execute("PRAGMA table_info(feedback)")]
    notnull = {r[1] for r in conn.execute("PRAGMA table_info(feedback)") if r[3]}
    row = {c: ("x" if c in notnull else None) for c in cols if c != "id"}
    conn.execute(
        f"INSERT INTO feedback ({', '.join(row)}) "
        f"VALUES ({', '.join('?' for _ in row)})",
        list(row.values()),
    )
    conn.commit()
    conn.close()
    empty = lc.compare(before, before, data_copy, NEEDLES)
    verdict = lc.judge(empty, _window(before, data_copy))
    assert any(
        "brief.db:feedback" in f and "never writes" in f for f in verdict.failures
    )


def test_planted_log_lines_are_caught_needle_and_foreign_logger(data_copy):
    """The two WILD SPECIMENS from 2026-09-14, re-timestamped so they are not the
    acknowledged originals: one caught by its needle, one by its logger name."""
    before = lc.snapshot(data_copy)
    with open(data_copy / "logs" / "brief.log", "a", encoding="utf-8") as fh:
        fh.write(
            "2026-09-14 12:00:00  ERROR   brief.gdelt  GDELT fetch FAILED "
            "(ConnectionError('gdelt down'))\n"
            "2026-09-14 12:00:01  INFO    brief.dispatch_speak  bulletin skipped — "
            "in a meeting (microphone in use) (1 item(s))\n"
        )
    suite = _window(before, data_copy)
    assert suite.bytes_scanned < 1000  # only the appended region was scanned
    hits = [h for h in suite.hits if h.startswith("logs/brief.log")]
    assert any("needle 'gdelt down'" in h for h in hits)
    assert any("foreign logger" in h for h in hits)


def test_planted_config_leak_is_caught(data_copy):
    before = lc.snapshot(data_copy)
    kw = data_copy / "google_news_keywords.json"
    kw.write_text(kw.read_text() + '\n["http://example.test/feed"]\n')
    (data_copy / "leak.json").write_text("{}")
    empty = lc.compare(before, before, data_copy, NEEDLES)
    verdict = lc.judge(empty, _window(before, data_copy))
    assert any(
        f.startswith("google_news_keywords.json") and "example.test" in f
        for f in verdict.failures
    )
    assert any(f.startswith("leak.json") for f in verdict.failures)


# --- the control window -----------------------------------------------------------


def test_a_change_seen_in_the_control_window_too_warns_not_fails(tmp_path):
    suite = lc.Window(["mystery.json"], ["brief.db:briefs"], [], 0, 0)
    control = lc.Window(["mystery.json"], ["brief.db:briefs"], [], 0, 0)
    verdict = lc.judge(control, suite)
    assert verdict.ok
    assert len(verdict.warnings) == 2
    alone = lc.judge(lc.Window([], [], [], 0, 0), suite)
    assert not alone.ok and len(alone.failures) == 2


def test_live_writers_and_live_tables_never_fail_on_change_alone():
    suite = lc.Window(
        ["logs/brief.log", "speaker_stamp.json"], ["brief.db:news_articles"], [], 0, 0
    )
    assert lc.judge(lc.Window([], [], [], 0, 0), suite).ok


# --- WAL blindness (dispatch #1302) -----------------------------------------------


def test_a_byte_hash_is_blind_to_a_committed_wal_write_the_logical_hash_is_not(
    tmp_path,
):
    db = tmp_path / "wal.db"
    writer = sqlite3.connect(db)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("CREATE TABLE t (v TEXT)")
    writer.execute("INSERT INTO t VALUES ('one')")
    writer.commit()
    writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    bytes_before = hashlib.sha256(db.read_bytes()).hexdigest()
    logical_before = lc.table_state(db)
    writer.execute("UPDATE t SET v = 'two'")
    writer.commit()  # committed, writer still open, not checkpointed
    try:
        assert hashlib.sha256(db.read_bytes()).hexdigest() == bytes_before
        assert lc.table_state(db) != logical_before
    finally:
        writer.close()


def test_sqlite_sidecars_are_excluded_from_file_hashes(tmp_path):
    (tmp_path / "brief.db-wal").write_text("x")
    (tmp_path / "brief.db-shm").write_text("x")
    (tmp_path / "note.txt").write_text("x")
    assert list(lc.snapshot(tmp_path).files) == ["note.txt"]


# --- needles and the T-51 floor ---------------------------------------------------


def test_needles_include_test_fingerprints_and_exclude_real_errors():
    assert "gdelt down" in NEEDLES and "http://r/1" in NEEDLES
    assert not any("eventregistry.org" in n for n in NEEDLES)  # a real 403, not ours
    assert not any("localhost" in n for n in NEEDLES)


def test_url_needles_match_only_at_a_token_boundary():
    assert lc._needle_in("http://a", "fetched http://a/1 ok")
    assert not lc._needle_in("http://a", "fetched http://apnews.com/x ok")
    assert lc._needle_in("http://r/1", "url='http://r/1'")


def test_known_hits_are_the_two_recorded_lines_exactly():
    assert len(lc.KNOWN_HITS) == 2
    for line in lc.KNOWN_HITS:
        assert lc.scan_text(line, NEEDLES, "brief.log")  # still detected, just known


def test_an_empty_tree_cannot_pass(tmp_path, monkeypatch):
    monkeypatch.setattr(lc, "DATA", tmp_path)
    with pytest.raises(SystemExit) as exc:
        lc._require_tree(lc.snapshot(tmp_path))
    assert exc.value.code == 2
