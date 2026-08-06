"""Tests for brief.modelroles (Helm 2c: Engine Room model-role resolution).
Ported from project-jarvis's test_modelroles.py — same contract, same
fail-soft guarantees, env var names adapted to this repo (BRIEF_ENGINEROOM_*)."""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

from brief import modelroles


@pytest.fixture(autouse=True)
def _clear_cache():
    modelroles._cache.clear()
    yield
    modelroles._cache.clear()


def _fake_urlopen_returning(payload: dict):
    def _fake(req, timeout=None):
        return io.BytesIO(json.dumps(payload).encode())
    return _fake


def test_resolve_returns_engine_room_model_on_success(monkeypatch):
    monkeypatch.setattr(
        modelroles.urllib.request, "urlopen",
        _fake_urlopen_returning({"role": "chat.small", "model": "qwen3.5:9b"}),
    )
    assert modelroles.resolve("chat.small", "fallback-default") == "qwen3.5:9b"


def test_resolve_falls_back_when_unreachable(monkeypatch):
    def _raise(req, timeout=None):
        raise urllib.error.URLError("no route to host")

    monkeypatch.setattr(modelroles.urllib.request, "urlopen", _raise)
    assert modelroles.resolve("chat.small", "fallback-default") == "fallback-default"


def test_resolve_falls_back_on_malformed_json(monkeypatch):
    def _fake(req, timeout=None):
        return io.BytesIO(b"not json")

    monkeypatch.setattr(modelroles.urllib.request, "urlopen", _fake)
    assert modelroles.resolve("chat.small", "fallback-default") == "fallback-default"


def test_resolve_falls_back_when_model_field_missing(monkeypatch):
    monkeypatch.setattr(
        modelroles.urllib.request, "urlopen",
        _fake_urlopen_returning({"role": "chat.small"}),
    )
    assert modelroles.resolve("chat.small", "fallback-default") == "fallback-default"


def test_resolve_caches_success_no_second_network_call(monkeypatch):
    calls = {"n": 0}

    def _fake(req, timeout=None):
        calls["n"] += 1
        return io.BytesIO(json.dumps({"model": "qwen3.5:9b"}).encode())

    monkeypatch.setattr(modelroles.urllib.request, "urlopen", _fake)
    assert modelroles.resolve("chat.small", "default") == "qwen3.5:9b"
    assert modelroles.resolve("chat.small", "default") == "qwen3.5:9b"
    assert calls["n"] == 1


def test_resolve_caches_fallback_too_no_repeated_timeouts(monkeypatch):
    # The hard rule: Engine Room being down must never cost every subsequent
    # call another network round-trip -- the fallback itself gets cached.
    calls = {"n": 0}

    def _raise(req, timeout=None):
        calls["n"] += 1
        raise urllib.error.URLError("down")

    monkeypatch.setattr(modelroles.urllib.request, "urlopen", _raise)
    assert modelroles.resolve("chat.small", "default") == "default"
    assert modelroles.resolve("chat.small", "default") == "default"
    assert calls["n"] == 1


def test_resolve_sends_bearer_token_when_key_file_present(tmp_path, monkeypatch):
    key_file = tmp_path / "key.token"
    key_file.write_text("secret-key-123\n")
    monkeypatch.setenv("BRIEF_ENGINEROOM_KEY_FILE", str(key_file))
    captured = {}

    def _fake(req, timeout=None):
        captured["headers"] = dict(req.header_items())
        return io.BytesIO(json.dumps({"model": "qwen3.5:9b"}).encode())

    monkeypatch.setattr(modelroles.urllib.request, "urlopen", _fake)
    modelroles.resolve("chat.small", "default")
    assert captured["headers"].get("Authorization") == "Bearer secret-key-123"


def test_resolve_no_auth_header_when_key_file_missing(monkeypatch):
    monkeypatch.setenv("BRIEF_ENGINEROOM_KEY_FILE", "/nonexistent/path")
    captured = {}

    def _fake(req, timeout=None):
        captured["headers"] = dict(req.header_items())
        return io.BytesIO(json.dumps({"model": "qwen3.5:9b"}).encode())

    monkeypatch.setattr(modelroles.urllib.request, "urlopen", _fake)
    modelroles.resolve("chat.small", "default")
    assert "Authorization" not in captured["headers"]


def test_base_url_overridable_via_env(monkeypatch):
    monkeypatch.setenv("BRIEF_ENGINEROOM_URL", "http://example.test:9999/")
    assert modelroles._base_url() == "http://example.test:9999"


def test_base_url_defaults_to_engine_room_tailnet_address(monkeypatch):
    monkeypatch.delenv("BRIEF_ENGINEROOM_URL", raising=False)
    assert modelroles._base_url() == modelroles._BASE_URL_DEFAULT
