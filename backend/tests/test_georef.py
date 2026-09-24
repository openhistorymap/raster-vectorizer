"""Georeferencing: fitting, accuracy, checks, saving, warping — against a known ground truth.

The synthetic scan is 400 × 300 px at exactly 10 Web-Mercator metres per pixel near Bologna, with a
red square at pixels (100..140, 50..90), so every number below has a known right answer.
"""
from __future__ import annotations

import io
import json
import math

import numpy as np
import pytest

from backend import georef

W, H, RES = 400, 300, 10.0
X0, Y0 = georef.to_merc(np.array([[11.34, 44.49]]))[0]


def truth(px: float, py: float) -> list[float]:
    """Exact lon/lat of a pixel."""
    return georef.to_lonlat(np.array([[X0 + RES * px, Y0 - RES * py]]))[0].tolist()


def gcps(points=((0, 0), (400, 0), (400, 300), (0, 300), (200, 150), (100, 220), (330, 60))):
    return [{"pixel": [x, y], "lonlat": truth(x, y)} for x, y in points]


def scan_png() -> bytes:
    from PIL import Image
    img = np.full((H, W, 3), 255, np.uint8)
    img[50:90, 100:140] = (255, 0, 0)
    buf = io.BytesIO()
    Image.fromarray(img).save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture
def scan(client):
    r = client.post("/api/worlds/fakeworld/scans", files={"file": ("carta.png", scan_png(), "image/png")})
    assert r.status_code == 200, r.text
    return r.json()


# --------------------------------------------------------------------------- pure fitting

def test_exact_affine_fits_with_zero_error():
    res = georef.fit(gcps(), "polynomial1")
    assert res["accuracy"]["rmse"] < 1e-3
    assert res["accuracy"]["leaveOneOutRmse"] < 1e-3
    lonlat = georef.to_lonlat(res["forward"].apply(np.array([[123.0, 45.0]])))[0]
    assert np.allclose(lonlat, truth(123, 45), atol=1e-9)


def test_a_misplaced_point_shows_up_and_leave_one_out_is_honest():
    pts = gcps()
    lon, lat = pts[4]["lonlat"]
    pts[4]["lonlat"] = [lon + 50 / (111320 * math.cos(math.radians(lat))), lat]   # 50 m east
    res = georef.fit(pts, "polynomial1")
    errs = [p["residual_m"] for p in res["points"]]
    assert int(np.argmax(errs)) == 4
    assert res["accuracy"]["leaveOneOutRmse"] > res["accuracy"]["rmse"] > 5


def test_tps_publishes_leave_one_out_not_its_zero_residuals():
    pts = gcps()
    lon, lat = pts[4]["lonlat"]
    pts[4]["lonlat"] = [lon + 0.0005, lat]
    res = georef.fit(pts, "tps")
    assert max(p["residual_m"] for p in res["points"]) < 1e-3          # interpolates exactly
    assert res["accuracy"]["method"] == "leave-one-out"
    assert res["accuracy"]["rmse"] == res["accuracy"]["leaveOneOutRmse"] > 1


def test_too_few_and_collinear_points_are_refused():
    with pytest.raises(georef.GeorefError, match="at least 6"):
        georef.fit(gcps()[:5], "polynomial2")
    with pytest.raises(georef.GeorefError, match="collinear"):
        georef.fit(gcps(((0, 0), (100, 100), (200, 200), (300, 300))), "polynomial1")


def test_checks_catch_a_wrong_scale():
    res = georef.fit(gcps(), "polynomial1")
    ground_width = W * RES * math.cos(math.radians(44.49))                # ~2853 m
    ok = georef.checks(res, W, H, {}, expected_width_m=ground_width)
    assert not [c for c in ok if c["level"] == "error"]
    bad = georef.checks(res, W, H, {}, expected_width_m=ground_width * 10)  # the Defiant mistake
    assert [c for c in bad if c["level"] == "error"]


def test_checks_warn_about_a_centimetre_wide_ship():
    tiny = [{"pixel": [x, y], "lonlat": georef.to_lonlat(np.array([[x * 4e-6, -y * 4e-6]]))[0].tolist()}
            for x, y in ((0, 0), (3000, 0), (3000, 5000), (0, 5000))]
    res = georef.fit(tiny, "polynomial1")
    msgs = [c["message"] for c in georef.checks(res, 3000, 5000, {"base": {"lat": 0.000355, "lng": -0.000429}})]
    assert any("cm across" in m for m in msgs)
    assert any("default view" in m for m in msgs)


# --------------------------------------------------------------------------- API

def test_upload_lists_and_serves_a_preview(client, scan):
    assert scan["width"] == W and scan["height"] == H
    assert [s["name"] for s in client.get("/api/worlds/fakeworld/scans").json()] == ["carta"]
    r = client.get("/api/worlds/fakeworld/scans/carta/image?max=200")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert float(r.headers["x-scale"]) == pytest.approx(2.0)
    assert client.post("/api/worlds/fakeworld/scans",
                       files={"file": ("carta.png", scan_png(), "image/png")}).status_code == 409


def test_fit_is_a_dry_run_and_put_saves_the_annotation(client, scan):
    body = {"gcps": gcps(), "transformation": "polynomial1"}
    r = client.post("/api/worlds/fakeworld/scans/carta/georef/fit", json=body)
    assert r.status_code == 200
    assert r.json()["accuracy"]["rmse"] < 1e-3
    assert client.get("/api/worlds/fakeworld/scans/carta/georef").status_code == 404

    r = client.put("/api/worlds/fakeworld/scans/carta/georef", json=body)
    assert r.status_code == 200, r.text
    ann = client.get("/api/worlds/fakeworld/scans/carta/georef").json()
    assert ann["type"] == "Annotation" and ann["motivation"] == "georeferencing"
    assert ann["target"]["source"] == {"id": "carta", "type": "Image", "width": W, "height": H}
    assert ann["body"]["transformation"] == {"type": "polynomial", "options": {"order": 1}}
    assert ann["body"]["features"][0]["properties"]["resourceCoords"] == [0, 0]
    assert client.get("/api/worlds/fakeworld/scans").json()[0]["georeference"]["accuracy"]["controlPoints"] == 7


def test_put_refuses_a_failing_check(client, scan):
    r = client.put("/api/worlds/fakeworld/scans/carta/georef",
                   json={"gcps": gcps(), "transformation": "polynomial1", "expectedWidthM": 40000})
    assert r.status_code == 422
    assert "expected" in json.dumps(r.json())


def test_warp_puts_pixels_where_they_belong(client, scan, ofm_root):
    import rasterio
    client.put("/api/worlds/fakeworld/scans/carta/georef", json={"gcps": gcps(), "transformation": "polynomial1"})
    r = client.post("/api/worlds/fakeworld/scans/carta/warp")
    assert r.status_code == 200, r.text
    assert r.json()["resolution_m"] == pytest.approx(RES, rel=1e-3)

    with rasterio.open(ofm_root / "fakeworld" / "cogs" / "carta.tif") as ds:
        assert ds.crs.to_epsg() == 3857
        left, bottom, right, top = ds.bounds
        assert (left, top) == (pytest.approx(X0, abs=RES), pytest.approx(Y0, abs=RES))
        assert (right - left, top - bottom) == (pytest.approx(W * RES, abs=2 * RES), pytest.approx(H * RES, abs=2 * RES))
        # The centre of the red square, pixel (120, 70), must be red in the warped image.
        row, col = ds.index(X0 + RES * 120, Y0 - RES * 70)
        pixel = ds.read(window=((row, row + 1), (col, col + 1)))[:, 0, 0]
        assert tuple(pixel[:3]) == (255, 0, 0) and pixel[3] == 255
    names = [s["name"] for s in client.get("/api/worlds/fakeworld/rasters").json()]
    assert "carta" in names                                  # discovered as a basemap


def test_scan_registered_to_a_source_is_exported_with_its_georeference(client):
    src = "https://example.org/carta-1702"
    client.put("/api/worlds/fakeworld/sources", json={src: {"title": "Carta di Bologna, 1702", "type": "map"}})
    r = client.post("/api/worlds/fakeworld/scans", data={"source": src},
                    files={"file": ("sheet3.png", scan_png(), "image/png")})
    assert r.status_code == 200, r.text
    file_entry = client.get("/api/worlds/fakeworld/sources").json()[src]["files"][0]
    assert file_entry["id"] == "sheet3" and file_entry["checksum"].startswith("sha256:")

    client.put("/api/worlds/fakeworld/scans/sheet3/georef", json={"gcps": gcps(), "transformation": "polynomial1"})
    feat = {"type": "Feature", "geometry": {"type": "Point", "coordinates": truth(120, 70)},
            "properties": {"name": "red square"},
            "citations": [{"source": src, "file": "sheet3", "method": "traced",
                           "selector": [{"type": "FragmentSelector", "value": "xywh=100,50,40,40"}]}]}
    client.put("/api/worlds/fakeworld/layers/places", json={"type": "FeatureCollection", "features": [feat]})
    doc = client.get("/api/worlds/fakeworld/layers/places/cited-geojson").json()
    assert [g["target"]["source"]["id"] for g in doc["georeferences"]] == ["sheet3"]
    assert doc["georeferences"][0]["accuracy"]["unit"] == "m"
