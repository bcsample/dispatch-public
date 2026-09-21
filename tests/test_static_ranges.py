"""UP-1 step 4 (starlette 1.6.0): /static rejects malformed byte ranges.

The board serves `brief/window/static/` through Starlette's StaticFiles, reachable on
the private network. Starlette 1.5.1 hardened FileResponse: an inverted single-byte range is
rejected and a request is capped at 100 ranges. Measured against the LIVE engine on
starlette 1.3.1 before the upgrade (2026-09-14): `Range: bytes=1-0` answered 206 with
an empty body, and 101 ranges answered 206 with 10514 bytes. These tests pin the
hardened behaviour so a pin rollback cannot quietly undo it.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from brief.window.app import create_app

STATIC = "/static/ne_110m_land.json"


@pytest.fixture
def client():
    # No `with`: no lifespan, so no loops start; StaticFiles needs none.
    return TestClient(create_app(world_feeds=[], window_cfg={}, news_sources=[]))


def test_a_normal_range_is_served_partially(client):
    r = client.get(STATIC, headers={"Range": "bytes=0-99"})
    assert r.status_code == 206
    assert len(r.content) == 100


@pytest.mark.parametrize("bad", ["bytes=1-0", "bytes=10-9"])
def test_an_inverted_single_byte_range_is_rejected(client, bad):
    r = client.get(STATIC, headers={"Range": bad})
    assert r.status_code in (400, 416), (r.status_code, len(r.content))


def test_more_than_100_ranges_is_not_honoured_as_a_multipart(client):
    """Starlette 1.6.0 caps at `max_ranges = 100` by IGNORING the Range header past
    it (responses.py returns no ranges, so the whole file is served with 200), which
    RFC 9110 allows. The protection is that it never builds a 101-part body; on
    1.3.1 it answered 206 multipart/byteranges. Asserting 400/416 here was a wrong
    guess about the shape, corrected after reading the source."""
    ranges = ",".join(f"{i * 10}-{i * 10 + 1}" for i in range(101))
    r = client.get(STATIC, headers={"Range": f"bytes={ranges}"})
    assert r.status_code == 200, (r.status_code, len(r.content))
    assert "multipart/byteranges" not in r.headers.get("content-type", "")


def test_100_ranges_is_still_allowed(client):
    ranges = ",".join(f"{i * 10}-{i * 10 + 1}" for i in range(100))
    r = client.get(STATIC, headers={"Range": f"bytes={ranges}"})
    assert r.status_code == 206
