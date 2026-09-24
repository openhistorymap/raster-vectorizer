"""Saving a layer must not destroy what the editor did not touch.

Regression for the original save path, which deleted every row and re-inserted only `name`,
`class` and `properties`: on tables that keep attributes in their own columns (from_time, to_time,
subclass, wiki, ... as in the krynn/claude PostGIS worlds) one save wiped all of them and
renumbered every feature.

The same scenarios run against SpatiaLite always, and against PostGIS when OFM_TEST_PG_HOST is set
(docker compose's test profile starts a throwaway PostGIS for that).
"""
from __future__ import annotations

import json
import os
import uuid

import pytest

from backend.storage.diff import SaveRejected

POLY = {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]}
POLY2 = {"type": "Polygon", "coordinates": [[[2, 2], [3, 2], [3, 3], [2, 3], [2, 2]]]}
POLY_Z = {"type": "Polygon", "coordinates": [[[5, 5, 0], [6, 5, 0], [6, 6, 0], [5, 6, 0], [5, 5, 0]]]}
LINE = {"type": "LineString", "coordinates": [[0, 0], [1, 1]]}


# --------------------------------------------------------------------------- backends

def _spatialite(tmp_path):
    from backend.storage.spatialite import SpatiaLiteAdapter

    wdir = tmp_path / "legacy"
    wdir.mkdir()
    a = SpatiaLiteAdapter(wdir)
    a.conn.execute(
        "CREATE TABLE politics (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, class TEXT, "
        "subclass TEXT, wiki TEXT, from_time REAL, to_time REAL)")
    a.conn.execute("SELECT AddGeometryColumn('politics', 'the_geom', 4326, 'MULTIPOLYGON', 'XY')")
    for name, sub, ft in (("Wengnga", "city league", 1078), ("Fisar", "caliphate", 986)):
        a.conn.execute(
            "INSERT INTO politics (name, class, subclass, wiki, from_time, to_time, the_geom) "
            "VALUES (?, 'political', ?, 'https://example.org/wiki', ?, NULL, "
            "GeomFromText('MULTIPOLYGON(((0 0,1 0,1 1,0 1,0 0)))', 4326))", (name, sub, ft))
    a.conn.commit()
    return a


def _history_spatialite(a):
    if not a.conn.execute("SELECT 1 FROM sqlite_master WHERE name = '_ofm_history'").fetchone():
        return []
    rows = a.conn.execute("SELECT layer, fid, op, before, after, editor FROM _ofm_history ORDER BY id").fetchall()
    return [{"layer": r[0], "fid": r[1], "op": r[2], "before": json.loads(r[3]) if r[3] else None,
             "after": json.loads(r[4]) if r[4] else None, "editor": r[5]} for r in rows]


PG = {k: os.environ.get(f"OFM_TEST_PG_{k.upper()}") for k in ("host", "port", "user", "password", "db")}


def _postgis(tmp_path):
    if not PG["host"]:
        pytest.skip("set OFM_TEST_PG_HOST (and _PORT/_USER/_PASSWORD/_DB) to run PostGIS tests")
    import psycopg
    from backend.storage.postgis import PostGISAdapter

    a = PostGISAdapter(tmp_path, {k: v for k, v in PG.items() if v})
    with psycopg.connect(a.dsn) as c:
        c.execute("DROP TABLE IF EXISTS politics, _ofm_history")
        # Like the krynn/claude tables: attributes as columns, no `properties` column, and an
        # integer id without a default (as ogr2ogr-style imports create it).
        c.execute("CREATE TABLE politics (id integer PRIMARY KEY, geom geometry(MULTIPOLYGON, 4326), "
                  "name text, class text, subclass text, wiki text, from_time double precision, "
                  "to_time double precision)")
        for i, (name, sub, ft) in enumerate((("Wengnga", "city league", 1078), ("Fisar", "caliphate", 986)), 1):
            c.execute("INSERT INTO politics VALUES (%s, ST_GeomFromText('MULTIPOLYGON(((0 0,1 0,1 1,0 1,0 0)))', 4326), "
                      "%s, 'political', %s, 'https://example.org/wiki', %s, NULL)", (i, name, sub, ft))
    return a


def _history_postgis(a):
    import psycopg
    from psycopg.rows import dict_row
    with psycopg.connect(a.dsn, row_factory=dict_row) as c:
        if c.execute("SELECT to_regclass('_ofm_history') AS t").fetchone()["t"] is None:
            return []
        return c.execute("SELECT layer, fid, op, before, after, editor FROM _ofm_history ORDER BY id").fetchall()


@pytest.fixture(params=["spatialite", "postgis"])
def backend(request, tmp_path):
    if request.param == "spatialite":
        a = _spatialite(tmp_path)
        return a, lambda: _history_spatialite(a)
    a = _postgis(tmp_path)
    return a, lambda: _history_postgis(a)


def _by_name(fc):
    return {f["properties"]["name"]: f for f in fc["features"]}


# --------------------------------------------------------------------------- scenarios

def test_editing_one_feature_keeps_every_other_column(backend):
    a, _ = backend
    fc = a.load_layer("politics")
    w = _by_name(fc)["Wengnga"]
    w["properties"]["name"] = "Wengnga League"
    summary = a.save_layer("politics", fc, editor="tester")

    assert summary["updated"] == 1 and summary["unchanged"] == 1
    assert summary["inserted"] == 0 and summary["deleted"] == 0
    after = _by_name(a.load_layer("politics"))
    assert set(after) == {"Wengnga League", "Fisar"}
    for f in after.values():
        p = f["properties"]
        assert p["class"] == "political"
        assert p["wiki"] == "https://example.org/wiki"
        assert p["from_time"] in (1078, 986)
        assert p["subclass"] in ("city league", "caliphate")


def test_ids_become_stable_and_survive_saves(backend):
    a, _ = backend
    first = a.load_layer("politics")
    assert all(str(f["id"]).startswith("row:") for f in first["features"])  # legacy table, not migrated by reading
    a.save_layer("politics", first)
    ids1 = {f["properties"]["name"]: f["id"] for f in a.load_layer("politics")["features"]}
    assert all(uuid.UUID(i) for i in ids1.values())
    fc = a.load_layer("politics")
    _by_name(fc)["Fisar"]["geometry"] = {"type": "MultiPolygon", "coordinates": [POLY2["coordinates"]]}
    a.save_layer("politics", fc)
    ids2 = {f["properties"]["name"]: f["id"] for f in a.load_layer("politics")["features"]}
    assert ids1 == ids2


def test_insert_delete_and_history(backend):
    a, history = backend
    fc = a.load_layer("politics")
    fc["features"] = [f for f in fc["features"] if f["properties"]["name"] != "Fisar"]
    fc["features"].append({"type": "Feature", "id": "new_1", "geometry": POLY,  # promoted to Multi
                           "properties": {"name": "Muni", "class": "political", "from_time": 756}})
    s = a.save_layer("politics", fc, editor="tester")
    assert (s["inserted"], s["deleted"], s["unchanged"]) == (1, 1, 1)

    after = _by_name(a.load_layer("politics"))
    assert set(after) == {"Wengnga", "Muni"}
    assert after["Muni"]["geometry"]["type"] == "MultiPolygon"
    assert after["Muni"]["properties"]["from_time"] == 756

    h = history()
    ops = sorted(r["op"] for r in h)
    assert ops == ["delete", "insert"]
    deleted = next(r for r in h if r["op"] == "delete")
    assert deleted["before"]["properties"]["name"] == "Fisar"
    assert deleted["before"]["properties"]["subclass"] == "caliphate"   # full image, restorable
    assert all(r["editor"] == "tester" and r["layer"] == "politics" for r in h)


def test_unknown_attributes_are_reported_not_silently_dropped(backend):
    a, _ = backend
    fc = a.load_layer("politics")
    _by_name(fc)["Wengnga"]["properties"]["population"] = 60_000_000
    s = a.save_layer("politics", fc)
    assert s["unstored_attributes"] == ["population"]


def test_explicit_null_clears_but_absent_keeps(backend):
    a, _ = backend
    fc = a.load_layer("politics")
    w = _by_name(fc)["Wengnga"]
    w["properties"]["wiki"] = None           # explicit: clear it
    del w["properties"]["subclass"]          # absent: leave it alone
    a.save_layer("politics", fc)
    p = _by_name(a.load_layer("politics"))["Wengnga"]["properties"]
    assert "wiki" not in p
    assert p["subclass"] == "city league"


def test_z_coordinates_are_flattened(backend):
    a, _ = backend
    fc = a.load_layer("politics")
    fc["features"].append({"type": "Feature", "geometry": POLY_Z, "properties": {"name": "Z"}})
    a.save_layer("politics", fc)
    coords = _by_name(a.load_layer("politics"))["Z"]["geometry"]["coordinates"][0][0][0]
    assert len(coords) == 2


def test_bad_geometry_rejects_the_whole_save(backend):
    a, history = backend
    before = a.load_layer("politics")
    fc = a.load_layer("politics")
    _by_name(fc)["Wengnga"]["properties"]["name"] = "should not be saved"
    fc["features"].append({"type": "Feature", "geometry": LINE, "properties": {"name": "a road"}})
    with pytest.raises(SaveRejected):
        a.save_layer("politics", fc)
    # All or nothing: no rename, no new row, no history, and not even the fid migration.
    after = a.load_layer("politics")
    assert after == before
    assert all(str(f["id"]).startswith("row:") for f in after["features"])
    assert history() == []


# --------------------------------------------------------------------------- GeoJSON files

def test_geojson_file_keeps_ids_foreign_members_and_history(tmp_path):
    from backend.storage.geojson_file import GeoJSONFileAdapter

    a = GeoJSONFileAdapter(tmp_path / "w")
    path = tmp_path / "w" / "raw" / "geojson" / "places.geojson"
    path.write_text(json.dumps({
        "type": "FeatureCollection",
        "conformsTo": ["https://www.openhistorymap.org/cited-geojson/0.1"],
        "sources": {"https://example.org/src": {"title": "A source"}},
        "features": [{"type": "Feature", "geometry": POLY, "properties": {"name": "A"}},
                     {"type": "Feature", "geometry": POLY2, "properties": {"name": "B"}}]}))
    fc = a.load_layer("places")
    assert [f["id"] for f in fc["features"]] == ["row:0", "row:1"]
    fc["features"][0]["properties"]["name"] = "A2"
    s = a.save_layer("places", fc, editor="tester")
    assert (s["updated"], s["unchanged"]) == (1, 1)

    saved = json.loads(path.read_text())
    assert saved["sources"] == {"https://example.org/src": {"title": "A source"}}   # Cited GeoJSON survives
    ids = [f["id"] for f in saved["features"]]
    assert all(uuid.UUID(i) for i in ids)
    a.save_layer("places", a.load_layer("places"))
    assert [f["id"] for f in json.loads(path.read_text())["features"]] == ids

    log = [json.loads(line) for line in (tmp_path / "w" / "raw" / "history" / "places.jsonl").read_text().splitlines()]
    assert [(r["op"], r["before"]["properties"]["name"], r["after"]["properties"]["name"]) for r in log] == [("update", "A", "A2")]


def test_citations_column_is_added_and_round_trips(backend):
    a, history = backend
    cit = [{"source": "https://example.org/src", "method": "traced", "supports": ["geometry"],
            "selector": [{"type": "FragmentSelector", "value": "xywh=1,2,3,4"}]}]
    fc = a.load_layer("politics")
    _by_name(fc)["Wengnga"]["citations"] = cit
    s = a.save_layer("politics", fc, editor="tester")
    assert s["updated"] == 1 and s["unchanged"] == 1
    loaded = _by_name(a.load_layer("politics"))
    assert loaded["Wengnga"]["citations"] == cit
    assert "citations" not in loaded["Fisar"]
    assert loaded["Wengnga"]["properties"]["subclass"] == "city league"   # columns untouched

    fc = a.load_layer("politics")
    del _by_name(fc)["Wengnga"]["citations"]          # absent: keep
    assert a.save_layer("politics", fc)["unchanged"] == 2
    assert _by_name(a.load_layer("politics"))["Wengnga"]["citations"] == cit

    fc = a.load_layer("politics")
    _by_name(fc)["Wengnga"]["citations"] = []          # explicit empty: clear
    a.save_layer("politics", fc)
    assert "citations" not in _by_name(a.load_layer("politics"))["Wengnga"]

    updates = [r for r in history() if r["op"] == "update"]
    assert updates[0]["after"]["citations"] == cit and "citations" not in updates[0]["before"]
