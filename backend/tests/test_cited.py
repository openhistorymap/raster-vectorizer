"""Cited GeoJSON: source registry, citations round-trip, export and import."""
from __future__ import annotations

import json

SRC = "https://www.zotero.org/groups/3757017/items/ABCD1234"
SCAN = "https://example.org/scans/carta-1702"
POLY = {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]}


def _registry():
    return {
        SRC: {"title": "Cose notabili", "type": "text", "derivedFrom": [SCAN]},
        SCAN: {"title": "Carta di Bologna, 1702", "type": "map",
               "files": [{"id": "sheet-3", "url": "https://example.org/scans/carta-1702/3.tif",
                          "checksum": "sha256:" + "a" * 64}]},
    }


def _feature(**extra):
    return {"type": "Feature", "geometry": POLY, "properties": {"name": "Palazzo"}, **extra}


def test_registry_roundtrip_and_validation(client):
    assert client.get("/api/worlds/fakeworld/sources").json() == {}
    r = client.put("/api/worlds/fakeworld/sources", json=_registry())
    assert r.status_code == 200
    assert client.get("/api/worlds/fakeworld/sources").json() == _registry()
    bad = {SRC: {"type": "text"}}                      # no title
    r = client.put("/api/worlds/fakeworld/sources", json=bad)
    assert r.status_code == 422 and "title" in json.dumps(r.json())
    dangling = {SRC: {"title": "x", "derivedFrom": ["https://nowhere.example"]}}
    assert client.put("/api/worlds/fakeworld/sources", json=dangling).status_code == 422


def test_citations_are_stored_and_exported(client):
    client.put("/api/worlds/fakeworld/sources", json=_registry())
    cit = {"source": SCAN, "file": "sheet-3", "method": "traced", "supports": ["geometry"],
           "selector": [{"type": "FragmentSelector", "value": "xywh=10,20,30,40"}]}
    r = client.put("/api/worlds/fakeworld/layers/buildings",
                   json={"type": "FeatureCollection", "features": [_feature(citations=[cit])]})
    assert r.status_code == 200
    fc = client.get("/api/worlds/fakeworld/layers/buildings").json()
    assert fc["features"][0]["citations"] == [cit]

    r = client.get("/api/worlds/fakeworld/layers/buildings/cited-geojson")
    assert r.status_code == 200, r.text
    doc = r.json()
    assert doc["conformsTo"] == ["https://www.openhistorymap.org/cited-geojson/0.1"]
    assert set(doc["sources"]) == {SCAN}                   # only what is cited (+ ancestors)
    assert doc["features"][0]["citations"] == [cit]


def test_export_refuses_citations_of_unregistered_sources(client):
    r = client.put("/api/worlds/fakeworld/layers/b", json={"type": "FeatureCollection", "features": [
        _feature(citations=[{"source": "https://unknown.example/src"}])]})
    assert r.status_code == 200
    r = client.get("/api/worlds/fakeworld/layers/b/cited-geojson")
    assert r.status_code == 422
    assert "not in the world's source registry" in json.dumps(r.json())


def test_import_merges_sources_and_saves_features(client):
    client.put("/api/worlds/fakeworld/sources", json={SCAN: _registry()[SCAN]})
    doc = {
        "type": "FeatureCollection",
        "conformsTo": ["https://www.openhistorymap.org/cited-geojson/0.1"],
        "sources": _registry() | {SCAN: {"title": "A different title", "type": "map",
                                          "files": _registry()[SCAN]["files"]}},
        "features": [{**_feature(citations=[{"source": SRC, "method": "transcribed",
                                              "selector": [{"type": "PageSelector", "pageStart": 12}]}]),
                      "id": "11111111-1111-4111-8111-111111111111"}],
    }
    r = client.put("/api/worlds/fakeworld/layers/imported/cited-geojson", json=doc)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["inserted"] == 1
    assert body["sources"] == {"added": [SRC], "conflicts": [SCAN]}   # registry entry kept, difference reported
    feat = client.get("/api/worlds/fakeworld/layers/imported").json()["features"][0]
    assert feat["id"] == "11111111-1111-4111-8111-111111111111"   # the document's stable id is kept
    assert feat["citations"][0]["source"] == SRC
    reg = client.get("/api/worlds/fakeworld/sources").json()
    assert reg[SCAN]["title"] == "Carta di Bologna, 1702"


def test_import_rejects_invalid_documents(client):
    r = client.put("/api/worlds/fakeworld/layers/x/cited-geojson",
                   json={"type": "FeatureCollection", "features": []})   # no conformsTo / sources
    assert r.status_code == 422
