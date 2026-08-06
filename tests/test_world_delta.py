"""Gate 1 for World Delta: seed a snapshot, mutate the state, assert exactly the
changed/new/gone keys become Items — and a first run (empty prior snapshot)
emits zero Items rather than flooding the brief."""

import sqlite3

from brief.db import _SCHEMA
from brief.ingest import world

FEED = {
    "name": "Test Quakes",
    "adapter": "usgs_quakes",
    "trust": "high",
    "min_severity": 0,
}


def _con() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(_SCHEMA)
    return con


def _rec(key, mag, title, lat=1.0, lon=2.0):
    return {
        "key": key,
        "value": f"mag={mag}",
        "title": title,
        "url": "",
        "raw_text": f"{title} detail",
        "lat": lat,
        "lon": lon,
        "severity": mag,
    }


def test_first_run_seeds_silently_and_writes_snapshot():
    con = _con()
    records = [_rec("eq1", 5.0, "Quake A"), _rec("eq2", 6.0, "Quake B")]

    items = world.diff_feed(con, FEED, records)

    assert items == []
    rows = con.execute(
        "SELECT record_key FROM feed_snapshots WHERE feed_name = ?", (FEED["name"],)
    ).fetchall()
    assert {r["record_key"] for r in rows} == {"eq1", "eq2"}


def test_diff_emits_exactly_new_changed_gone_and_nothing_for_unchanged():
    con = _con()
    seed = [
        _rec("eq1", 5.0, "Quake A"),  # stays unchanged next sweep
        _rec("eq2", 6.0, "Quake B"),  # will change magnitude
        _rec("eq3", 3.0, "Quake C"),  # will disappear
    ]
    world.diff_feed(con, FEED, seed)  # seed sweep — asserted silent above

    mutated = [
        _rec("eq1", 5.0, "Quake A"),  # unchanged -> no item
        _rec("eq2", 6.5, "Quake B"),  # changed value -> "changed"
        _rec("eq4", 8.0, "Quake D", lat=9.0, lon=10.0),  # new key -> "new"
        # eq3 dropped -> "gone"
    ]

    items = world.diff_feed(con, FEED, mutated)

    by_kind = {i.delta_kind: i for i in items}
    assert len(items) == 3
    assert set(by_kind) == {"new", "changed", "gone"}
    assert "Quake D" in by_kind["new"].title
    assert by_kind["new"].severity == 8.0
    assert by_kind["new"].lat == 9.0 and by_kind["new"].lon == 10.0
    assert "Quake B" in by_kind["changed"].title
    assert "Quake A" not in {
        i.title.split(": ", 1)[-1] for i in items
    }  # unchanged stayed silent
    assert "Quake C" in by_kind["gone"].title
    assert all(i.source_type == "world" for i in items)
    assert all(i.source_name == FEED["name"] for i in items)

    # snapshot now reflects the mutated state: eq3 pruned, eq4 present
    keys = {
        r["record_key"]
        for r in con.execute(
            "SELECT record_key FROM feed_snapshots WHERE feed_name = ?", (FEED["name"],)
        ).fetchall()
    }
    assert keys == {"eq1", "eq2", "eq4"}


def test_min_severity_floor_filters_before_diffing():
    con = _con()
    feed = {**FEED, "min_severity": 4.5}
    world.diff_feed(
        con, feed, [_rec("eq1", 2.0, "Small Quake")]
    )  # below floor, seed sweep
    items = world.diff_feed(
        con, feed, [_rec("eq1", 2.1, "Small Quake")]
    )  # still below floor
    assert items == []  # never considered, even though the value changed


def test_fetch_all_is_fail_soft_for_unknown_adapter_and_continues_sweep():
    con = _con()
    good_feed = {
        "name": "Good",
        "adapter": "usgs_quakes",
        "trust": "high",
        "min_severity": 0,
    }
    bad_feed = {
        "name": "Bad",
        "adapter": "does_not_exist",
        "trust": "high",
        "min_severity": 0,
    }

    # Monkeypatch the registry entry so "Good" doesn't hit the network.
    original = world.ADAPTERS["usgs_quakes"]
    world.ADAPTERS["usgs_quakes"] = lambda feed: [_rec("eqX", 5.0, "Quake X")]
    try:
        items = world.fetch_all([bad_feed, good_feed], con)
    finally:
        world.ADAPTERS["usgs_quakes"] = original

    # First run for "Good" -> seeds silently (zero items), but the sweep did not
    # abort because of the bad feed.
    assert items == []
    keys = {
        r["record_key"]
        for r in con.execute(
            "SELECT record_key FROM feed_snapshots WHERE feed_name = 'Good'"
        ).fetchall()
    }
    assert keys == {"eqX"}


def test_parse_usgs_geojson_fixture():
    fixture = {
        "features": [
            {
                "id": "us1234",
                "properties": {
                    "mag": 5.8,
                    "place": "30km NE of Tokyo, Japan",
                    "updated": 1700000000000,
                    "url": "https://example.test/us1234",
                },
                "geometry": {"type": "Point", "coordinates": [139.9, 35.9, 10.0]},
            }
        ]
    }
    records = world._parse_usgs(fixture)
    assert len(records) == 1
    r = records[0]
    assert r["key"] == "us1234"
    assert r["severity"] == 5.8
    assert r["lon"] == 139.9 and r["lat"] == 35.9
    assert "Tokyo" in r["title"]


def test_parse_cisa_kev_fixture():
    fixture = {
        "vulnerabilities": [
            {
                "cveID": "CVE-2026-1234",
                "vendorProject": "Microsoft",
                "product": "SharePoint",
                "vulnerabilityName": "RCE vulnerability",
                "dateAdded": "2026-07-16",
                "shortDescription": "Remote code execution.",
                "knownRansomwareCampaignUse": "Known",
            },
            {
                "cveID": "CVE-2026-9999",
                "vendorProject": "Fortinet",
                "product": "FortiSandbox",
                "vulnerabilityName": "Auth bypass",
                "dateAdded": "2026-07-16",
                "knownRansomwareCampaignUse": "Unknown",
            },
            {"cveID": "", "vendorProject": "x"},  # no cveID -> dropped
        ]
    }
    records = world._parse_cisa_kev(fixture)
    assert len(records) == 2
    r = records[0]
    assert r["key"] == "CVE-2026-1234"
    assert r["value"] == "CVE-2026-1234|2026-07-16"
    assert r["severity"] is None and r["lat"] is None
    assert "Microsoft SharePoint" in r["title"] and "[ransomware]" in r["title"]
    assert r["url"] == "https://nvd.nist.gov/vuln/detail/CVE-2026-1234"
    assert "[ransomware]" not in records[1]["title"]  # Unknown -> no tag


def test_cisa_kev_selected_in_v1_scope():
    from brief.window import service

    feeds = [{"name": "CISA Exploited Vulns", "adapter": "cisa_kev", "min_severity": 0}]
    assert service.select_v1_feeds(feeds)[0]["adapter"] == "cisa_kev"


def test_parse_firms_csv_fixture():
    csv_text = (
        "latitude,longitude,frp,confidence,acq_date,acq_time\n"
        "34.05,-118.25,12.3,nominal,2026-07-15,0930\n"
    )
    records = world._parse_firms_csv(csv_text)
    assert len(records) == 1
    r = records[0]
    assert r["key"] == "34.05:-118.25:2026-07-15"
    assert r["severity"] == 12.3
    assert r["lat"] == 34.05 and r["lon"] == -118.25


# ---------------------------------------------------------------------------
# Phase 2 — OFAC SDN sanctions delta. Fixture rows mirror the real, documented
# SDN.CSV export format: unheadered, 12 comma-separated fields (ent_num,
# SDN_Name, SDN_Type, Program, Title, Call_Sign, Vess_type, Tonnage, GRT,
# Vess_flag, Vess_owner, Remarks), "-0- " as the empty-field placeholder.
# ---------------------------------------------------------------------------

_SDN_FIXTURE_V1 = (
    '36,"AEROCARIBBEAN AIRLINES",-0- ,"CUBA",-0- ,-0- ,-0- ,-0- ,-0- ,-0- ,-0- ,-0- \n'
    '2674,"ABBAS, Abu","individual","SDGT","Director of PLF",-0- ,-0- ,-0- ,-0- ,-0- ,-0- ,'
    '"DOB 10 Dec 1948."\n'
)

# eq: entry 36 unchanged, entry 2674 gets its remarks updated, entry 906 is new.
_SDN_FIXTURE_V2 = (
    '36,"AEROCARIBBEAN AIRLINES",-0- ,"CUBA",-0- ,-0- ,-0- ,-0- ,-0- ,-0- ,-0- ,-0- \n'
    '2674,"ABBAS, Abu","individual","SDGT","Director of PLF",-0- ,-0- ,-0- ,-0- ,-0- ,-0- ,'
    '"DOB 10 Dec 1948; deceased."\n'
    '906,"HAVIN BANK LIMITED",-0- ,"CUBA",-0- ,-0- ,-0- ,-0- ,-0- ,-0- ,-0- ,'
    '"SWIFT/BIC HAVIGB2L."\n'
)


def test_parse_sdn_csv_fixture():
    records = world._parse_sdn_csv(_SDN_FIXTURE_V1)
    assert len(records) == 2
    by_key = {r["key"]: r for r in records}
    assert set(by_key) == {"36", "2674"}
    assert by_key["36"]["title"] == "AEROCARIBBEAN AIRLINES (entity)"
    assert "CUBA" in by_key["36"]["raw_text"]
    assert by_key["2674"]["title"] == "ABBAS, Abu (individual)"
    assert "DOB 10 Dec 1948" in by_key["2674"]["raw_text"]
    assert all(
        r["severity"] is None and r["lat"] is None and r["lon"] is None for r in records
    )


def test_sdn_delta_additions_removals_and_edits():
    con = _con()
    feed = {
        "name": "OFAC SDN",
        "adapter": "ofac_sdn",
        "trust": "high",
        "min_severity": 0,
    }

    seed_records = world._parse_sdn_csv(_SDN_FIXTURE_V1)
    first = world.diff_feed(con, feed, seed_records)
    assert first == []  # first run seeds silently

    mutated_records = world._parse_sdn_csv(_SDN_FIXTURE_V2)
    items = world.diff_feed(con, feed, mutated_records)

    by_kind = {}
    for i in items:
        by_kind.setdefault(i.delta_kind, []).append(i)

    assert set(by_kind) == {"changed", "new"}
    assert len(by_kind["changed"]) == 1 and "ABBAS" in by_kind["changed"][0].title
    assert len(by_kind["new"]) == 1 and "HAVIN BANK" in by_kind["new"][0].title
    # entry 36 was present in both and unchanged -> silent
    assert not any("AEROCARIBBEAN" in i.title for i in items)


def test_sdn_removal_emits_gone():
    con = _con()
    feed = {
        "name": "OFAC SDN",
        "adapter": "ofac_sdn",
        "trust": "high",
        "min_severity": 0,
    }
    seed_records = world._parse_sdn_csv(_SDN_FIXTURE_V1)
    world.diff_feed(con, feed, seed_records)

    # entry 2674 dropped entirely from the next export -> delisted
    remaining_only = [r for r in seed_records if r["key"] != "2674"]
    items = world.diff_feed(con, feed, remaining_only)

    assert len(items) == 1
    assert items[0].delta_kind == "gone"
    assert "ABBAS" in items[0].title


# ---------------------------------------------------------------------------
# Phase 2 — OpenSky flights near a watch-box. Fixture mirrors the documented
# /states/all state-vector layout (index 0 icao24, 1 callsign, 5 lon, 6 lat,
# 8 on_ground, 9 velocity).
# ---------------------------------------------------------------------------


def test_parse_opensky_states_fixture():
    fixture = {
        "time": 1784220452,
        "states": [
            [
                "44028c",
                "EJU931C ",
                "Austria",
                1784220451,
                1784220451,
                6.9818,
                46.5879,
                6964.68,
                False,
                177.73,
                5.31,
                -7.48,
                None,
                7391.4,
                "1000",
                False,
                0,
            ],
        ],
    }
    records = world._parse_opensky_states(fixture)
    assert len(records) == 1
    r = records[0]
    assert r["key"] == "44028c"
    assert "EJU931C" in r["title"]
    assert r["lat"] == 46.5879 and r["lon"] == 6.9818
    assert r["severity"] is None


def test_opensky_flights_raises_without_credentials(monkeypatch):
    for var in ("OPENSKY_CLIENT_ID", "OPENSKY_CLIENT_SECRET", "OPENSKY_TOKEN_FILE"):
        monkeypatch.delenv(var, raising=False)
    try:
        world.opensky_flights(
            {"name": "Flights", "lamin": 0, "lomin": 0, "lamax": 1, "lomax": 1}
        )
        assert False, "expected RuntimeError for missing OpenSky credentials"
    except RuntimeError as exc:
        assert "OPENSKY_CLIENT_ID" in str(exc) or "OPENSKY_TOKEN_FILE" in str(exc)


# ---------------------------------------------------------------------------
# v2 — fetch_all's `current` out-param (, "v2"
# section, V2.1). Mirrors the existing additive `stats` param: default None,
# byte-identical behavior/return value for callers that don't pass it.
# ---------------------------------------------------------------------------


def test_fetch_all_current_outparam_omitted_leaves_behavior_unchanged():
    con = _con()
    feed = {
        "name": "Good",
        "adapter": "usgs_quakes",
        "trust": "high",
        "min_severity": 0,
    }
    original = world.ADAPTERS["usgs_quakes"]
    world.ADAPTERS["usgs_quakes"] = lambda feed: [_rec("eqX", 5.0, "Quake X")]
    try:
        items = world.fetch_all([feed], con)  # current omitted entirely
    finally:
        world.ADAPTERS["usgs_quakes"] = original
    assert (
        items == []
    )  # first run still seeds silently, exactly as before this param existed


def test_fetch_all_current_outparam_captures_records_with_min_severity_floor():
    con = _con()
    feed = {
        "name": "Quakes",
        "adapter": "usgs_quakes",
        "trust": "high",
        "min_severity": 4.5,
    }
    original = world.ADAPTERS["usgs_quakes"]
    world.ADAPTERS["usgs_quakes"] = lambda feed: [
        _rec("eq1", 5.0, "Big Quake"),
        _rec("eq2", 2.0, "Small Quake"),
    ]
    try:
        current: list[dict] = []
        world.fetch_all([feed], con, current=current)
    finally:
        world.ADAPTERS["usgs_quakes"] = original

    assert len(current) == 1
    assert current[0]["name"] == "Quakes"
    keys = {r["key"] for r in current[0]["records"]}
    assert keys == {"eq1"}  # eq2 filtered out by the min_severity display floor
    rec = current[0]["records"][0]
    assert set(rec.keys()) == {"key", "title", "lat", "lon", "severity", "url"}
    assert rec["severity"] == 5.0


def test_fetch_all_current_outparam_empty_records_for_failed_or_unknown_feed():
    con = _con()
    bad_feed = {
        "name": "Bad",
        "adapter": "does_not_exist",
        "trust": "high",
        "min_severity": 0,
    }
    current: list[dict] = []
    world.fetch_all([bad_feed], con, current=current)
    assert current == [{"name": "Bad", "records": []}]


def test_opensky_movement_registers_as_changed():
    con = _con()
    feed = {
        "name": "Flights",
        "adapter": "opensky_flights",
        "trust": "medium",
        "min_severity": 0,
    }

    def _state(lat, lon):
        return {
            "states": [
                [
                    "abc123",
                    "TEST01  ",
                    "Nowhere",
                    1,
                    1,
                    lon,
                    lat,
                    100.0,
                    False,
                    200.0,
                    0.0,
                    0.0,
                    None,
                    100.0,
                    "1000",
                    False,
                    0,
                ]
            ]
        }

    seed = world._parse_opensky_states(_state(46.0, 7.0))
    assert world.diff_feed(con, feed, seed) == []  # first run seeds silently

    moved = world._parse_opensky_states(_state(46.5, 7.5))  # aircraft moved
    items = world.diff_feed(con, feed, moved)
    assert len(items) == 1
    assert items[0].delta_kind == "changed"
    assert items[0].lat == 46.5 and items[0].lon == 7.5
