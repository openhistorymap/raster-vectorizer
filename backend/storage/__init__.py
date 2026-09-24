"""Storage adapter factory — pick the right backend from timeline.json#mode."""
from __future__ import annotations

import json
from pathlib import Path

from .base import StorageAdapter
from .geojson_file import GeoJSONFileAdapter
from .postgis import PostGISAdapter
from .spatialite import SpatiaLiteAdapter


def for_world(world_dir: Path) -> StorageAdapter:
    tl_path = world_dir / "timeline.json"
    if tl_path.is_file() and tl_path.stat().st_size > 0:
        tl = json.loads(tl_path.read_text(encoding="utf-8"))
    else:
        tl = {}
    mode = (tl.get("mode") or "geojson").lower()
    if mode == "postgis":
        return PostGISAdapter(world_dir, tl.get("connection", {}))
    if mode == "spatialite":
        return SpatiaLiteAdapter(world_dir)
    return GeoJSONFileAdapter(world_dir)


__all__ = [
    "StorageAdapter",
    "GeoJSONFileAdapter",
    "SpatiaLiteAdapter",
    "PostGISAdapter",
    "for_world",
]
