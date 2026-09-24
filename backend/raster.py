"""Raster source discovery + tile serving for OFM worlds.

Two source kinds supported:

* **pyramid** — pre-baked XYZ tile pyramid on disk. Two layouts recognised:
    1. `{layer_dir}/{z}/{x}/{y}.{ext}`  (standard XYZ; planetos, sta, etc.)
    2. `{layer_dir}/{z}/{x}_{y}.{ext}`  (piggyback; cyberpunk, zelda-botw)

* **geotiff** — a georeferenced single-file `.tif` / `.tiff` (ideally a COG).
  Tiles are rendered on demand via rio-tiler and returned as PNG.

Discovery walks the world dir and reports all sources with their zoom range
and (for GeoTIFFs) geographic bounds, so the frontend can position the map
correctly and populate a basemap-picker dropdown.
"""
from __future__ import annotations

import functools
from pathlib import Path
from typing import Any

PYRAMID_PARENTS = ("raw/tiles", "aerial", "tiles")
TILE_EXTS = (".jpg", ".jpeg", ".png", ".webp")
GEOTIFF_EXTS = (".tif", ".tiff")


# --- discovery --------------------------------------------------------------

def discover(world_dir: Path) -> list[dict[str, Any]]:
    """Return all raster sources for a world, both pyramids and GeoTIFFs."""
    sources: list[dict[str, Any]] = []

    # 1. tile pyramids — two layouts in the wild:
    #    A) parent/<layer>/{z}/{x}/{y}.ext  (planetos: raw/tiles/fsm/...)
    #    B) parent/{z}/{x}/{y}.ext          (cyberpunk: aerial/14/8061/8009.png)
    for parent_name in PYRAMID_PARENTS:
        parent = world_dir / parent_name
        if not parent.is_dir():
            continue
        # Try (B) first: probe parent itself. If it has numeric z-dir children
        # with real tiles, parent IS the pyramid root.
        info = _probe_pyramid(parent)
        if info:
            sources.append(
                {
                    "name": parent.name,
                    "kind": "pyramid",
                    "path": str(parent.relative_to(world_dir)),
                    **info,
                }
            )
            continue  # don't also re-scan its children
        # Otherwise (A): each child of parent might be its own pyramid layer.
        for layer_dir in sorted(parent.iterdir()):
            if not layer_dir.is_dir():
                continue
            info = _probe_pyramid(layer_dir)
            if info:
                sources.append(
                    {
                        "name": layer_dir.name,
                        "kind": "pyramid",
                        "path": str(layer_dir.relative_to(world_dir)),
                        **info,
                    }
                )

    # 2. GeoTIFFs — top level + raw/ + cogs/
    for sub in ("", "raw", "cogs"):
        scan_dir = world_dir / sub if sub else world_dir
        if not scan_dir.is_dir():
            continue
        for ext in GEOTIFF_EXTS:
            for tif in sorted(scan_dir.glob(f"*{ext}")):
                info = _probe_geotiff(tif)
                if info:
                    sources.append(
                        {
                            "name": tif.stem,
                            "kind": "geotiff",
                            "path": str(tif.relative_to(world_dir)),
                            **info,
                        }
                    )

    return sources


def _probe_pyramid(layer_dir: Path) -> dict[str, Any] | None:
    zooms: list[int] = []
    for z_dir in layer_dir.iterdir():
        if z_dir.is_dir() and z_dir.name.isdigit():
            zooms.append(int(z_dir.name))
    if not zooms:
        return None
    zooms.sort()
    # Detect layout + extension by sniffing the first zoom level.
    for z in zooms:
        z_dir = layer_dir / str(z)
        for child in z_dir.iterdir():
            if child.is_dir():
                # Standard XYZ — descend to find a tile and capture extension
                for tile in child.iterdir():
                    if tile.is_file() and tile.suffix.lower() in TILE_EXTS:
                        return {
                            "min_zoom": zooms[0],
                            "max_zoom": zooms[-1],
                            "ext": tile.suffix.lower().lstrip("."),
                            "layout": "xyz",
                        }
            elif child.is_file() and child.suffix.lower() in TILE_EXTS and "_" in child.stem:
                # Piggyback layout — {x}_{y}.ext at the zoom-level root
                return {
                    "min_zoom": zooms[0],
                    "max_zoom": zooms[-1],
                    "ext": child.suffix.lower().lstrip("."),
                    "layout": "piggyback",
                }
    return None


def _native_max_zoom(ds, fallback: int) -> int:
    """The web-map zoom at which one tile pixel matches one raster pixel.

    Deck plans are a few centimetres per pixel (zoom ~24); capping them at a default 22 would
    stretch every tile 4× in the editor.
    """
    import math
    try:
        res = abs(ds.transform.a)
        if ds.crs and ds.crs.is_geographic:
            res *= 111_319.49  # degrees -> metres at the equator
        return max(0, min(26, math.ceil(math.log2(156_543.034 / res))))
    except Exception:
        return fallback


def _probe_geotiff(path: Path) -> dict[str, Any] | None:
    try:
        from rio_tiler.io import Reader
    except ImportError:
        return None
    try:
        with Reader(str(path)) as src:
            info = src.info()
            # The frontend needs lng/lat bounds, but src.bounds is in the dataset's CRS — metres
            # for EPSG:3857 rasters such as the starbase deck plans. Reproject explicitly (the old
            # rio-tiler `geographic_bounds` property is gone; its fallback reported metres as
            # degrees and put those worlds' maps thousands of kilometres away).
            from rasterio.warp import transform_bounds
            gbounds = list(transform_bounds(src.crs, "EPSG:4326", *src.bounds, densify_pts=21))
            return {
                "min_zoom": int(getattr(info, "minzoom", 0)),
                "max_zoom": _native_max_zoom(src.dataset, int(getattr(info, "maxzoom", 22))),
                "bounds": gbounds,           # [west, south, east, north] in EPSG:4326
                "crs": str(getattr(src, "crs", "EPSG:3857")),
                "ext": "png",
            }
    except Exception:
        return None


# --- tile serving -----------------------------------------------------------

def find_source(world_dir: Path, name: str) -> dict[str, Any] | None:
    for s in discover(world_dir):
        if s["name"] == name:
            return s
    return None


def read_pyramid_tile(
    world_dir: Path,
    source: str | None = None,
    z: int = 0,
    x: int = 0,
    y: int = 0,
    ext: str = "png",
    *,
    path: str | None = None,
) -> bytes | None:
    """Read a tile. Pass either `path` (relative to world_dir, from discover)
    or `source` (name, for back-compat — searches the known parent dirs)."""
    candidates: list[Path] = []
    if path:
        candidates.append(world_dir / path)
    elif source:
        for parent_name in PYRAMID_PARENTS:
            # Try parent/source (layout A) then parent itself if name matches (layout B)
            candidates.append(world_dir / parent_name / source)
            if (world_dir / parent_name).name == source:
                candidates.append(world_dir / parent_name)
    for layer_dir in candidates:
        if not layer_dir.is_dir():
            continue
        # standard XYZ
        p = layer_dir / str(z) / str(x) / f"{y}.{ext}"
        if p.is_file():
            return p.read_bytes()
        # piggyback {x}_{y}.ext at zoom-level root
        p = layer_dir / str(z) / f"{x}_{y}.{ext}"
        if p.is_file():
            return p.read_bytes()
    return None


def render_geotiff_tile(tif_path: Path, z: int, x: int, y: int) -> bytes:
    """Render a single XYZ tile from a GeoTIFF as PNG bytes.

    Out-of-bounds requests raise — caller maps to 404.
    """
    from rio_tiler.io import Reader

    with Reader(str(tif_path)) as src:
        img = src.tile(x, y, z)
    return img.render(img_format="PNG")


# Tiny LRU on the path-level probe to avoid re-opening the same .tif on every
# tile call. discover() itself is also lightweight enough to call per request
# but we cache it briefly to spare disk scans.

@functools.lru_cache(maxsize=128)
def discover_cached(world_dir_str: str) -> tuple:
    return tuple((s["name"], s["kind"], s.get("path"), s.get("ext", "png"))
                 for s in discover(Path(world_dir_str)))
