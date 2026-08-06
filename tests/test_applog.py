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


def test_setup_creates_log_dir_and_file(tmp_path, monkeypatch):
    monkeypatch.setattr(applog, "_LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(applog, "_CONFIGURED", False)
    applog.setup()
    assert (tmp_path / "logs").is_dir()
    assert (tmp_path / "logs" / "brief.log").exists()


def test_setup_is_idempotent_no_duplicate_handlers(tmp_path, monkeypatch):
    monkeypatch.setattr(applog, "_LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(applog, "_CONFIGURED", False)
    applog.setup()
    applog.setup()
    applog.setup()
    root = logging.getLogger("brief")
    handlers = [h for h in root.handlers if isinstance(h, logging.handlers.RotatingFileHandler)]
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
