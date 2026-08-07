"""Tests for calendar_check.in_meeting_now() -- the optional extra Google-
Calendar meeting signal. Network-free: the external integration is faked via
sys.modules, never touching any real calendar API."""

from __future__ import annotations

import sys
import types

from brief.window import calendar_check


def _fake_integration_module(is_in_meeting_now):
    mod = types.ModuleType("calendar_integration")
    mod.is_in_meeting_now = is_in_meeting_now
    return mod


def test_true_when_the_integration_reports_a_meeting(monkeypatch):
    monkeypatch.setitem(
        sys.modules, "calendar_integration", _fake_integration_module(lambda: True)
    )
    assert calendar_check.in_meeting_now() is True


def test_false_when_the_integration_reports_no_meeting(monkeypatch):
    monkeypatch.setitem(
        sys.modules, "calendar_integration", _fake_integration_module(lambda: False)
    )
    assert calendar_check.in_meeting_now() is False


def test_fails_soft_when_no_integration_is_configured(monkeypatch):
    # sys.modules[name] = None is Python's own signal for "known missing" --
    # `from calendar_integration import ...` raises ImportError, simulating
    # the default state: no integration configured. Must return False, never
    # raise -- this is what a fresh install with zero setup looks like.
    monkeypatch.setitem(sys.modules, "calendar_integration", None)
    assert calendar_check.in_meeting_now() is False


def test_fails_soft_when_the_calendar_call_itself_raises(monkeypatch):
    def _raise():
        raise RuntimeError("calendar API unreachable")

    monkeypatch.setitem(
        sys.modules, "calendar_integration", _fake_integration_module(_raise)
    )
    assert calendar_check.in_meeting_now() is False
