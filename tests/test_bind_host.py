"""Bind address (architecture review /#539, 2026-08-16). Dispatch was the ONLY
service in the project bound to every interface (0.0.0.0) -- reachable
from any device on the local wifi, not merely the private network, for a daily
intel brief. Project convention: loopback bind + a `a private-network proxy`
forwarder (as ComfyUI :8000, Studio :7861, overwatch :8900 do), never the
private network interface and never a wildcard. This file exists because the
failure mode IS the security property: the dangerous regression isn't "the
board is unreachable", it's "the board silently went back to being
LAN-readable"."""

from __future__ import annotations

import ipaddress

from brief import config


def test_the_default_bind_is_loopback():
    assert config._WINDOW_DEFAULTS["host"] == "127.0.0.1"


def test_the_default_bind_is_not_a_wildcard():
    """The actual regression guard. 0.0.0.0 and :: both mean "every
    interface", which is what LAN-readable looks like."""
    host = config._WINDOW_DEFAULTS["host"]
    assert host not in ("0.0.0.0", "::", "")
    assert ipaddress.ip_address(host).is_loopback


def test_window_yaml_does_not_override_back_to_a_wildcard():
    """The code default is only half the guarantee -- config/window.yaml's
    own `host:` key wins if present. Assert the file agrees, since that's
    exactly how this became 0.0.0.0 in the first place."""
    cfg = config.load_window_config()
    assert cfg["host"] not in ("0.0.0.0", "::", "")
    assert ipaddress.ip_address(cfg["host"]).is_loopback


def test_entry_point_binds_exactly_the_configured_host(monkeypatch):
    """__main__ must hand uvicorn the configured host verbatim, and the
    DISPATCH_BIND_HOST env override -- when set -- must win over the config
    file (a typed-out escape hatch, never a failed-lookup fallback)."""
    from brief.window import __main__ as entry

    captured = {}
    monkeypatch.setattr(entry.uvicorn, "run", lambda app, **kw: captured.update(kw))
    monkeypatch.delenv("DISPATCH_BIND_HOST", raising=False)
    entry.main()
    assert captured["host"] == "127.0.0.1"

    monkeypatch.setenv("DISPATCH_BIND_HOST", "100.115.16.42")
    entry.main()
    assert captured["host"] == "100.115.16.42"
