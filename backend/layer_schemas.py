"""Per-layer JSON Schemas — describe the typed properties of features in a layer.

Stored as sidecar files at `<world>/raw/schemas/<layer>.schema.json`,
independent of the storage backend (GeoJSON / SpatiaLite / PostGIS). The
schema constrains the `properties` object of GeoJSON features in that layer
(geometry type is determined by the draw tool, not the schema).

A "valid" layer schema is a JSON object that conforms to JSON Schema
draft-07 metaschema — we validate that on PUT before writing.
"""
from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from typing import Any

import jsonschema

SAFE_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_:.\-]{0,62}$")  # incl. deck tables like d1:walls


def _dir(world_dir: Path) -> Path:
    p = world_dir / "raw" / "schemas"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _path(world_dir: Path, layer: str) -> Path:
    if not SAFE_NAME.match(layer):
        raise ValueError(f"unsafe layer name: {layer!r}")
    return _dir(world_dir) / f"{layer}.schema.json"


def get(world_dir: Path, layer: str) -> dict[str, Any]:
    """Return the layer schema, or an empty stub if none exists yet."""
    p = _path(world_dir, layer)
    if not p.is_file() or p.stat().st_size == 0:
        return {"type": "object", "properties": {}}
    return json.loads(p.read_text(encoding="utf-8"))


def list_layers(world_dir: Path) -> list[str]:
    d = _dir(world_dir)
    return sorted(p.stem.replace(".schema", "") for p in d.glob("*.schema.json"))


def validate_meta(schema: Any) -> list[str]:
    """Validate that `schema` is itself a valid JSON Schema (draft-07)."""
    if not isinstance(schema, dict):
        return ["root: layer schema must be an object"]
    try:
        jsonschema.Draft7Validator.check_schema(schema)
    except jsonschema.exceptions.SchemaError as e:
        return [f"{'/'.join(str(p) for p in e.absolute_path) or '(root)'}: {e.message}"]
    return []


def put(world_dir: Path, layer: str, schema: Any) -> dict[str, Any]:
    """Validate then atomically write the layer schema; keeps one rolling .bak."""
    errs = validate_meta(schema)
    if errs:
        raise ValueError({"validation_errors": errs})
    p = _path(world_dir, layer)
    bak = None
    if p.exists():
        bak = p.with_suffix(p.suffix + ".bak")
        shutil.copy2(p, bak)
    tmp = p.with_suffix(p.suffix + ".tmp")
    text = json.dumps(schema, indent=2, ensure_ascii=False) + "\n"
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, p)
    return {
        "layer": layer,
        "path": str(p),
        "bytes": len(text),
        "backup": str(bak) if bak else None,
    }


def delete(world_dir: Path, layer: str) -> None:
    p = _path(world_dir, layer)
    if p.exists():
        p.unlink()
