"""The "Skies" board (brief/window/flights.py + service.FlightLoop) — adsb.fi
normalization, ordering, fail-soft, and the loop's location gate."""

from __future__ import annotations

from brief.window import flights, service


class _Resp:
    def __init__(self, body, ok=True):
        self._body = body
        self.ok = ok

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError("http error")

    def json(self):
        return self._body


_SAMPLE = {
    "ac": [
        {
            "flight": "UAL123 ",
            "alt_baro": 34000,
            "gs": 450,
            "track": 90,
            "baro_rate": 1200,
            "lat": 38.9,
            "lon": -77.0,
        },
        {
            "flight": "GND99",
            "alt_baro": "ground",
            "gs": 12,
            "track": None,
            "baro_rate": None,
            "lat": 38.8,
            "lon": -77.1,
        },
        {
            "flight": "DAL55",
            "alt_baro": 8000,
            "gs": 300,
            "track": 270,
            "baro_rate": -900,
            "lat": 39.0,
            "lon": -77.2,
        },
        {"alt_baro": 5000},  # no identity -> dropped
    ]
}


def test_fetch_normalizes_orders_and_converts(monkeypatch):
    monkeypatch.setattr(flights.requests, "get", lambda *a, **k: _Resp(_SAMPLE))
    got = flights.fetch_flights(38.85, -77.04)
    # Airborne first (highest alt leads), on-ground sinks; no-identity dropped.
    assert [a["callsign"] for a in got] == ["UAL123", "DAL55", "GND99"]
    ual = got[0]
    assert ual["altitude"] == 34000
    assert ual["speed"] == round(450 * 1.15078)  # knots -> mph
    assert ual["heading"] == 90 and ual["climb"] == 1200
    gnd = got[-1]
    assert gnd["on_ground"] is True and gnd["altitude"] is None


def test_fetch_fail_soft_returns_none(monkeypatch):
    def boom(*a, **k):
        raise ConnectionError("adsb down")

    monkeypatch.setattr(flights.requests, "get", boom)
    assert flights.fetch_flights(38.85, -77.04) is None


def test_fetch_limit_caps_results(monkeypatch):
    many = {
        "ac": [
            {"flight": f"AC{i}", "alt_baro": 30000 - i * 100, "gs": 400}
            for i in range(20)
        ]
    }
    monkeypatch.setattr(flights.requests, "get", lambda *a, **k: _Resp(many))
    assert len(flights.fetch_flights(0, 0, limit=5)) == 5


def test_flight_loop_disabled_without_location():
    state = service.WindowState(sweep_interval_seconds=900)
    loop = service.FlightLoop(state, lat=None, lon=None)
    assert loop.enabled is False
    loop.run_one_fetch()  # no-op
    loop.start()  # no thread spawned
    assert loop.is_alive() is False
    assert state.flights_snapshot() == []


def test_flight_loop_populates_state(monkeypatch):
    monkeypatch.setattr(
        service.flights, "fetch_flights", lambda *a, **k: [{"callsign": "X"}]
    )
    state = service.WindowState(sweep_interval_seconds=900)
    loop = service.FlightLoop(state, lat=38.85, lon=-77.04)
    assert loop.enabled is True
    loop.run_one_fetch()
    assert state.flights_snapshot() == [{"callsign": "X"}]


def test_flight_loop_fetch_none_keeps_prior_board(monkeypatch):
    state = service.WindowState(sweep_interval_seconds=900)
    state.record_flights([{"callsign": "PRIOR"}])
    monkeypatch.setattr(service.flights, "fetch_flights", lambda *a, **k: None)
    loop = service.FlightLoop(state, lat=1.0, lon=2.0)
    loop.run_one_fetch()
    assert state.flights_snapshot() == [{"callsign": "PRIOR"}]
