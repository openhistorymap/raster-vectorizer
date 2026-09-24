"""Shared fixtures: a synthetic OFM root with two fake worlds."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

# Force pyproj to load its PROJ database once at session start, before any
# monkeypatch fiddles with os.environ. Without this, EPSG:4326 lookups in
# rasterio/rio_tiler fail later with "no database context specified" if
# another test that uses monkeypatch.delenv runs first.
try:
    import pyproj as _pyproj
    _pyproj.CRS.from_epsg(4326)
except ImportError:
    pass


def _seed_world(root: Path, slug: str, mode: str, extras: dict | None = None) -> Path:
    wdir = root / slug
    wdir.mkdir(parents=True, exist_ok=True)
    timeline = {
        "name": slug.capitalize(),
        "url": f"/{slug}",
        "date": 1000,
        "mode": mode,
        "base": {"zoom": 4, "lat": 0, "lng": 0},
        "tags": ["test"],
        "connection": {"db": slug},
    }
    if extras:
        timeline.update(extras)
    (wdir / "timeline.json").write_text(json.dumps(timeline), encoding="utf-8")
    (wdir / "gaia.json").write_text(
        json.dumps({"version": "1.0", "sources": ["parks", "roads"]}),
        encoding="utf-8",
    )
    (wdir / "map.json").write_text(
        json.dumps({"version": 8, "name": slug, "sources": {}, "layers": []}),
        encoding="utf-8",
    )
    return wdir


@pytest.fixture
def ofm_root(tmp_path: Path, monkeypatch) -> Path:
    """A synthetic /ofm root with two minimal worlds for the tests to chew on."""
    root = tmp_path / "ofm"
    root.mkdir()
    _seed_world(root, "fakeworld", "geojson")
    _seed_world(root, "anotherworld", "spatialite")
    # an empty-timeline world to exercise the skip path
    bad = root / "broken"
    bad.mkdir()
    (bad / "timeline.json").write_text("", encoding="utf-8")
    monkeypatch.setenv("OFM_ROOT", str(root))
    # worlds.OFM_ROOT is a lazy lookup so no reload needed.
    yield root


@pytest.fixture
def client(ofm_root, monkeypatch):
    """A FastAPI test client with auth disabled (no creds in env)."""
    monkeypatch.delenv("OFM_EDITOR_USER", raising=False)
    monkeypatch.delenv("OFM_EDITOR_PASSWORD", raising=False)
    from backend import app as app_mod
    from fastapi.testclient import TestClient

    return TestClient(app_mod.app)
