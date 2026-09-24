"""Tests for the manifest editor: read/validate/write of the four world files."""
import json
from pathlib import Path

import pytest


def _valid_timeline():
    return {
        "name": "Fakeworld",
        "url": "/fakeworld",
        "size": 3,
        "date": 1234,
        "mode": "geojson",
        "base": {"zoom": 4, "lat": 0, "lng": 0},
        "tags": ["test"],
        "connection": {"db": "fakeworld"},
    }


def _valid_gaia():
    return {
        "version": "1.0",
        "system": ["You are an observer in Fakeworld."],
        "sources": ["roads", "rivers"],
        "weather": [],
        "terrain": ["forest"],
        "agents": ["actors"],
    }


def _valid_render():
    return [
        {"asset": "Grass.jpg", "type": "texture", "weight": -100},
        {"asset": "Lantern.png", "type": "point", "weight": 100, "condition": ["lights"]},
    ]


def _valid_map():
    return {
        "version": 8,
        "name": "Fakeworld",
        "sources": {},
        "layers": [{"id": "bg", "type": "background"}],
    }


def _valid_orbitals():
    return [
        {
            "name": "M1",
            "class": "planet",
            "mass": 5.972e24,
            "radius": 6371,
            "centralMass": 2e30,
            "semimajor": 75000000,
            "eccentricity": 0,
            "inclination": 0.0,
            "maps": [{"slot": "map", "url": "Earth.jpg"}],
            "ephemeris": {"revolutionOrder": "y"},
        }
    ]


# --- direct module tests ----------------------------------------------------

def test_schemas_exist_for_all_kinds():
    from backend import manifest

    for kind in manifest.list_kinds():
        s = manifest.get_schema(kind)
        assert "$schema" in s
        assert s.get("title", "").startswith("OFM")


def test_validate_accepts_valid_payloads():
    from backend import manifest

    assert manifest.validate("timeline", _valid_timeline()) == []
    assert manifest.validate("gaia", _valid_gaia()) == []
    assert manifest.validate("render", _valid_render()) == []
    assert manifest.validate("map", _valid_map()) == []
    assert manifest.validate("orbitals", _valid_orbitals()) == []


def test_validate_orbitals_rejects_bad_slot():
    from backend import manifest

    bad = _valid_orbitals()
    bad[0]["maps"] = [{"slot": "diffuse", "url": "x.jpg"}]  # not in slot enum
    errs = manifest.validate("orbitals", bad)
    assert any("slot" in e for e in errs)


def test_validate_orbitals_rejects_eccentricity_negative():
    from backend import manifest

    bad = _valid_orbitals()
    bad[0]["eccentricity"] = -0.1
    errs = manifest.validate("orbitals", bad)
    assert any("eccentricity" in e for e in errs)


def test_api_get_orbitals_schema(client):
    r = client.get("/api/schemas/orbitals")
    assert r.status_code == 200
    j = r.json()
    assert j["type"] == "array"
    assert "name" in j["items"]["properties"]


def test_validate_rejects_missing_required():
    from backend import manifest

    errs = manifest.validate("timeline", {"name": "x"})
    assert any("required" in e or "url" in e or "base" in e for e in errs)


def test_validate_rejects_unknown_mode():
    from backend import manifest

    bad = _valid_timeline() | {"mode": "carrierpigeon"}
    errs = manifest.validate("timeline", bad)
    assert any("mode" in e for e in errs)


def test_save_writes_and_backs_up(ofm_root: Path):
    from backend import manifest

    wdir = ofm_root / "fakeworld"
    # First write — no backup expected (the seeded timeline.json already exists)
    result = manifest.save(wdir, "timeline", _valid_timeline())
    assert result["backup"] is not None  # the seed gets backed up
    assert Path(result["path"]).exists()
    # Round-trip
    loaded = manifest.load(wdir, "timeline")
    assert loaded["mode"] == "geojson"
    # Second write — backup is overwritten with the prior version
    result2 = manifest.save(wdir, "timeline", _valid_timeline() | {"date": 9999})
    assert Path(result2["backup"]).exists()
    bak_content = json.loads(Path(result2["backup"]).read_text())
    assert bak_content["date"] == 1234


def test_save_refuses_invalid_payload(ofm_root: Path):
    from backend import manifest

    wdir = ofm_root / "fakeworld"
    with pytest.raises(ValueError) as excinfo:
        manifest.save(wdir, "timeline", {"name": "x"})
    err = excinfo.value.args[0]
    assert "validation_errors" in err
    assert any("required" in e or "url" in e for e in err["validation_errors"])


# --- HTTP API tests ---------------------------------------------------------

def test_api_list_schemas(client):
    r = client.get("/api/schemas")
    assert r.status_code == 200
    assert set(r.json()["kinds"]) == {"timeline", "gaia", "map", "render", "orbitals"}


def test_api_get_each_schema(client):
    for kind in ("timeline", "gaia", "map", "render", "orbitals"):
        r = client.get(f"/api/schemas/{kind}")
        assert r.status_code == 200
        assert r.json()["title"].startswith("OFM")


def test_api_get_schema_404(client):
    assert client.get("/api/schemas/nope").status_code == 404


def test_api_get_manifest_returns_seeded_content(client):
    r = client.get("/api/worlds/fakeworld/manifest/timeline")
    assert r.status_code == 200
    body = r.json()
    assert body["kind"] == "timeline"
    assert body["content"]["name"] == "Fakeworld"


def test_api_put_manifest_roundtrip(client):
    payload = {"content": _valid_timeline() | {"date": 5000}}
    r = client.put("/api/worlds/fakeworld/manifest/timeline", json=payload)
    assert r.status_code == 200, r.text
    assert r.json()["bytes"] > 0
    # confirm
    r = client.get("/api/worlds/fakeworld/manifest/timeline")
    assert r.json()["content"]["date"] == 5000


def test_api_put_invalid_returns_422_with_errors(client):
    r = client.put(
        "/api/worlds/fakeworld/manifest/timeline",
        json={"content": {"name": "broken"}},
    )
    assert r.status_code == 422
    assert "validation_errors" in r.json()["detail"]


def test_api_put_unknown_kind_404(client):
    r = client.put(
        "/api/worlds/fakeworld/manifest/nope",
        json={"content": {}},
    )
    assert r.status_code == 404


def test_api_put_missing_content_400(client):
    r = client.put(
        "/api/worlds/fakeworld/manifest/timeline",
        json={"foo": "bar"},
    )
    assert r.status_code == 400
