"""End-to-end API tests via FastAPI TestClient."""


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_list_worlds(client):
    r = client.get("/api/worlds")
    assert r.status_code == 200
    slugs = {w["slug"] for w in r.json()}
    assert {"fakeworld", "anotherworld"} <= slugs


def test_get_world_404_for_unknown(client):
    r = client.get("/api/worlds/nope")
    assert r.status_code == 404


def test_get_world_style(client):
    r = client.get("/api/worlds/fakeworld/style")
    assert r.status_code == 200
    assert r.json()["version"] == 8


def test_layer_roundtrip_via_api(client):
    fc = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]],
                },
                "properties": {"name": "API-Park"},
            }
        ],
    }
    r = client.put("/api/worlds/fakeworld/layers/parks", json=fc)
    assert r.status_code == 200
    assert r.json()["saved"] == 1
    r = client.get("/api/worlds/fakeworld/layers/parks")
    assert r.status_code == 200
    body = r.json()
    assert len(body["features"]) == 1
    assert body["features"][0]["properties"]["name"] == "API-Park"


def test_put_rejects_non_featurecollection(client):
    r = client.put("/api/worlds/fakeworld/layers/parks", json={"type": "Feature"})
    assert r.status_code == 400


def test_auth_required_when_env_set(monkeypatch, ofm_root):
    monkeypatch.setenv("OFM_EDITOR_USER", "u")
    monkeypatch.setenv("OFM_EDITOR_PASSWORD", "pw")
    from backend import app as app_mod
    from fastapi.testclient import TestClient

    c = TestClient(app_mod.app)
    assert c.get("/api/worlds").status_code == 401
    assert c.get("/api/worlds", auth=("u", "wrong")).status_code == 401
    assert c.get("/api/worlds", auth=("u", "pw")).status_code == 200


def test_adapter_method_failure_returns_503_with_detail(client, ofm_root, monkeypatch):
    """When the storage adapter raises mid-call, we must return a clean 503
    (with CORS headers) so the browser surfaces the error instead of treating
    it as a network-level 'Failed to fetch'. Regression for the athas/postgis
    case where psycopg.OperationalError escaped uncaught.
    """
    from backend.storage import geojson_file

    # Monkeypatch the adapter to raise on list_layers
    def _boom(self):
        raise RuntimeError("simulated backend down")

    monkeypatch.setattr(geojson_file.GeoJSONFileAdapter, "list_layers", _boom)
    r = client.get("/api/worlds/fakeworld/layers")
    assert r.status_code == 503
    body = r.json()
    assert "list_layers" in body["detail"]
    assert "simulated backend down" in body["detail"]


def test_delete_layer(client):
    fc = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [0, 0]},
                "properties": {"name": "pt"},
            }
        ],
    }
    client.put("/api/worlds/fakeworld/layers/markers", json=fc)
    r = client.delete("/api/worlds/fakeworld/layers/markers")
    assert r.status_code == 200
    r = client.get("/api/worlds/fakeworld/layers/markers")
    assert r.json() == {"type": "FeatureCollection", "features": []}
