"""GeoJSON-file storage: one .geojson per layer under <world>/raw/geojson/.

Features keep stable ids across saves, collection-level foreign members (e.g. Cited GeoJSON's
`sources`) are preserved, writes are atomic, and each change is appended to
<world>/raw/history/<layer>.jsonl.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .diff import ROW_PREFIX, StoredRow, history_record, new_fid, plan_save

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

    def _history_path(self, layer: str) -> Path:
        return self.dir.parent / "history" / f"{layer}.jsonl"

    def load_layer(self, layer: str) -> dict[str, Any]:
        p = self._path(layer)
        if not p.exists():
            return {"type": "FeatureCollection", "features": []}
        fc = json.loads(p.read_text(encoding="utf-8"))
        # Features written before stable ids get a positional id until their first save.
        for i, f in enumerate(fc.get("features", [])):
            if f.get("id") is None:
                f["id"] = f"{ROW_PREFIX}{i}"
        return fc

    def save_layer(self, layer: str, feature_collection: dict[str, Any],
                   editor: str | None = None) -> dict[str, Any]:
        if feature_collection.get("type") != "FeatureCollection":
            raise ValueError("expected a FeatureCollection")
        p = self._path(layer)
        previous = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {"features": []}
        doc = {k: v for k, v in previous.items() if k != "features"}  # keep foreign members
        doc["type"] = "FeatureCollection"

        stored, alias = [], {}
        for i, f in enumerate(previous.get("features", [])):
            key = str(f["id"]) if f.get("id") is not None else new_fid()
            alias[f"{ROW_PREFIX}{i}"] = key
            stored.append(StoredRow(key=key, columns={}, extras=dict(f.get("properties") or {}),
                                    geometry=f.get("geometry")))
        incoming = [{**f, "id": alias.get(str(f.get("id")), f.get("id"))}
                    for f in feature_collection.get("features", [])]
        plan = plan_save(stored, incoming, [], has_extras_column=True)

        # Rebuild in the incoming order, with every feature carrying its stable id.
        new_ids = iter(ch.fid for ch in plan.inserts)
        known = {r.key for r in stored}
        out = []
        for f in incoming:
            fid = str(f["id"]) if f.get("id") is not None and str(f["id"]) in known else next(new_ids)
            out.append({"type": "Feature", "id": fid, "geometry": f.get("geometry"),
                        "properties": {k: v for k, v in (f.get("properties") or {}).items() if k != "fid"}})
        doc["features"] = out
        tmp = p.with_suffix(".geojson.tmp")
        tmp.write_text(json.dumps(doc, indent=1, ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)  # atomic: readers never see a half-written layer

        records = [history_record(layer, ch, op, editor)
                   for op, changes in (("insert", plan.inserts), ("update", plan.updates),
                                       ("delete", plan.deletes)) for ch in changes]
        if records:
            hp = self._history_path(layer)
            hp.parent.mkdir(parents=True, exist_ok=True)
            now = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
            with hp.open("a", encoding="utf-8") as fh:
                for rec in records:
                    fh.write(json.dumps({**rec, "at": now}, ensure_ascii=False, default=str) + "\n")
        return plan.summary(layer)

    def delete_layer(self, layer: str) -> None:
        p = self._path(layer)
        if p.exists():
            p.unlink()
