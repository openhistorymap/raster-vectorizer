"""Tests for per-layer JSON Schema storage and API."""
from pathlib import Path

import pytest


def _ok_schema():
    return {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "population": {"type": "integer", "minimum": 0},
            "kingdom": {"type": "string", "enum": ["North", "South", "East", "West"]},
        },
        "required": ["name"],
    }


# --- module-level ---------------------------------------------------------

def test_get_returns_empty_stub_when_no_schema(ofm_root: Path):
    from backend import layer_schemas

    s = layer_schemas.get(ofm_root / "fakeworld", "parks")
    assert s == {"type": "object", "properties": {}}


def test_put_then_get_roundtrip(ofm_root: Path):
    from backend import layer_schemas

    res = layer_schemas.put(ofm_root / "fakeworld", "parks", _ok_schema())
    assert res["bytes"] > 0
    assert Path(res["path"]).exists()
    got = layer_schemas.get(ofm_root / "fakeworld", "parks")
    assert got["properties"]["kingdom"]["enum"] == ["North", "South", "East", "West"]


def test_put_creates_bak_on_overwrite(ofm_root: Path):
    from backend import layer_schemas

    layer_schemas.put(ofm_root / "fakeworld", "parks", _ok_schema())
    res = layer_schemas.put(
        ofm_root / "fakeworld",
        "parks",
        _ok_schema() | {"description": "v2"},
    )
    assert res["backup"] is not None
    assert Path(res["backup"]).exists()


def test_put_rejects_non_schema(ofm_root: Path):
    from backend import layer_schemas

    with pytest.raises(ValueError) as excinfo:
        layer_schemas.put(
            ofm_root / "fakeworld",
            "parks",
            {"properties": {"name": {"type": "not-a-real-type"}}},
        )
    assert "validation_errors" in excinfo.value.args[0]


def test_put_rejects_array(ofm_root: Path):
    from backend import layer_schemas

    with pytest.raises(ValueError):
        layer_schemas.put(ofm_root / "fakeworld", "parks", ["not", "a", "schema"])


def test_unsafe_layer_name_rejected(ofm_root: Path):
    from backend import layer_schemas

    with pytest.raises(ValueError):
        layer_schemas.put(ofm_root / "fakeworld", "../etc/passwd", _ok_schema())


def test_list_layers_after_puts(ofm_root: Path):
    from backend import layer_schemas

    layer_schemas.put(ofm_root / "fakeworld", "parks", _ok_schema())
    layer_schemas.put(ofm_root / "fakeworld", "roads", _ok_schema())
    assert set(layer_schemas.list_layers(ofm_root / "fakeworld")) >= {"parks", "roads"}


# --- HTTP API -------------------------------------------------------------

def test_api_get_layer_schema_empty(client):
    r = client.get("/api/worlds/fakeworld/layers/parks/schema")
    assert r.status_code == 200
    assert r.json() == {"type": "object", "properties": {}}


def test_api_put_then_get_roundtrip(client):
    r = client.put("/api/worlds/fakeworld/layers/parks/schema", json=_ok_schema())
    assert r.status_code == 200, r.text
    r = client.get("/api/worlds/fakeworld/layers/parks/schema")
    assert r.status_code == 200
    assert r.json()["properties"]["kingdom"]["enum"][0] == "North"


def test_api_put_invalid_schema_returns_422(client):
    r = client.put(
        "/api/worlds/fakeworld/layers/parks/schema",
        json={"type": "object", "properties": {"x": {"type": "weird"}}},
    )
    assert r.status_code == 422
    assert "validation_errors" in r.json()["detail"]


def test_api_put_non_object_returns_422(client):
    # Arrays/strings/etc. aren't valid layer schemas.
    r = client.put("/api/worlds/fakeworld/layers/parks/schema", json=[1, 2, 3])
    # FastAPI's body model expects dict[str, Any], so an array → 422 from
    # request validation before our handler sees it.
    assert r.status_code in (400, 422)
