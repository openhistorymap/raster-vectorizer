"""Tests for raster source discovery and tile serving."""
from pathlib import Path

import pytest


def _make_xyz_tile(world_dir: Path, source: str, z: int, x: int, y: int, ext="jpg"):
    p = world_dir / "raw" / "tiles" / source / str(z) / str(x) / f"{y}.{ext}"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\xff\xd8\xff\xd9")  # JPEG SOI+EOI


def _make_piggyback_tile(world_dir: Path, source: str, z: int, x: int, y: int, ext="png"):
    p = world_dir / "aerial" / source / str(z) / f"{x}_{y}.{ext}"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\x89PNG\r\n\x1a\n")


def _make_tiny_cog(path: Path) -> bool:
    """Synthesize a 256x256 RGB GeoTIFF spanning [-1,-1,1,1] in EPSG:4326.

    Returns True on success; False if rasterio isn't installed (test gets skipped).
    """
    try:
        import numpy as np
        import rasterio
        from rasterio.transform import from_bounds
    except ImportError:
        return False
    arr = np.zeros((3, 256, 256), dtype=np.uint8)
    arr[0] = 200  # red-ish
    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(
        path, "w",
        driver="GTiff",
        height=256, width=256, count=3,
        dtype="uint8",
        crs="EPSG:4326",
        transform=from_bounds(-1, -1, 1, 1, 256, 256),
    ) as dst:
        dst.write(arr)
    return True


# --- discovery -------------------------------------------------------------

def test_discover_finds_xyz_pyramid(ofm_root: Path):
    from backend import raster
    _make_xyz_tile(ofm_root / "fakeworld", "fsm", 3, 1, 2, "jpg")
    srcs = raster.discover(ofm_root / "fakeworld")
    names = {s["name"]: s for s in srcs}
    assert "fsm" in names
    assert names["fsm"]["kind"] == "pyramid"
    assert names["fsm"]["layout"] == "xyz"
    assert names["fsm"]["ext"] == "jpg"
    assert names["fsm"]["min_zoom"] == 3
    assert names["fsm"]["max_zoom"] == 3


def test_discover_finds_piggyback_pyramid(ofm_root: Path):
    from backend import raster
    _make_piggyback_tile(ofm_root / "fakeworld", "satellite", 10, 32, 64, "png")
    srcs = raster.discover(ofm_root / "fakeworld")
    by_name = {s["name"]: s for s in srcs}
    assert "satellite" in by_name
    assert by_name["satellite"]["layout"] == "piggyback"
    assert by_name["satellite"]["ext"] == "png"


def test_discover_finds_geotiff(ofm_root: Path):
    from backend import raster
    ok = _make_tiny_cog(ofm_root / "fakeworld" / "terrain.tif")
    if not ok:
        pytest.skip("rasterio not installed")
    srcs = raster.discover(ofm_root / "fakeworld")
    by_name = {s["name"]: s for s in srcs}
    assert "terrain" in by_name
    g = by_name["terrain"]
    assert g["kind"] == "geotiff"
    assert g["ext"] == "png"
    assert len(g["bounds"]) == 4
    # bounds are roughly the input bounds, in EPSG:4326
    w, s, e, n = g["bounds"]
    assert -1.5 < w < -0.5 and -1.5 < s < -0.5
    assert 0.5 < e < 1.5 and 0.5 < n < 1.5


# --- pyramid tile reads ----------------------------------------------------

def test_read_pyramid_tile_xyz(ofm_root: Path):
    from backend import raster
    _make_xyz_tile(ofm_root / "fakeworld", "fsm", 3, 1, 2, "jpg")
    body = raster.read_pyramid_tile(ofm_root / "fakeworld", "fsm", 3, 1, 2, "jpg")
    assert body is not None and body.startswith(b"\xff\xd8")


def test_read_pyramid_tile_piggyback(ofm_root: Path):
    from backend import raster
    _make_piggyback_tile(ofm_root / "fakeworld", "satellite", 10, 32, 64, "png")
    body = raster.read_pyramid_tile(
        ofm_root / "fakeworld", "satellite", 10, 32, 64, "png"
    )
    assert body is not None and body.startswith(b"\x89PNG")


def test_read_pyramid_tile_missing_returns_none(ofm_root: Path):
    from backend import raster
    assert raster.read_pyramid_tile(
        ofm_root / "fakeworld", "fsm", 99, 99, 99, "jpg"
    ) is None


# --- GeoTIFF tile render ---------------------------------------------------

def test_render_geotiff_tile(ofm_root: Path):
    from backend import raster
    p = ofm_root / "fakeworld" / "terrain.tif"
    if not _make_tiny_cog(p):
        pytest.skip("rasterio not installed")
    # At z=0, tile (0,0) covers the whole world — should overlap our [-1,1] data.
    body = raster.render_geotiff_tile(p, 0, 0, 0)
    assert body and body[:8] == b"\x89PNG\r\n\x1a\n"


# --- HTTP API --------------------------------------------------------------

def test_api_list_rasters(client, ofm_root):
    _make_xyz_tile(ofm_root / "fakeworld", "fsm", 2, 0, 0, "jpg")
    r = client.get("/api/worlds/fakeworld/rasters")
    assert r.status_code == 200
    names = {s["name"] for s in r.json()}
    assert "fsm" in names


def test_api_get_raster_tile_pyramid(client, ofm_root):
    _make_xyz_tile(ofm_root / "fakeworld", "fsm", 2, 0, 0, "jpg")
    r = client.get("/api/worlds/fakeworld/rasters/fsm/tiles/2/0/0.jpg")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/")
    assert r.headers.get("cache-control", "").startswith("public")


def test_api_get_raster_tile_unknown_source(client, ofm_root):
    r = client.get("/api/worlds/fakeworld/rasters/nope/tiles/2/0/0.jpg")
    assert r.status_code == 404


def test_api_get_raster_tile_geotiff(client, ofm_root):
    p = ofm_root / "fakeworld" / "terrain.tif"
    if not _make_tiny_cog(p):
        pytest.skip("rasterio not installed")
    r = client.get("/api/worlds/fakeworld/rasters/terrain/tiles/0/0/0.png")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "image/png"
    assert r.content[:8] == b"\x89PNG\r\n\x1a\n"


def test_api_legacy_tile_route_still_works(client, ofm_root):
    _make_xyz_tile(ofm_root / "fakeworld", "fsm", 2, 0, 0, "jpg")
    r = client.get("/api/worlds/fakeworld/tiles/2/0/0.jpg")
    assert r.status_code == 200


def test_api_get_raster_tile_piggyback(client, ofm_root):
    _make_piggyback_tile(ofm_root / "fakeworld", "aerial", 10, 32, 64, "png")
    r = client.get("/api/worlds/fakeworld/rasters/aerial/tiles/10/32/64.png")
    assert r.status_code == 200
    assert r.content[:8] == b"\x89PNG\r\n\x1a\n"


def test_api_get_raster_tile_bad_extension(client, ofm_root):
    _make_xyz_tile(ofm_root / "fakeworld", "fsm", 2, 0, 0, "jpg")
    r = client.get("/api/worlds/fakeworld/rasters/fsm/tiles/2/0/0.bmp")
    assert r.status_code == 400


def test_api_get_raster_tile_404_when_y_oob_pyramid(client, ofm_root):
    _make_xyz_tile(ofm_root / "fakeworld", "fsm", 2, 0, 0, "jpg")
    r = client.get("/api/worlds/fakeworld/rasters/fsm/tiles/2/9/9.jpg")
    assert r.status_code == 404


# --- flat pyramid layout (cyberpunk / zelda style) ------------------------
# parent IS the pyramid root: <world>/aerial/{z}/{x}/{y}.png  (no layer subdir)

def _make_flat_aerial_tile(world_dir: Path, z: int, x: int, y: int, ext="png"):
    p = world_dir / "aerial" / str(z) / str(x) / f"{y}.{ext}"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\x89PNG\r\n\x1a\n")


def test_discover_finds_flat_aerial_pyramid(ofm_root: Path):
    from backend import raster
    _make_flat_aerial_tile(ofm_root / "fakeworld", 14, 8061, 8009, "png")
    srcs = raster.discover(ofm_root / "fakeworld")
    by_name = {s["name"]: s for s in srcs}
    assert "aerial" in by_name
    assert by_name["aerial"]["kind"] == "pyramid"
    assert by_name["aerial"]["path"] == "aerial"
    assert by_name["aerial"]["ext"] == "png"
    assert by_name["aerial"]["min_zoom"] == 14
    assert by_name["aerial"]["max_zoom"] == 14


def test_api_get_flat_aerial_tile(client, ofm_root):
    _make_flat_aerial_tile(ofm_root / "fakeworld", 14, 8061, 8009, "png")
    r = client.get("/api/worlds/fakeworld/rasters/aerial/tiles/14/8061/8009.png")
    assert r.status_code == 200
    assert r.content[:8] == b"\x89PNG\r\n\x1a\n"


def test_projected_geotiff_bounds_are_reported_in_degrees(tmp_path):
    """Starbase deck plans are EPSG:3857 GeoTIFFs a few tens of metres wide at the origin; their
    bounds must come back as lng/lat near 0,0, not as metres read as degrees."""
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin
    from backend import raster

    wdir = tmp_path / "starbase"
    wdir.mkdir()
    with rasterio.open(wdir / "d1.tif", "w", driver="GTiff", width=300, height=200, count=3, dtype="uint8",
                       crs="EPSG:3857", transform=from_origin(-60.0, 47.5, 0.0084, 0.0084)) as dst:
        dst.write(np.full((3, 200, 300), 128, np.uint8))
    src = next(s for s in raster.discover(wdir) if s["name"] == "d1")
    west, south, east, north = src["bounds"]
    assert -0.001 < west < east < 0 and 0 < south < north < 0.001


def test_max_zoom_follows_native_resolution(tmp_path):
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin
    from backend import raster
    wdir = tmp_path / "w"
    wdir.mkdir()
    with rasterio.open(wdir / "deck.tif", "w", driver="GTiff", width=64, height=64, count=1, dtype="uint8",
                       crs="EPSG:3857", transform=from_origin(0, 0, 0.0084, 0.0084)) as dst:
        dst.write(np.zeros((1, 64, 64), np.uint8))
    assert next(s for s in raster.discover(wdir) if s["name"] == "deck")["max_zoom"] == 25   # 0.0084 m/px
