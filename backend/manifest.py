"""Manifest file editor — read/validate/write for the four files that define
a world: timeline.json, gaia.json, map.json, render.json.

Writes are atomic (write to .tmp + os.replace) and keep one rolling .bak of
the previous version. Validation is JSON-Schema based; schemas live under
backend/schemas/<kind>.schema.json.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

import jsonschema

SCHEMAS_DIR = Path(__file__).resolve().parent / "schemas"

# Manifest kind → filename on disk. The kind is what callers pass on the URL;
# the filename is what we actually read/write inside the world dir.
KINDS: dict[str, str] = {
    "timeline": "timeline.json",
    "gaia":     "gaia.json",
    "map":      "map.json",
    "render":   "render.json",
    "orbitals": "orbitals.json",
}


def _schema_path(kind: str) -> Path:
    if kind not in KINDS:
        raise KeyError(f"unknown manifest kind: {kind!r}")
    return SCHEMAS_DIR / f"{kind}.schema.json"


def get_schema(kind: str) -> dict[str, Any]:
    return json.loads(_schema_path(kind).read_text(encoding="utf-8"))


def list_kinds() -> list[str]:
    return list(KINDS.keys())


def file_path(world_dir: Path, kind: str) -> Path:
    if kind not in KINDS:
        raise KeyError(f"unknown manifest kind: {kind!r}")
    return world_dir / KINDS[kind]


def load(world_dir: Path, kind: str) -> dict[str, Any] | list[Any] | None:
    p = file_path(world_dir, kind)
    if not p.is_file() or p.stat().st_size == 0:
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def validate(kind: str, payload: Any) -> list[str]:
    """Return a list of validation error messages (empty list = valid)."""
    schema = get_schema(kind)
    validator = jsonschema.Draft7Validator(schema)
    errs = sorted(validator.iter_errors(payload), key=lambda e: list(e.absolute_path))
    return [
        f"{'/'.join(str(p) for p in e.absolute_path) or '(root)'}: {e.message}"
        for e in errs
    ]


def save(world_dir: Path, kind: str, payload: Any) -> dict[str, Any]:
    """Validate then write atomically. Keeps a single rolling .bak.

    Returns {kind, path, bytes, backup} on success.
    Raises ValueError on schema failure with the list of errors attached.
    """
    errs = validate(kind, payload)
    if errs:
        raise ValueError({"validation_errors": errs})

    p = file_path(world_dir, kind)
    p.parent.mkdir(parents=True, exist_ok=True)
    bak = None
    if p.exists():
        bak = p.with_suffix(p.suffix + ".bak")
        shutil.copy2(p, bak)

    tmp = p.with_suffix(p.suffix + ".tmp")
    text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, p)
    return {
        "kind": kind,
        "path": str(p),
        "bytes": len(text),
        "backup": str(bak) if bak else None,
    }
