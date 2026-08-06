"""Generate the dotted-world basemap (static/dotmap.json) from the bundled
Natural Earth land polygons.

The modern intel-board look renders continents as a grid of glowing dots
(Stripe/Vercel/ShadowBroker style) instead of filled polygons. Rather than
adding a runtime dependency (e.g. the dotted-map npm lib), this offline
generator rasterizes the SAME ne_110m_land.json the dashboard already bundles:
sample an equirectangular grid over the 720x380 viewBox, keep the points that
fall inside a land ring (ray-casting with a bbox prefilter), and write the
result as one small static JSON the page draws directly.

Run once (and re-run only if the land data or grid spacing changes):
    .venv/bin/python scripts/generate_dotmap.py
"""

from __future__ import annotations

import json
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "brief" / "window" / "static"
LAND = STATIC / "ne_110m_land.json"
OUT = STATIC / "dotmap.json"

# MUST match dashboard.html's projection (W/H/LAT_TOP/LAT_BOTTOM). Cropped to the
# populated world — no Antarctica / empty polar oceans.
W, H = 720.0, 278.0  # dashboard.html's SVG viewBox
LAT_TOP, LAT_BOTTOM = 82.0, -57.0
SPACING = 4.4  # px between dot centers — ~2.2 deg; tuned for wall legibility
DOT_R = 1.35  # dot radius the client should draw (carried in the JSON)


def lon_to_x(lon: float) -> float:
    return (lon + 180.0) / 360.0 * W


def lat_to_y(lat: float) -> float:
    return (LAT_TOP - lat) / (LAT_TOP - LAT_BOTTOM) * H


def x_to_lon(x: float) -> float:
    return x / W * 360.0 - 180.0


def y_to_lat(y: float) -> float:
    return LAT_TOP - y / H * (LAT_TOP - LAT_BOTTOM)


def point_in_ring(lon: float, lat: float, ring: list) -> bool:
    """Standard ray-casting odd/even test."""
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def rings_with_bbox(geo: dict) -> list[tuple[tuple[float, float, float, float], list]]:
    """Flatten every polygon's OUTER ring (110m land has no meaningful holes at
    this dot resolution) with a precomputed bbox for the cheap prefilter."""
    out = []
    for feature in geo.get("features", []):
        geom = feature.get("geometry") or {}
        if geom.get("type") == "Polygon":
            polys = [geom["coordinates"]]
        elif geom.get("type") == "MultiPolygon":
            polys = geom["coordinates"]
        else:
            continue
        for poly in polys:
            ring = poly[0]
            lons = [p[0] for p in ring]
            lats = [p[1] for p in ring]
            out.append(((min(lons), min(lats), max(lons), max(lats)), ring))
    return out


def main() -> None:
    geo = json.loads(LAND.read_text(encoding="utf-8"))
    rings = rings_with_bbox(geo)
    dots: list[list[float]] = []
    y = SPACING / 2
    while y < H:
        x = SPACING / 2
        lat = y_to_lat(y)
        while x < W:
            lon = x_to_lon(x)
            for (lo, la, hi, ha), ring in rings:
                if (
                    lo <= lon <= hi
                    and la <= lat <= ha
                    and point_in_ring(lon, lat, ring)
                ):
                    dots.append([round(x, 1), round(y, 1)])
                    break
            x += SPACING
        y += SPACING

    OUT.write_text(
        json.dumps({"w": W, "h": H, "r": DOT_R, "dots": dots}, separators=(",", ":")),
        encoding="utf-8",
    )
    print(f"dotmap: {len(dots)} land dots -> {OUT} ({OUT.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
