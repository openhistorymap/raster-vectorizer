from pathlib import Path

import pytest

from backend.storage import for_world
from backend.storage.geojson_file import GeoJSONFileAdapter


def _fc(features=()):
    return {"type": "FeatureCollection", "features": list(features)}


def _poly(name, coords=((0, 0), (1, 0), (1, 1), (0, 1), (0, 0))):
    return {
        "type": "Feature",
        "geometry": {"type": "Polygon", "coordinates": [list(coords)]},
        "properties": {"name": name},
    }


def test_factory_picks_geojson_when_mode_geojson(ofm_root: Path):
    wdir = ofm_root / "fakeworld"
    a = for_world(wdir)
    assert a.__class__.__name__ == "GeoJSONFileAdapter"


def test_save_then_load_roundtrips(ofm_root: Path):
    a = GeoJSONFileAdapter(ofm_root / "fakeworld")
    assert a.save_layer("parks", _fc([_poly("Central"), _poly("North")]))["saved"] == 2
    loaded = a.load_layer("parks")
    assert loaded["type"] == "FeatureCollection"
    assert len(loaded["features"]) == 2
    assert {f["properties"]["name"] for f in loaded["features"]} == {"Central", "North"}


def test_load_missing_layer_is_empty(ofm_root: Path):
    a = GeoJSONFileAdapter(ofm_root / "fakeworld")
    fc = a.load_layer("doesnotexist")
    assert fc == {"type": "FeatureCollection", "features": []}


def test_list_layers_after_save(ofm_root: Path):
    a = GeoJSONFileAdapter(ofm_root / "fakeworld")
    a.save_layer("parks", _fc([_poly("p")]))
    a.save_layer("roads", _fc([
        {
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": [[0, 0], [1, 1]]},
            "properties": {},
        }
    ]))
    by_name = {r["name"]: r for r in a.list_layers()}
    assert by_name["parks"]["count"] == 1
    assert by_name["parks"]["geometry_type"] == "Polygon"
    assert by_name["roads"]["geometry_type"] == "LineString"


def test_unsafe_layer_name_rejected(ofm_root: Path):
    a = GeoJSONFileAdapter(ofm_root / "fakeworld")
    with pytest.raises(ValueError):
        a.save_layer("../etc/passwd", _fc())


def test_save_requires_featurecollection(ofm_root: Path):
    a = GeoJSONFileAdapter(ofm_root / "fakeworld")
    with pytest.raises(ValueError):
        a.save_layer("parks", {"type": "Feature"})


def test_delete_layer_removes_file(ofm_root: Path):
    a = GeoJSONFileAdapter(ofm_root / "fakeworld")
    a.save_layer("parks", _fc([_poly("p")]))
    a.delete_layer("parks")
    assert not (ofm_root / "fakeworld" / "raw" / "geojson" / "parks.geojson").exists()
