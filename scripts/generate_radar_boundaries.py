"""Generate static/radar_boundaries.json — DC/VA/MD state borders clipped to
the local flight-radar area, so the Skies radar has real land/state reference
(the Potomac shows up naturally: it IS the VA/DC/MD border along the river).

Sources (fetched once, at generation time only):
  - Virginia, Maryland: glynnbird/usstatesgeojson (high resolution)
  - District of Columbia: PublicaMundi all-states (coarse, but DC is a small
    diamond so a few points suffice)

We keep only the points near Alexandria (padded bbox) and split each ring into
polylines at gaps, so the output is tiny (a few hundred [lat,lon] points). The
dashboard projects them client-side with the same px() the aircraft use, and
the SVG viewport clips anything past the drawn box.

Run once (re-run only to widen the area or refresh the data):
    .venv/bin/python scripts/generate_radar_boundaries.py
"""

from __future__ import annotations

import json
from pathlib import Path

import requests

OUT = (
    Path(__file__).resolve().parents[1]
    / "brief"
    / "window"
    / "static"
    / ("radar_boundaries.json")
)

# Keep-box: just past the 30nm radar edges (lat ±0.5°, lon ±0.63° around
# Alexandria) so border lines reach the drawn edges without hauling in the far
# Chesapeake shoreline (which would only bloat the file, then get SVG-clipped).
LAT_MIN, LAT_MAX = 38.25, 39.45
LON_MIN, LON_MAX = -77.77, -76.31

SOURCES = [
    (
        "virginia",
        "https://raw.githubusercontent.com/glynnbird/usstatesgeojson/master/virginia.geojson",
        None,
    ),
    (
        "maryland",
        "https://raw.githubusercontent.com/glynnbird/usstatesgeojson/master/maryland.geojson",
        None,
    ),
    # DC comes from the all-states file, filtered by name.
    (
        "dc",
        "https://raw.githubusercontent.com/PublicaMundi/MappingAPI/master/data/geojson/us-states.json",
        "District of Columbia",
    ),
]


def _rings(geom: dict):
    t = geom.get("type")
    if t == "Polygon":
        return list(geom.get("coordinates", []))
    if t == "MultiPolygon":
        return [ring for poly in geom.get("coordinates", []) for ring in poly]
    return []


def _in_box(lon: float, lat: float) -> bool:
    return LON_MIN <= lon <= LON_MAX and LAT_MIN <= lat <= LAT_MAX


def _clip_ring(ring: list) -> list[list]:
    """Split a GeoJSON ring ([lon,lat] points) into [lat,lon] polylines covering
    the box. A run of in-box points is extended by one neighbour on each side so
    lines reach the edges."""
    lines: list[list] = []
    cur: list = []
    for i, (lon, lat) in enumerate(ring):
        if _in_box(lon, lat):
            if not cur and i > 0:  # reach back to the previous (outside) point
                plon, plat = ring[i - 1]
                cur.append([round(plat, 4), round(plon, 4)])
            cur.append([round(lat, 4), round(lon, 4)])
        else:
            if cur:
                cur.append([round(lat, 4), round(lon, 4)])  # reach out to the edge
                if len(cur) >= 2:
                    lines.append(cur)
                cur = []
    if len(cur) >= 2:
        lines.append(cur)
    return lines


def main() -> None:
    all_lines: list[list] = []
    for name, url, filter_name in SOURCES:
        try:
            data = requests.get(url, timeout=40).json()
        except Exception as exc:  # noqa: BLE001
            print(f"  {name}: FETCH FAILED ({exc!r}) — skipped")
            continue
        feats = data.get("features") if "features" in data else [data]
        for feat in feats:
            if filter_name and feat.get("properties", {}).get("name") != filter_name:
                continue
            for ring in _rings(feat.get("geometry", {})):
                all_lines.extend(_clip_ring(ring))
        print(f"  {name}: {sum(len(line) for line in all_lines)} cumulative points")

    OUT.write_text(
        json.dumps({"lines": all_lines}, separators=(",", ":")), encoding="utf-8"
    )
    pts = sum(len(line) for line in all_lines)
    print(
        f"radar_boundaries: {len(all_lines)} lines / {pts} points -> {OUT} "
        f"({OUT.stat().st_size // 1024} KB)"
    )


if __name__ == "__main__":
    main()
