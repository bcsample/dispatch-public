""": no test's log line can land in the live data/logs/ (see conftest.py).

test_applog's teardown used to leave the "brief" logger with no handler at all,
which is the accident that hid the leak from full-suite runs. It now restores
conftest's test-dir handler, and the first test below requires a handler to
exist, so this file checks the real configuration whatever order it runs in.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

import pytest

from brief import applog, config

REPO = Path(__file__).resolve().parents[1]


def _file_handlers() -> list[tuple[str, Path]]:
    return [
        (type(h).__module__, Path(h.baseFilename).resolve())
        for h in logging.getLogger("brief").handlers
        if hasattr(h, "baseFilename")
    ]


def test_brief_logger_writes_to_the_test_dir_not_live_data(conftest_log_dir):
    """The property: NO file handler of any origin targets live data, and applog's
    own handler targets the conftest temp dir. pytest 9.1 attaches its own capture
    handlers to non-propagating loggers too, one with baseFilename /dev/null
    (found at UP-1, 2026-09-14); those are pytest's, not a leak, and not required
    to live in the test dir."""
    live = config.DATA_DIR.resolve()
    handlers = _file_handlers()
    assert handlers, "conftest must install a handler (a check with nothing to check)"
    for _module, path in handlers:
        assert live not in path.parents, f"test logging targets live data: {path}"
    ours = [path for module, path in handlers if not module.startswith("_pytest")]
    assert ours, "applog's handler is missing"
    for path in ours:
        assert conftest_log_dir in path.parents, (
            f"applog handler outside test dir: {path}"
        )


def test_a_logged_error_lands_in_the_test_dir(conftest_log_dir):
    marker = "t48-isolation-marker-7c1f"
    applog.get("brief.gdelt").error("GDELT fetch FAILED (%s)", marker)
    for h in logging.getLogger("brief").handlers:
        h.flush()
    written = "".join(p.read_text() for p in conftest_log_dir.glob("*.log"))
    assert marker in written


def test_single_file_run_leaves_the_live_log_untouched(tmp_path):
    """Birth test on the wild specimen: before the conftest fix, running exactly
    this one test on its own appended "GDELT fetch FAILED (ConnectionError('gdelt
    down'))" to the live brief.log. Re-run it in a subprocess and assert the live
    log did not gain the line. Reads the live log; never writes it."""
    live_log = config.DATA_DIR / "logs" / "brief.log"
    if not live_log.exists():
        pytest.skip("no live brief.log on this machine: a leak would be invisible")
    needle = "ConnectionError('gdelt down')"
    before = live_log.read_text(errors="replace").count(needle)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "tests/test_gdelt.py::test_fetch_fail_soft_on_exception",
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    after = live_log.read_text(errors="replace").count(needle)
    assert after == before, "a single-test run wrote a fixture line into the live log"


# --- HS-2: no thread outlives its test (the conftest audit's own birth test) ------


def test_leftover_threads_names_a_thread_that_outlives_the_grace():
    import threading

    from _isolation import leftover_threads

    before = set(threading.enumerate())
    release = threading.Event()
    t = threading.Thread(target=release.wait, name="planted-sweep-loop", daemon=True)
    t.start()
    try:
        assert leftover_threads(before, grace=0.05) == [
            "planted-sweep-loop (daemon=True)"
        ]
    finally:
        release.set()
        t.join(timeout=2)
    assert leftover_threads(before, grace=0.05) == []  # released: silent control


def test_leftover_threads_ignores_a_thread_that_finishes_within_grace():
    import threading
    import time

    from _isolation import leftover_threads

    before = set(threading.enumerate())
    threading.Thread(target=time.sleep, args=(0.05,), name="quick").start()
    assert leftover_threads(before, grace=1.0) == []


def test_the_handler_check_flags_a_handler_aimed_at_live_data():
    """Birth test for the corrected property above. delay=True sets baseFilename
    without opening, so nothing is created under the live data/logs/."""
    live_path = config.DATA_DIR / "logs" / "zz-birth-test-never-created.log"
    planted = logging.FileHandler(live_path, delay=True)
    root = logging.getLogger("brief")
    root.addHandler(planted)
    try:
        live = config.DATA_DIR.resolve()
        flagged = [p for _m, p in _file_handlers() if live in p.parents]
        assert flagged == [live_path.resolve()]
    finally:
        root.removeHandler(planted)
        planted.close()
    assert not live_path.exists()


# --- fable #1657 item 4: importing a brief module must not OPEN the live log ------


def test_importing_a_brief_module_does_not_open_a_handle_on_the_live_log():
    """The sink property, asserted directly (): after a bare `import
    brief.window.kokoro_tts` in a fresh interpreter -- no conftest, no test
    redirect, applog pointed at the operator's real data/logs/ -- applog's handler has
    NOT opened its file.

    Wild specimen: the desk's read of kokoro_tts.py:27 (`log =
    applog.get(__name__)` at module level -> applog.setup() at IMPORT). This is
    a birth test: run it against applog.py without delay=True and the handler's
    stream is an open file object on brief.log, so it fails. A handle is not a
    written line, but  is the project's proof that a held handle on live
    data is its own hazard -- a rotation behind it and the engine writes to an
    unlinked inode.

    Checks the stream rather than the file's bytes on purpose: opening for
    append writes nothing and changes no mtime, so a before/after diff of the
    file is blind to exactly the thing this guards.
    """
    probe = (
        "import brief.window.kokoro_tts, logging, json\n"
        "hs = [h for h in logging.getLogger('brief').handlers "
        "if hasattr(h, 'baseFilename')]\n"
        "print(json.dumps([[h.baseFilename, h.stream is not None] for h in hs]))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    import json

    handlers = json.loads(result.stdout.strip().splitlines()[-1])
    assert handlers, "applog installed no file handler (a check with nothing to check)"
    live = config.DATA_DIR.resolve()
    for filename, is_open in handlers:
        if live in Path(filename).resolve().parents:
            assert not is_open, f"import opened a handle on live data: {filename}"
