"""brief/applog.py — the print()-to-logging setup (Helm 3b). Logging must
never crash the app, so setup() is best-effort and idempotent."""

from __future__ import annotations

import logging
import logging.handlers

import pytest

from brief import applog


@pytest.fixture(autouse=True)
def _reset_brief_logger():
    # logging.getLogger("brief") is a process-wide singleton -- without this,
    # a handler added by one test (pointing at ITS tmp_path) survives into the
    # next test and satisfies setup()'s "already has a handler" check, so the
    # next test's own tmp_path never gets a handler at all.
    root = logging.getLogger("brief")
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()
    yield
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()
    # Put back conftest's test-dir handler (monkeypatch has already restored
    # _LOG_DIR to it). Leaving NO handler here is what made full-suite runs look
    # isolated while subset runs leaked into the live log (, 2026-09-14).
    applog._CONFIGURED = False
    applog.setup()


def test_setup_creates_the_log_dir_and_the_file_appears_on_the_first_line(
    tmp_path, monkeypatch
):
    """Amended 2026-09-21 (fable #1657 item 4). This used to assert the file
    existed the moment `setup()` returned. That was asserting the
    implementation, not the property: with `delay=True` the handler resolves
    its path at construction and opens it on the first emit, which is the whole
    point (an import must not open a handle on the operator's live log). The property
    worth holding is that a logged line LANDS in data/logs/<name> -- so log
    one. The directory is still made eagerly; only the file is deferred."""
    monkeypatch.setattr(applog, "_LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(applog, "_CONFIGURED", False)
    applog.setup()
    assert (tmp_path / "logs").is_dir()
    assert not (tmp_path / "logs" / "brief.log").exists(), (
        "setup() opened the file; delay=True is the guard against import-time handles"
    )
    applog.get("brief.window.service").warning("first line")
    assert "first line" in (tmp_path / "logs" / "brief.log").read_text()


def test_setup_is_idempotent_no_duplicate_handlers(tmp_path, monkeypatch):
    monkeypatch.setattr(applog, "_LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(applog, "_CONFIGURED", False)
    applog.setup()
    applog.setup()
    applog.setup()
    root = logging.getLogger("brief")
    handlers = [
        h for h in root.handlers if isinstance(h, logging.handlers.RotatingFileHandler)
    ]
    assert len(handlers) == 1


def test_setup_never_raises_on_unwritable_dir(tmp_path, monkeypatch):
    # A file where a directory is expected -> mkdir raises OSError (NotADirectoryError).
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory")
    monkeypatch.setattr(applog, "_LOG_DIR", blocked / "logs")
    monkeypatch.setattr(applog, "_CONFIGURED", False)
    applog.setup()  # must not raise


def test_get_returns_a_brief_namespaced_logger(tmp_path, monkeypatch):
    monkeypatch.setattr(applog, "_LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(applog, "_CONFIGURED", False)
    log = applog.get("brief.window.service")
    assert log.name == "brief.service"
    log2 = applog.get(__name__)  # "tests.test_applog" -> last segment
    assert log2.name == "brief.test_applog"


def test_log_level_defaults_to_info(tmp_path, monkeypatch):
    monkeypatch.delenv("BRIEF_LOG_LEVEL", raising=False)
    monkeypatch.setattr(applog, "_LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(applog, "_CONFIGURED", False)
    applog.setup()
    assert logging.getLogger("brief").level == logging.INFO


def test_log_level_overridable_via_env(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIEF_LOG_LEVEL", "DEBUG")
    monkeypatch.setattr(applog, "_LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(applog, "_CONFIGURED", False)
    applog.setup()
    assert logging.getLogger("brief").level == logging.DEBUG


# --- HS-5: one log file per process ------------------------------------------


def test_log_file_defaults_to_brief_log(tmp_path, monkeypatch):
    """With BRIEF_LOG_FILE unset the process writes brief.log, not something
    else. Amended 2026-09-21 for delay=True (see the setup test above): the
    name is proved by writing a line, which is what the sibling
    `test_log_file_selectable_per_process_via_env` already did."""
    monkeypatch.delenv("BRIEF_LOG_FILE", raising=False)
    monkeypatch.setattr(applog, "_LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(applog, "_CONFIGURED", False)
    applog.get("window.service").warning("hello from the engine")
    assert (tmp_path / "logs" / "brief.log").read_text().count("hello") == 1
    assert not (tmp_path / "logs" / "speaker.log").exists()


def test_log_file_selectable_per_process_via_env(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIEF_LOG_FILE", "speaker.log")
    monkeypatch.setattr(applog, "_LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(applog, "_CONFIGURED", False)
    applog.get("dispatch_speak").warning("hello from the speaker")
    assert (tmp_path / "logs" / "speaker.log").read_text().count("hello") == 1
    assert not (tmp_path / "logs" / "brief.log").exists()


def test_log_file_env_cannot_escape_the_log_dir(monkeypatch):
    monkeypatch.setenv("BRIEF_LOG_FILE", "../../etc/evil.log")
    assert applog.log_filename() == "evil.log"
    monkeypatch.setenv("BRIEF_LOG_FILE", "   ")
    assert applog.log_filename() == "brief.log"
