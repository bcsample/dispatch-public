"""Tests for calendar_check.in_meeting_now() -- the extra Google-Calendar
meeting signal that reuses project-jarvis's already-authorized read-only
tokens. Network-free: the cross-repo import is faked via sys.modules, never
touching the real project-jarvis install, Google API, or Keychain."""

from __future__ import annotations

import sys
import types

from brief.window import calendar_check


def _fake_jarvis_module(is_in_meeting_now):
    jarvis_pkg = types.ModuleType("jarvis")
    google_sync_mod = types.ModuleType("jarvis.google_sync")
    google_sync_mod.is_in_meeting_now = is_in_meeting_now
    jarvis_pkg.google_sync = google_sync_mod
    return jarvis_pkg, google_sync_mod


def test_true_when_project_jarvis_reports_a_meeting(monkeypatch):
    jarvis_pkg, google_sync_mod = _fake_jarvis_module(lambda: True)
    monkeypatch.setitem(sys.modules, "jarvis", jarvis_pkg)
    monkeypatch.setitem(sys.modules, "jarvis.google_sync", google_sync_mod)
    assert calendar_check.in_meeting_now() is True


def test_false_when_project_jarvis_reports_no_meeting(monkeypatch):
    jarvis_pkg, google_sync_mod = _fake_jarvis_module(lambda: False)
    monkeypatch.setitem(sys.modules, "jarvis", jarvis_pkg)
    monkeypatch.setitem(sys.modules, "jarvis.google_sync", google_sync_mod)
    assert calendar_check.in_meeting_now() is False


def test_fails_soft_when_project_jarvis_is_missing(monkeypatch):
    # sys.modules[name] = None is Python's own signal for "known missing" --
    # `from jarvis import ...` raises ImportError, simulating project-jarvis
    # not being installed/found on this machine. Must return False, never raise.
    monkeypatch.setitem(sys.modules, "jarvis", None)
    assert calendar_check.in_meeting_now() is False


def test_fails_soft_when_the_calendar_call_itself_raises(monkeypatch):
    def _raise():
        raise RuntimeError("Google API unreachable")

    jarvis_pkg, google_sync_mod = _fake_jarvis_module(_raise)
    monkeypatch.setitem(sys.modules, "jarvis", jarvis_pkg)
    monkeypatch.setitem(sys.modules, "jarvis.google_sync", google_sync_mod)
    assert calendar_check.in_meeting_now() is False
