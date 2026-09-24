"""Cited GeoJSON (https://www.openhistorymap.org/cited-geojson/) for the editor.

- Each world keeps a source registry in `<world>/raw/sources.json`: the same object as a Cited
  GeoJSON document's `sources` member, keyed by source IRI.
- Features carry their `citations` (stored by the adapters, see storage/diff.py).
- Georeferences of scanned maps live in `<world>/raw/georef/*.json` (see georef.py).

`export_layer` assembles a layer as a Cited GeoJSON document — only the sources it cites (and
their `derivedFrom` ancestors), and the georeferences of the files it cites — and validates it
against the vendored 0.1 JSON Schema plus the cross-references the schema cannot express.
`import_layer` does the reverse: it merges the document's sources into the registry and hands the
features, with their citations, to the storage adapter.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema

PROFILE = "https://www.openhistorymap.org/cited-geojson/0.1"
CONTEXT = "https://www.openhistorymap.org/cited-geojson/0.1/context.jsonld"
SCHEMA_PATH = Path(__file__).parent / "schemas" / "cited-geojson-0.1.schema.json"
_validator: jsonschema.Draft202012Validator | None = None


class CitedGeoJSONError(ValueError):
    """A document or registry does not conform; `.errors` lists why."""

    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


def validator() -> jsonschema.Draft202012Validator:
    global _validator
    if _validator is None:
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        _validator = jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())
    return _validator


def schema_errors(doc: dict[str, Any]) -> list[str]:
    return [f"{'/'.join(map(str, e.absolute_path)) or '(root)'}: {e.message}"
            for e in sorted(validator().iter_errors(doc), key=lambda e: list(e.absolute_path))]


def crossref_errors(doc: dict[str, Any]) -> list[str]:
    """Checks the JSON Schema cannot express (Cited GeoJSON 0.1 §7)."""
    errors = []
    sources = doc.get("sources", {})
    files = {iri: {f["id"] for f in src.get("files", [])} for iri, src in sources.items()}
    for iri, src in sources.items():
        for parent in src.get("derivedFrom", []):
            if parent not in sources:
                errors.append(f"source {iri} derivedFrom {parent}, which is not in sources")
    seen = set()
    for feat in doc.get("features", []):
        fid = feat.get("id")
        if fid in seen:
            errors.append(f"duplicate feature id {fid}")
        seen.add(fid)
        for i, cit in enumerate(feat.get("citations", [])):
            if cit.get("source") not in sources:
                errors.append(f"feature {fid} citation {i + 1}: source {cit.get('source')} is not in sources")
            elif "file" in cit and cit["file"] not in files[cit["source"]]:
                errors.append(f"feature {fid} citation {i + 1}: file {cit['file']} is not a file of {cit['source']}")
    return errors


# --------------------------------------------------------------------------- registry

def registry_path(world_dir: Path) -> Path:
    return world_dir / "raw" / "sources.json"


def load_sources(world_dir: Path) -> dict[str, Any]:
    p = registry_path(world_dir)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def validate_sources(sources: dict[str, Any]) -> None:
    probe = {"type": "FeatureCollection", "conformsTo": [PROFILE], "sources": sources, "features": []}
    errors = schema_errors(probe) + crossref_errors(probe)
    if not sources:
        errors = []  # an empty registry is fine; the schema only requires sources in documents
    if errors:
        raise CitedGeoJSONError(errors)


def save_sources(world_dir: Path, sources: dict[str, Any]) -> dict[str, Any]:
    validate_sources(sources)
    p = registry_path(world_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(sources, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)
    return sources


def merge_sources(world_dir: Path, incoming: dict[str, Any]) -> dict[str, list[str]]:
    """Add incoming sources to the registry. Existing entries win; differing ones are reported."""
    registry = load_sources(world_dir)
    added, conflicts = [], []
    for iri, src in incoming.items():
        if iri not in registry:
            registry[iri] = src
            added.append(iri)
        elif json.dumps(registry[iri], sort_keys=True) != json.dumps(src, sort_keys=True):
            conflicts.append(iri)
    if added:
        save_sources(world_dir, registry)
    return {"added": added, "conflicts": conflicts}


# --------------------------------------------------------------------------- georeferences

def load_georeferences(world_dir: Path) -> list[dict[str, Any]]:
    d = world_dir / "raw" / "georef"
    if not d.is_dir():
        return []
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(d.glob("*.json"))]


def _georef_target(gr: dict[str, Any]) -> str | None:
    target = gr.get("target")
    if isinstance(target, str):
        return target
    source = (target or {}).get("source")
    return source.get("id") if isinstance(source, dict) else source or (target or {}).get("id")


# --------------------------------------------------------------------------- export / import

def export_layer(world_dir: Path, timeline: dict[str, Any], layer: str,
                 fc: dict[str, Any]) -> dict[str, Any]:
    registry = load_sources(world_dir)
    features, cited = [], set()
    for f in fc.get("features", []):
        feat = {"type": "Feature", "id": str(f.get("id")), "geometry": f.get("geometry"),
                "properties": f.get("properties") or {}}
        if f.get("citations"):
            feat["citations"] = f["citations"]
            cited.update(c.get("source") for c in f["citations"])
        features.append(feat)

    # The cited sources plus everything they were derived from.
    wanted, stack = set(), [s for s in cited if s]
    while stack:
        iri = stack.pop()
        if iri in wanted or iri not in registry:
            wanted.add(iri)
            continue
        wanted.add(iri)
        stack.extend(registry[iri].get("derivedFrom", []))
    sources = {iri: registry[iri] for iri in sorted(wanted) if iri in registry}

    cited_files = {v for iri in sources for fl in sources[iri].get("files", [])
                   for v in (fl.get("id"), fl.get("url"), fl.get("iiif")) if v}
    georefs = [gr for gr in load_georeferences(world_dir) if _georef_target(gr) in cited_files]

    doc: dict[str, Any] = {
        "@context": CONTEXT,
        "type": "FeatureCollection",
        "conformsTo": [PROFILE],
        "name": f"{timeline.get('name', world_dir.name)} — {layer}",
    }
    if timeline.get("calendar"):
        doc["calendar"] = timeline["calendar"]
    doc["sources"] = sources
    if georefs:
        doc["georeferences"] = georefs
    doc["features"] = features

    missing = sorted(s for s in cited if s and s not in registry)
    errors = [f"source {s} is cited but not in the world's source registry" for s in missing]
    if not sources:
        errors.append("no feature in this layer cites a source")
    errors += schema_errors(doc) + crossref_errors(doc)
    if errors:
        raise CitedGeoJSONError(errors)
    return doc


def prepare_import(world_dir: Path, doc: dict[str, Any]) -> tuple[dict[str, Any], dict[str, list[str]]]:
    """Validate a Cited GeoJSON document, merge its sources, return (FeatureCollection, merge report)."""
    errors = schema_errors(doc) + crossref_errors(doc)
    if errors:
        raise CitedGeoJSONError(errors)
    report = merge_sources(world_dir, doc.get("sources", {}))
    fc = {"type": "FeatureCollection", "features": doc.get("features", [])}
    return fc, report
