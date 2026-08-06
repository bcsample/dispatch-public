"""World Delta — a stateful ingestor. Where RSS gives discrete articles, this
gives "the number changed / a new marker appeared": each adapter reports the
*current* state of a feed, we diff it against the last snapshot in SQLite, and
emit a normal Item per significant change. Those Items flow through the
existing dedupe -> score -> generate -> store -> deliver spine untouched.

Design, Phase 1):
  - An adapter is `fetch_state(feed: dict) -> list[dict]`, returning current
    records. Each record has a stable `key`, a `value` (the string the delta
    hash is computed over), and optional `title`/`url`/`raw_text`/`lat`/`lon`/
    `severity`.
  - The engine loads the prior snapshot per feed, diffs (new / changed / gone),
    writes the new snapshot, and emits an Item per delta.
  - First run for a feed (empty prior snapshot) seeds silently — it must never
    flood the brief with "new" for everything that already existed.
  - One dead/misconfigured feed logs and is skipped; the sweep completes.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import requests

from .. import applog
from ..models import Item

log = applog.get(__name__)

DEFAULT_USGS_URL = (
    "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_day.geojson"
)
FIRMS_AREA_URL = "https://firms.modaps.eosdis.nasa.gov/api/area/csv/{map_key}/{product}/{area}/{day_range}"
OFAC_SDN_URL = (
    "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/SDN.CSV"
)
OFAC_SEARCH_URL = "https://sanctionssearch.ofac.treas.gov/"
OPENSKY_TOKEN_URL = "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"
OPENSKY_STATES_URL = "https://opensky-network.org/api/states/all"


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# USGS earthquakes — GeoJSON summary feed. Free, no key.
# ---------------------------------------------------------------------------


def _parse_usgs(data: dict) -> list[dict]:
    """Pure parse of a USGS GeoJSON summary feed payload -> delta records.
    Kept separate from the HTTP fetch so tests can feed it a fixture directly."""
    records: list[dict] = []
    for feature in data.get("features", []) or []:
        props = feature.get("properties", {}) or {}
        geom = feature.get("geometry", {}) or {}
        coords = geom.get("coordinates") or [None, None, None]
        lon, lat = coords[0], coords[1]
        mag = _to_float(props.get("mag"))
        place = props.get("place") or "unknown location"
        event_id = feature.get("id", "")
        if not event_id:
            continue
        records.append(
            {
                "key": event_id,
                "value": f"mag={mag}|place={place}|updated={props.get('updated')}",
                "title": (
                    f"M{mag} earthquake — {place}"
                    if mag is not None
                    else f"Earthquake — {place}"
                ),
                "url": props.get("url", ""),
                "raw_text": f"Magnitude {mag} earthquake, {place}.",
                "lat": lat,
                "lon": lon,
                "severity": mag,
            }
        )
    return records


def usgs_quakes(feed: dict) -> list[dict]:
    url = feed.get("url", DEFAULT_USGS_URL)
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    return _parse_usgs(resp.json())


# ---------------------------------------------------------------------------
# NASA FIRMS active fires — CSV area API. Free, but requires a registered
# MAP_KEY (https://firms.modaps.eosdis.nasa.gov/api/map_key/). Never put the
# key in yaml/git: read it from an environment variable and skip the feed
# cleanly (fail-soft) if it's absent.
# ---------------------------------------------------------------------------


def _parse_firms_csv(text: str) -> list[dict]:
    """Pure parse of a FIRMS area-CSV payload -> delta records. FIRMS re-publishes
    raw detections rather than stable IDs, so the delta key is a location+day
    cell (rounded lat/lon + acquisition date) — that's what the plan calls
    "fire id/location cell"."""
    records: list[dict] = []
    reader = csv.DictReader(io.StringIO(text))
    for row in reader:
        lat = _to_float(row.get("latitude"))
        lon = _to_float(row.get("longitude"))
        if lat is None or lon is None:
            continue
        frp = _to_float(row.get("frp"))
        confidence = row.get("confidence", "")
        acq_date = row.get("acq_date", "")
        acq_time = row.get("acq_time", "")
        key = f"{round(lat, 2)}:{round(lon, 2)}:{acq_date}"
        records.append(
            {
                "key": key,
                "value": f"frp={frp}|confidence={confidence}|acq_time={acq_time}",
                "title": f"Active fire detection (FRP {frp}, confidence {confidence})",
                "url": "",
                "raw_text": f"Fire detected at {lat},{lon} on {acq_date} {acq_time}.",
                "lat": lat,
                "lon": lon,
                "severity": frp,
            }
        )
    return records


def nasa_firms(feed: dict) -> list[dict]:
    map_key = os.environ.get("NASA_FIRMS_MAP_KEY", "").strip()
    if not map_key:
        raise RuntimeError(
            "NASA_FIRMS_MAP_KEY not set — register a free key at "
            "https://firms.modaps.eosdis.nasa.gov/api/map_key/ and export it; "
            "skipping this feed"
        )
    url = FIRMS_AREA_URL.format(
        map_key=map_key,
        product=feed.get("product", "VIIRS_SNPP_NRT"),
        area=feed.get("area", "world"),
        day_range=feed.get("day_range", 1),
    )
    resp = requests.get(url, timeout=20)
    resp.raise_for_status()
    return _parse_firms_csv(resp.text)


# ---------------------------------------------------------------------------
# OFAC SDN list — sanctions delta. Free public download, no key. The SDN.CSV
# format is a documented, unheadered, 12-column layout (ent_num, SDN_Name,
# SDN_Type, Program, Title, Call_Sign, Vess_type, Tonnage, GRT, Vess_flag,
# Vess_owner, Remarks); empty fields are the literal placeholder "-0-". The
# `ent_num` column is the stable per-entry uid the plan calls for — additions
# are new ent_nums, removals are ent_nums that drop out of the file.
# ---------------------------------------------------------------------------

_SDN_COLUMNS = (
    "ent_num",
    "sdn_name",
    "sdn_type",
    "program",
    "title",
    "call_sign",
    "vess_type",
    "tonnage",
    "grt",
    "vess_flag",
    "vess_owner",
    "remarks",
)


def _sdn_clean(value: str) -> str:
    value = (value or "").strip()
    return "" if value == "-0-" else value


def _parse_sdn_csv(text: str) -> list[dict]:
    """Pure parse of an OFAC SDN.CSV payload -> delta records. Kept separate
    from the HTTP fetch so tests can feed it a fixture directly."""
    records: list[dict] = []
    reader = csv.reader(io.StringIO(text))
    for row in reader:
        if len(row) < len(_SDN_COLUMNS):
            continue  # malformed/trailing row (e.g. a stray blank line)
        fields = dict(zip(_SDN_COLUMNS, (_sdn_clean(v) for v in row)))
        ent_num = fields["ent_num"]
        if not ent_num:
            continue
        sdn_name = fields["sdn_name"] or f"SDN entry {ent_num}"
        sdn_type = fields["sdn_type"] or "entity"
        program = fields["program"]
        remarks = fields["remarks"]
        # Every field feeds the value string, so any edit to an existing entry
        # (program change, remarks update, etc.) registers as "changed", not
        # just pure additions/removals.
        value = "|".join(fields[c] for c in _SDN_COLUMNS if c != "ent_num")
        detail = f"Program: {program or 'n/a'}."
        if remarks:
            detail += f" {remarks}"
        records.append(
            {
                "key": ent_num,
                "value": value,
                "title": f"{sdn_name} ({sdn_type})",
                "url": OFAC_SEARCH_URL,
                "raw_text": detail,
                "lat": None,
                "lon": None,
                "severity": None,  # presence/absence is the signal, not a magnitude
            }
        )
    return records


def ofac_sdn(feed: dict) -> list[dict]:
    url = feed.get("url", OFAC_SDN_URL)
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    return _parse_sdn_csv(resp.text)


# ---------------------------------------------------------------------------
# Flights near a watch-box — OpenSky REST `/states/all`, bounded by a lat/lon
# box from feed config (lamin/lomin/lamax/lomax). OpenSky requires OAuth2
# client-credentials; per the house rule, secrets never live in yaml/git —
# read them from the environment or a token file. Absent creds means this
# adapter raises so `fetch_all` skips it fail-soft, same contract as FIRMS.
# ---------------------------------------------------------------------------


def _opensky_token() -> str:
    token_file = os.environ.get("OPENSKY_TOKEN_FILE", "").strip()
    if token_file:
        path = Path(token_file).expanduser()
        if not path.exists():
            raise RuntimeError(f"OPENSKY_TOKEN_FILE set but file not found: {path}")
        token = path.read_text(encoding="utf-8").strip()
        if not token:
            raise RuntimeError(f"OPENSKY_TOKEN_FILE at {path} is empty")
        return token

    client_id = os.environ.get("OPENSKY_CLIENT_ID", "").strip()
    client_secret = os.environ.get("OPENSKY_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        raise RuntimeError(
            "OpenSky OAuth2 credentials not set — export OPENSKY_CLIENT_ID / "
            "OPENSKY_CLIENT_SECRET (or OPENSKY_TOKEN_FILE pointing at a 0600 "
            "file holding a bearer token). Register API client credentials at "
            "https://opensky-network.org/my-opensky/account; skipping this feed"
        )
    resp = requests.post(
        OPENSKY_TOKEN_URL,
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        },
        timeout=15,
    )
    resp.raise_for_status()
    token = (resp.json() or {}).get("access_token", "")
    if not token:
        raise RuntimeError("OpenSky token response did not include access_token")
    return token


def _parse_opensky_states(data: dict) -> list[dict]:
    """Pure parse of an OpenSky /states/all payload -> delta records. Kept
    separate from the HTTP/OAuth fetch so tests can feed it a fixture
    directly. State vector layout per OpenSky's documented schema: index 0
    icao24, 1 callsign, 5 longitude, 6 latitude, 8 on_ground, 9 velocity."""
    records: list[dict] = []
    for state in data.get("states") or []:
        if not state or len(state) < 10:
            continue
        icao24 = (state[0] or "").strip()
        if not icao24:
            continue
        callsign = (state[1] or "").strip()
        lon = _to_float(state[5])
        lat = _to_float(state[6])
        on_ground = bool(state[8])
        velocity = _to_float(state[9])
        label = callsign or icao24
        records.append(
            {
                "key": icao24,
                # position/velocity/on_ground in the value: movement registers as
                # "changed" even when the callsign stays the same.
                "value": f"callsign={callsign}|lat={lat}|lon={lon}|on_ground={on_ground}|velocity={velocity}",
                "title": f"Aircraft {label} in watch-box",
                "url": f"https://opensky-network.org/aircraft-profile?icao24={icao24}",
                "raw_text": f"{label} ({icao24}) at {lat},{lon}, "
                f"{'on ground' if on_ground else 'airborne'}, velocity {velocity} m/s.",
                "lat": lat,
                "lon": lon,
                "severity": None,  # presence/movement is the signal, not a magnitude
            }
        )
    return records


def opensky_flights(feed: dict) -> list[dict]:
    token = _opensky_token()
    params = {
        "lamin": feed.get("lamin"),
        "lomin": feed.get("lomin"),
        "lamax": feed.get("lamax"),
        "lomax": feed.get("lomax"),
    }
    resp = requests.get(
        OPENSKY_STATES_URL,
        params=params,
        headers={"Authorization": f"Bearer {token}"},
        timeout=20,
    )
    resp.raise_for_status()
    return _parse_opensky_states(resp.json())


# ---------------------------------------------------------------------------
# CISA KEV — Known Exploited Vulnerabilities catalog. Free, no key. A CVE newly
# added to the catalog is a real "exploited in the wild right now" event; the
# delta engine seeds the ~1.6k existing entries silently on first run and then
# emits one "new" delta per addition. No geo (presence signal, like OFAC).
# ---------------------------------------------------------------------------
CISA_KEV_URL = (
    "https://www.cisa.gov/sites/default/files/feeds/"
    "known_exploited_vulnerabilities.json"
)


def _parse_cisa_kev(data: dict) -> list[dict]:
    """Pure parse of the KEV catalog payload -> delta records, keyed by cveID.
    value carries dateAdded so a re-listed CVE doesn't spuriously re-fire."""
    records: list[dict] = []
    for v in data.get("vulnerabilities") or []:
        cve = (v.get("cveID") or "").strip()
        if not cve:
            continue
        vendor = (v.get("vendorProject") or "").strip()
        product = (v.get("product") or "").strip()
        name = (v.get("vulnerabilityName") or "").strip()
        ransomware = (v.get("knownRansomwareCampaignUse") or "").strip().lower() == (
            "known"
        )
        label = f"{cve} — {vendor} {product}".strip()
        if name:
            label += f": {name}"
        if ransomware:
            label += " [ransomware]"
        records.append(
            {
                "key": cve,
                "value": f"{cve}|{v.get('dateAdded', '')}",
                "title": label,
                "url": f"https://nvd.nist.gov/vuln/detail/{cve}",
                "raw_text": (v.get("shortDescription") or "")[:2000],
                "lat": None,
                "lon": None,
                "severity": None,  # presence signal — exploited-in-wild is the point
            }
        )
    return records


def cisa_kev(feed: dict) -> list[dict]:
    url = feed.get("url") or CISA_KEV_URL
    resp = requests.get(url, timeout=30, headers={"User-Agent": "dispatch/1.0"})
    resp.raise_for_status()
    return _parse_cisa_kev(resp.json())


ADAPTERS: dict[str, Callable[[dict], list[dict]]] = {
    "usgs_quakes": usgs_quakes,
    "nasa_firms": nasa_firms,
    "ofac_sdn": ofac_sdn,
    "opensky_flights": opensky_flights,
    "cisa_kev": cisa_kev,
}


# ---------------------------------------------------------------------------
# The delta engine — generic across adapters.
# ---------------------------------------------------------------------------

_KIND_PREFIX = {"new": "New", "changed": "Updated", "gone": "No longer active"}


def _value_hash(value: str) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()[:16]


def _load_snapshot(con: sqlite3.Connection, feed_name: str) -> dict[str, dict]:
    rows = con.execute(
        "SELECT record_key, value_hash, payload FROM feed_snapshots WHERE feed_name = ?",
        (feed_name,),
    ).fetchall()
    out = {}
    for r in rows:
        try:
            payload = json.loads(r["payload"]) if r["payload"] else {}
        except json.JSONDecodeError:
            payload = {}
        out[r["record_key"]] = {"value_hash": r["value_hash"], "payload": payload}
    return out


def _write_snapshot(
    con: sqlite3.Connection,
    feed_name: str,
    record_key: str,
    value_hash: str,
    payload: dict,
) -> None:
    con.execute(
        "INSERT OR REPLACE INTO feed_snapshots (feed_name, record_key, value_hash, payload) "
        "VALUES (?, ?, ?, ?)",
        (feed_name, record_key, value_hash, json.dumps(payload)),
    )


def _delete_snapshot(con: sqlite3.Connection, feed_name: str, record_key: str) -> None:
    con.execute(
        "DELETE FROM feed_snapshots WHERE feed_name = ? AND record_key = ?",
        (feed_name, record_key),
    )


def _build_item(feed: dict, record: dict, kind: str) -> Item:
    label = record.get("title") or record.get("key", "")
    return Item(
        source_name=feed.get("name", "World"),
        source_type="world",
        title=f"{_KIND_PREFIX[kind]}: {label}",
        url=record.get("url", "") or "",
        published_at=_utcnow(),
        raw_text=record.get("raw_text", "") or "",
        trust=feed.get("trust", "medium"),
        lat=record.get("lat"),
        lon=record.get("lon"),
        severity=record.get("severity"),
        delta_kind=kind,
    )


def _apply_min_severity(feed: dict, records: list[dict]) -> list[dict]:
    """The per-feed `min_severity` floor from world_feeds.yaml: a record below
    it is not even considered (delta or current-state display). Shared by
    diff_feed and fetch_all's `current` out-param so the floor is applied in
    exactly one place."""
    min_severity = feed.get("min_severity") or 0
    return [
        r for r in records if r.get("severity") is None or r["severity"] >= min_severity
    ]


def diff_feed(
    con: sqlite3.Connection, feed: dict, current_records: list[dict]
) -> list[Item]:
    """Diff one feed's current state against its last snapshot; write the new
    snapshot; return an Item per significant change. First run for this feed
    (empty prior snapshot) seeds silently and returns nothing."""
    feed_name = feed.get("name", "World")

    filtered = _apply_min_severity(feed, current_records)

    prior = _load_snapshot(con, feed_name)
    first_run = len(prior) == 0

    items: list[Item] = []
    current_keys: set[str] = set()

    for record in filtered:
        key = record.get("key")
        if not key:
            continue
        current_keys.add(key)
        value_hash = _value_hash(record.get("value", ""))
        prior_entry = prior.get(key)
        if prior_entry is None:
            kind = "new"
        elif prior_entry["value_hash"] != value_hash:
            kind = "changed"
        else:
            kind = None
        if kind and not first_run:
            items.append(_build_item(feed, record, kind))
        _write_snapshot(con, feed_name, key, value_hash, record)

    gone_keys = set(prior.keys()) - current_keys
    for key in gone_keys:
        if not first_run:
            items.append(_build_item(feed, prior[key]["payload"], "gone"))
        _delete_snapshot(con, feed_name, key)

    con.commit()
    return items


def _current_record_view(record: dict) -> dict:
    """Trim a full adapter record down to the /api/current shape (v2)."""
    return {
        "key": record.get("key"),
        "title": record.get("title") or record.get("key", ""),
        "lat": record.get("lat"),
        "lon": record.get("lon"),
        "severity": record.get("severity"),
        "url": record.get("url", "") or "",
    }


def fetch_all(
    world_feeds: list[dict],
    con: sqlite3.Connection,
    stats: list[dict] | None = None,
    current: list[dict] | None = None,
) -> list[Item]:
    """Entry point the pipeline (and the Open Window sweep loop) calls.
    Fail-soft: one dead/misconfigured feed logs and is skipped, the sweep
    completes.

    `stats` is optional and additive: when a caller passes a list, one dict
    per feed (`name`, `ok`, `records`, `deltas_last_sweep`) is appended so a
    caller like the window service can report per-feed health without this
    function's core diff/log behavior changing at all for existing callers
    (the daily pipeline, the Phase 1/2 tests) that don't pass it.

    `current` is likewise optional and additive (v2 — see: when a caller passes a list,
    one dict per feed (`name`, `records`) is appended, where `records` is
    that feed's full current state (the same min_severity floor diff_feed
    already applies), trimmed to `{key,title,lat,lon,severity,url}`. This is
    what lets the window show "the world now", not just what changed. When
    omitted, behavior and return value are byte-identical to before this
    param existed."""
    items: list[Item] = []
    for feed in world_feeds:
        name = feed.get("name", "?")
        adapter = ADAPTERS.get(feed.get("adapter", ""))
        if adapter is None:
            log.error("%s: FAILED (unknown adapter %r)", name, feed.get("adapter"))
            if stats is not None:
                stats.append(
                    {"name": name, "ok": False, "records": 0, "deltas_last_sweep": 0}
                )
            if current is not None:
                current.append({"name": name, "records": []})
            continue
        try:
            records = adapter(feed)
        except Exception as exc:  # noqa: BLE001 — one bad feed shouldn't kill the sweep
            log.error("%s: FAILED (%r)", name, exc)
            if stats is not None:
                stats.append(
                    {"name": name, "ok": False, "records": 0, "deltas_last_sweep": 0}
                )
            if current is not None:
                current.append({"name": name, "records": []})
            continue
        if current is not None:
            current.append(
                {
                    "name": name,
                    "records": [
                        _current_record_view(r)
                        for r in _apply_min_severity(feed, records)
                    ],
                }
            )
        try:
            deltas = diff_feed(con, feed, records)
        except (
            Exception
        ) as exc:  # noqa: BLE001 — same fail-soft contract for the diff step
            log.error("%s: diff FAILED (%r)", name, exc)
            if stats is not None:
                stats.append(
                    {
                        "name": name,
                        "ok": False,
                        "records": len(records),
                        "deltas_last_sweep": 0,
                    }
                )
            continue
        items.extend(deltas)
        log.info("%s: %d changes (%d current records)", name, len(deltas), len(records))
        if stats is not None:
            stats.append(
                {
                    "name": name,
                    "ok": True,
                    "records": len(records),
                    "deltas_last_sweep": len(deltas),
                }
            )
    return items
