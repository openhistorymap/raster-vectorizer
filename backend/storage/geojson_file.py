"""GeoJSON-file storage: one .geojson per layer under <world>/raw/geojson/."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

SAFE_NAME = re.compile(r"^[a-zA-Z0-9_\-]+$")


class GeoJSONFileAdapter:
    mode = "geojson"

    def __init__(self, world_dir: Path):
        self.dir = world_dir / "raw" / "geojson"
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, layer: str) -> Path:
        if not SAFE_NAME.match(layer):
            raise ValueError(f"unsafe layer name: {layer!r}")
        return self.dir / f"{layer}.geojson"

    def list_layers(self) -> list[dict[str, Any]]:
        rows = []
        for p in sorted(self.dir.glob("*.geojson")):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                feats = data.get("features", [])
                geom_types = sorted({(f.get("geometry") or {}).get("type") for f in feats if f.get("geometry")})
                rows.append(
                    {
                        "name": p.stem,
                        "count": len(feats),
                        "geometry_type": ",".join(t for t in geom_types if t) or "Mixed",
                    }
                )
            except Exception as e:
                rows.append({"name": p.stem, "count": 0, "geometry_type": "ERROR", "error": str(e)})
        return rows

    def load_layer(self, layer: str) -> dict[str, Any]:
        p = self._path(layer)
        if not p.exists():
            return {"type": "FeatureCollection", "features": []}
        return json.loads(p.read_text(encoding="utf-8"))

    def save_layer(self, layer: str, feature_collection: dict[str, Any]) -> int:
        if feature_collection.get("type") != "FeatureCollection":
            raise ValueError("expected a FeatureCollection")
        feats = feature_collection.get("features", [])
        p = self._path(layer)
        p.write_text(
            json.dumps({"type": "FeatureCollection", "features": feats}, indent=1),
            encoding="utf-8",
        )
        return len(feats)

    def delete_layer(self, layer: str) -> None:
        p = self._path(layer)
        if p.exists():
            p.unlink()
