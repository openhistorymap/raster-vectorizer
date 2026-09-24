"""World discovery — walk /srv/ofm/*/timeline.json."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

def _ofm_root() -> Path:
    """Resolve the OFM root each time — env may change between tests."""
    return Path(os.environ.get("OFM_ROOT", "/ofm"))


def __getattr__(name: str):
    # Preserve `worlds.OFM_ROOT` access for callers that import it directly.
    if name == "OFM_ROOT":
        return _ofm_root()
    raise AttributeError(name)


def _safe_read(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def list_worlds() -> list[dict[str, Any]]:
    """Return a summary record per world (those with a timeline.json)."""
    out: list[dict[str, Any]] = []
    root = _ofm_root()
    if not root.is_dir():
        return out
    for child in sorted(root.iterdir()):
        tl_path = child / "timeline.json"
        if not tl_path.is_file():
            continue
        if tl_path.stat().st_size == 0:
            continue
        tl = _safe_read(tl_path)
        if not tl:
            continue
        out.append(
            {
                "slug": child.name,
                "name": tl.get("name", child.name),
                "mode": tl.get("mode", "geojson"),
                "url": tl.get("url", f"/{child.name}"),
                "date": tl.get("date"),
                "base": tl.get("base"),
                "tags": tl.get("tags", []),
                "has_tiles": (child / "raw" / "tiles").is_dir(),
                "has_geotiff": any(child.glob("**/*.tif")) or any(child.glob("**/*.tiff")),
            }
        )
    return out


def get_world(slug: str) -> dict[str, Any]:
    wdir = _ofm_root() / slug
    tl = _safe_read(wdir / "timeline.json") or {}
    gaia = _safe_read(wdir / "gaia.json") or {}
    mapjson = _safe_read(wdir / "map.json") or {}
    return {
        "slug": slug,
        "dir": str(wdir),
        "timeline": tl,
        "gaia": gaia,
        "map": mapjson,
        "expected_layers": gaia.get("sources", []) + gaia.get("indirect", []),
    }


def world_dir(slug: str) -> Path:
    wdir = _ofm_root() / slug
    if not wdir.is_dir() or not (wdir / "timeline.json").is_file():
        raise FileNotFoundError(f"no such world: {slug}")
    return wdir
