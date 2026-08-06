"""Flight route/airline enrichment (brief/window/flightroute.py) — adsbdb parse,
caching (no re-fetch), per-cycle lookup budget, negative caching, fail-soft."""

from __future__ import annotations

import pytest

from brief.window import flightroute


@pytest.fixture(autouse=True)
def _clear_cache():
    flightroute._CACHE.clear()
    yield
    flightroute._CACHE.clear()


class _Resp:
    def __init__(self, body, status=200):
        self._body = body
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("http error")

    def json(self):
        return self._body


_JBU = {
    "response": {
        "flightroute": {
            "airline": {"name": "JetBlue Airways"},
            "origin": {"iata_code": "BWI"},
            "destination": {"iata_code": "FLL"},
        }
    }
}


def test_enrich_adds_airline_and_route(monkeypatch):
    monkeypatch.setattr(flightroute.requests, "get", lambda *a, **k: _Resp(_JBU))
    ac = [{"callsign": "JBU2595"}]
    flightroute.enrich(ac, _now=1000)
    assert ac[0]["airline"] == "JetBlue Airways"
    assert ac[0]["from"] == "BWI" and ac[0]["to"] == "FLL"


def test_second_call_uses_cache_no_refetch(monkeypatch):
    calls = {"n": 0}

    def once(*a, **k):
        calls["n"] += 1
        return _Resp(_JBU)

    monkeypatch.setattr(flightroute.requests, "get", once)
    flightroute.enrich([{"callsign": "JBU2595"}], _now=1000)
    flightroute.enrich([{"callsign": "JBU2595"}], _now=1050)  # within TTL
    assert calls["n"] == 1


def test_lookup_budget_caps_network_per_cycle(monkeypatch):
    calls = {"n": 0}

    def counted(*a, **k):
        calls["n"] += 1
        return _Resp(_JBU)

    monkeypatch.setattr(flightroute.requests, "get", counted)
    ac = [{"callsign": f"CS{i}"} for i in range(10)]
    flightroute.enrich(ac, max_lookups=3, _now=1000)
    assert calls["n"] == 3  # only 3 uncached lookups this cycle


def test_404_negative_cached(monkeypatch):
    calls = {"n": 0}

    def notfound(*a, **k):
        calls["n"] += 1
        return _Resp({}, status=404)

    monkeypatch.setattr(flightroute.requests, "get", notfound)
    flightroute.enrich([{"callsign": "N12345"}], _now=1000)
    flightroute.enrich([{"callsign": "N12345"}], _now=1200)  # within NEG_TTL
    assert calls["n"] == 1
    assert "N12345" in flightroute._CACHE


def test_fail_soft_leaves_flight_unenriched(monkeypatch):
    def boom(*a, **k):
        raise ConnectionError("adsbdb down")

    monkeypatch.setattr(flightroute.requests, "get", boom)
    ac = [{"callsign": "JBU2595"}]
    flightroute.enrich(ac, _now=1000)
    assert "airline" not in ac[0] and "from" not in ac[0]
