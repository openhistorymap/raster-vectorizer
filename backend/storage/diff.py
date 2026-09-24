"""Storage-independent planning for non-destructive layer saves.

Saving a layer used to delete every row and re-insert only `name`, `class` and `properties`, which
destroyed every other column (from_time, to_time, subclass, wiki, ...) and renumbered every
feature. Adapters now load the rows they hold, ask `plan_save` what changed, and apply only that:

- features are matched by their stable id (`fid`); unknown or missing ids are inserts, stored rows
  that are no longer present are deletes, identical features are left untouched;
- each property is written to the column of the same name when the table has one, otherwise into
  the `properties` JSON column when there is one, otherwise it is reported as unstored rather than
  silently dropped;
- properties absent from an incoming feature leave the stored value alone (the editor omits NULL
  columns when loading), while an explicit null clears it.

Adapters record each insert, update and delete in an `_ofm_history` table (before/after images)
inside the same transaction as the change.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any

from shapely.geometry import mapping, shape

HISTORY_TABLE = "_ofm_history"
FID = "fid"
CITATIONS = "citations"   # feature-level member (Cited GeoJSON) and its storage column
ROW_PREFIX = "row:"


class SaveRejected(ValueError):
    """The incoming layer cannot be saved as sent (reported to the client as HTTP 422)."""


def new_fid() -> str:
    return str(uuid.uuid4())


def is_uuid(value: Any) -> bool:
    try:
        uuid.UUID(str(value))
        return True
    except ValueError:
        return False


def normalise_geometry(geom: dict | None) -> str | None:
    """Canonical WKT for change detection (None for missing geometry)."""
    return shape(geom).wkt if geom else None


def _norm_value(v: Any) -> Any:
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, (dict, list)):
        return json.dumps(v, sort_keys=True, default=str)
    if hasattr(v, "__float__") and not isinstance(v, str):  # Decimal and friends
        return float(v)
    return str(v) if not isinstance(v, str) else v


def same_value(a: Any, b: Any) -> bool:
    return _norm_value(a) == _norm_value(b)


@dataclass
class StoredRow:
    """A row as the adapter holds it: key, column values, JSON extras, geometry."""
    key: str                       # fid, or "row:<pk>" for tables without a fid column
    columns: dict[str, Any]        # attribute columns (no pk, fid, geometry, properties)
    extras: dict[str, Any]         # contents of the `properties` JSON column, if any
    geometry: dict | None          # GeoJSON geometry
    citations: list | None = None  # Cited GeoJSON citations, if the layer stores them


@dataclass
class RowChange:
    key: str | None                # stored key for updates/deletes; None for inserts
    fid: str                       # fid the row has (or will have)
    columns: dict[str, Any]        # column values to write (only keys that change or are new)
    extras: dict[str, Any] | None  # full new `properties` JSON, or None to leave it alone
    geometry: dict | None
    before: dict | None            # history image before the change
    after: dict | None             # history image after the change
    citations: list | None = None  # new citations to write, or None to leave them alone


@dataclass
class SavePlan:
    inserts: list[RowChange] = field(default_factory=list)
    updates: list[RowChange] = field(default_factory=list)
    deletes: list[RowChange] = field(default_factory=list)
    unchanged: int = 0
    unstored: set[str] = field(default_factory=set)

    def needs_citations_column(self) -> bool:
        return any(ch.citations for ch in self.inserts + self.updates)

    def summary(self, layer: str) -> dict[str, Any]:
        return {
            "layer": layer,
            "saved": len(self.inserts) + len(self.updates) + self.unchanged,
            "inserted": len(self.inserts),
            "updated": len(self.updates),
            "deleted": len(self.deletes),
            "unchanged": self.unchanged,
            "unstored_attributes": sorted(self.unstored),
        }


def _image(columns: dict, extras: dict, geometry: dict | None, citations: list | None = None) -> dict:
    props = {**extras, **{k: v for k, v in columns.items() if v is not None}}
    image = {"properties": props, "geometry": geometry}
    if citations:
        image[CITATIONS] = citations
    return image


def _same_json(a: Any, b: Any) -> bool:
    return json.dumps(a, sort_keys=True, default=str) == json.dumps(b, sort_keys=True, default=str)


def plan_save(stored: list[StoredRow], incoming: list[dict], columns: list[str],
              has_extras_column: bool) -> SavePlan:
    """Work out the inserts, updates and deletes that turn `stored` into `incoming`.

    `stored` rows are keyed by fid (adapters add the fid column before planning and translate
    "row:<pk>" ids from older loads). `columns` are the table's attribute columns (excluding pk,
    fid, geometry and `properties`).
    """
    col_set = set(columns)
    by_key = {r.key: r for r in stored}
    plan = SavePlan()
    seen: set[str] = set()

    for i, feat in enumerate(incoming):
        if feat.get("type") not in (None, "Feature"):
            raise SaveRejected(f"feature {i}: not a GeoJSON Feature")
        geometry = feat.get("geometry")
        if not geometry:
            raise SaveRejected(f"feature {i}: has no geometry")
        props = dict(feat.get("properties") or {})
        props.pop(FID, None)
        props.pop(CITATIONS, None)
        citations = feat.get(CITATIONS)
        if citations is not None and not isinstance(citations, list):
            raise SaveRejected(f"feature {i}: `citations` must be an array")
        fid = feat.get("id")
        key = str(fid) if fid is not None else None
        if key in seen:
            raise SaveRejected(f"feature {i}: id {key} appears twice")

        col_vals = {k: v for k, v in props.items() if k in col_set}
        extra_vals = {k: v for k, v in props.items() if k not in col_set}
        if extra_vals and not has_extras_column:
            plan.unstored.update(extra_vals)
            extra_vals = {}

        row = by_key.get(key) if key is not None else None
        if row is None:
            new = is_uuid(key) and key not in by_key and not str(key).startswith(ROW_PREFIX)
            fid_value = key if new else new_fid()
            extras = extra_vals if has_extras_column else None
            plan.inserts.append(RowChange(
                key=None, fid=fid_value, columns=col_vals, extras=extras, geometry=geometry,
                before=None, after=_image(col_vals, extra_vals, geometry, citations),
                citations=citations or None))
            if key is not None:
                seen.add(key)
            continue

        seen.add(key)
        changed_cols = {k: v for k, v in col_vals.items() if not same_value(row.columns.get(k), v)}
        extras_changed = has_extras_column and json.dumps(extra_vals, sort_keys=True, default=str) != \
            json.dumps(row.extras, sort_keys=True, default=str)
        geom_changed = normalise_geometry(geometry) != normalise_geometry(row.geometry)
        # An absent `citations` member leaves stored citations alone; [] clears them.
        cit_changed = citations is not None and not _same_json(citations, row.citations or [])
        if not (changed_cols or extras_changed or geom_changed or cit_changed):
            plan.unchanged += 1
            continue
        after_cols = {**row.columns, **col_vals}
        plan.updates.append(RowChange(
            key=row.key, fid=row.key,
            columns=changed_cols, extras=extra_vals if extras_changed else None,
            geometry=geometry if geom_changed else None,
            before=_image(row.columns, row.extras, row.geometry, row.citations),
            after=_image(after_cols, extra_vals if has_extras_column else row.extras, geometry,
                         citations if citations is not None else row.citations),
            citations=citations if cit_changed else None))

    for row in stored:
        if row.key not in seen:
            plan.deletes.append(RowChange(
                key=row.key, fid=row.key, columns={}, extras=None, geometry=None,
                before=_image(row.columns, row.extras, row.geometry, row.citations), after=None))
    return plan


def history_record(layer: str, change: RowChange, op: str, editor: str | None) -> dict[str, Any]:
    return {"layer": layer, "fid": change.fid, "op": op,
            "before": change.before, "after": change.after, "editor": editor}


def geometry_from_wkt(wkt_text: str | None) -> dict | None:
    from shapely import wkt as shapely_wkt
    return mapping(shapely_wkt.loads(wkt_text)) if wkt_text else None


def coerce_geometry(geometry: dict, column_type: str | None) -> dict:
    """Fit a GeoJSON geometry to a typed geometry column, or reject it clearly.

    Single geometries are promoted to their Multi form when the column is Multi; anything else that
    does not match raises SaveRejected instead of failing deep inside the database.
    """
    if not column_type:
        return geometry
    want = column_type.upper()
    if want in ("GEOMETRY", "GEOMETRYCOLLECTION"):
        return geometry
    have = geometry["type"].upper()
    if have == want:
        return geometry
    if want == "MULTI" + have:
        return {"type": "Multi" + geometry["type"], "coordinates": [geometry["coordinates"]]}
    raise SaveRejected(f"geometry {geometry['type']} does not fit this layer's {column_type} column")
