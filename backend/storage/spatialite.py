"""SpatiaLite storage adapter — column convention: `the_geom`, SRID 4326.

Saves are non-destructive (see diff.py): rows are matched by a stable `fid` column, only changed
rows are written, every attribute column is kept, and each change is recorded in `_ofm_history`,
all in one transaction.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

try:
    import spatialite  # type: ignore
except ImportError:  # pragma: no cover
    spatialite = None

import shapely
from shapely.geometry import shape

from .diff import (CITATIONS, FID, HISTORY_TABLE, ROW_PREFIX, StoredRow, coerce_geometry, geometry_from_wkt,
                   history_record, new_columns, new_fid, plan_save)

# Table names as OFM uses them, including per-deck tables such as "d1:walls" (quoted in SQL).
SAFE_TABLE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_:.\-]{0,62}$")

# Map GeoJSON geometry types → SpatiaLite column geometry types
GEOM_TYPE_MAP = {
    "Point": "POINT",
    "MultiPoint": "MULTIPOINT",
    "LineString": "LINESTRING",
    "MultiLineString": "MULTILINESTRING",
    "Polygon": "POLYGON",
    "MultiPolygon": "MULTIPOLYGON",
}


# SpatiaLite geometry_columns.geometry_type codes (Z/M variants add 1000/2000/3000).
GEOM_CODES = {0: "GEOMETRY", 1: "POINT", 2: "LINESTRING", 3: "POLYGON", 4: "MULTIPOINT",
              5: "MULTILINESTRING", 6: "MULTIPOLYGON", 7: "GEOMETRYCOLLECTION"}


def ident(name: str) -> str:
    """A plain identifier derived from a name (for index names)."""
    return re.sub(r"[^A-Za-z0-9_]", "_", name)[:63]


def q(name: str) -> str:
    """Quote a column name read from the database."""
    return '"' + name.replace('"', '""') + '"'


def wkt2d(geometry: dict) -> str:
    """WKT without Z/M: layers are XY, and some sources pad coordinates with a dummy Z."""
    return shapely.force_2d(shape(geometry)).wkt


class SpatiaLiteAdapter:
    mode = "spatialite"

    def __init__(self, world_dir: Path, db_filename: str | None = None):
        if spatialite is None:
            raise RuntimeError(
                "spatialite python package not installed (pip install spatialite)"
            )
        self.world = world_dir.name
        # Discover the .db file. Convention from azgaar-importer: <world>.db
        # in the world dir, sometimes <world>_2.db, etc.
        candidates = list(world_dir.glob("*.db"))
        if db_filename:
            self.path = world_dir / db_filename
        elif (world_dir / f"{self.world}.db").exists():
            self.path = world_dir / f"{self.world}.db"
        elif candidates:
            self.path = candidates[0]
        else:
            self.path = world_dir / f"{self.world}.db"
        self.conn = spatialite.connect(str(self.path))
        # Only initialise spatial metadata if the table doesn't already exist —
        # InitSpatialMetaData on an already-initialised DB raises and the error
        # gets caught here, but the underlying SQL transaction is left aborted
        # which then breaks the next query. Check first, init only if needed.
        cur = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='spatial_ref_sys'"
        )
        if not cur.fetchone():
            try:
                self.conn.execute("SELECT InitSpatialMetaData(1);")
                self.conn.commit()
            except Exception:
                self.conn.rollback()

    def _ensure_table(self, layer: str, geom_type: str) -> None:
        if not SAFE_TABLE.match(layer):
            raise ValueError(f"unsafe table name: {layer!r}")
        cur = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (layer,)
        )
        if cur.fetchone():
            return
        self.conn.execute(
            f"CREATE TABLE {q(layer)} ("
            "id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,"
            f"{FID} TEXT NOT NULL UNIQUE,"
            "name TEXT NULL,"
            "class TEXT NULL,"
            "properties TEXT NULL)"
        )
        self.conn.execute(
            "SELECT AddGeometryColumn(?, 'the_geom', 4326, ?, 'XY', 1)", (layer, geom_type)
        )

    def list_layers(self) -> list[dict[str, Any]]:
        cur = self.conn.execute(
            "SELECT f_table_name, geometry_type FROM geometry_columns"
        )
        rows = []
        for tbl, gtype in cur.fetchall():
            cnt = self.conn.execute(f"SELECT COUNT(*) FROM {q(tbl)}").fetchone()[0]
            name = GEOM_CODES.get(int(gtype) % 1000, str(gtype)) if str(gtype).isdigit() else str(gtype)
            rows.append({"name": tbl, "count": cnt, "geometry_type": name})
        return rows

    def _table_info(self, table: str) -> list[tuple[str, int]]:
        """[(column name, pk position), ...] in column order."""
        if not SAFE_TABLE.match(table):
            raise ValueError(f"unsafe table name: {table!r}")
        return [(r[1], r[5]) for r in self.conn.execute(f"PRAGMA table_info({q(table)})").fetchall()]

    def _columns(self, table: str) -> list[str]:
        """Return ordered list of column names for the given table."""
        return [name for name, _ in self._table_info(table)]

    def _geom_column(self, table: str) -> str | None:
        cur = self.conn.execute(
            "SELECT f_geometry_column FROM geometry_columns WHERE f_table_name = ?",
            (table,),
        )
        row = cur.fetchone()
        return row[0] if row else None

    def _geom_type(self, table: str) -> str | None:
        row = self.conn.execute(
            "SELECT geometry_type FROM geometry_columns WHERE f_table_name = ?", (table,)).fetchone()
        if not row:
            return None
        code = int(row[0]) % 1000  # 1000s encode Z/M variants
        return GEOM_CODES.get(code)

    def _layout(self, table: str) -> tuple[str, str | None, list[str], bool]:
        """(geometry column, primary-key column or None, attribute columns, has properties column)."""
        info = self._table_info(table)
        names = [n for n, _ in info]
        geom_col = self._geom_column(table) or "the_geom"
        pks = [n for n, pos in sorted(info, key=lambda x: x[1]) if pos]
        pk = pks[0] if len(pks) == 1 else None
        attrs = [n for n in names if n not in (geom_col, pk, FID, "properties", CITATIONS)]
        return geom_col, pk, attrs, "properties" in names

    def _read_rows(self, table: str) -> list[dict[str, Any]]:
        geom_col, pk, _, _ = self._layout(table)
        other = [n for n in self._columns(table) if n != geom_col]
        quoted = ", ".join(q(n) for n in other)
        cur = self.conn.execute(
            f"SELECT {quoted}, AsText({q(geom_col)}), rowid FROM {q(table)}")
        rows = []
        for r in cur.fetchall():
            d = dict(zip(other + ["_ofm_wkt", "_ofm_rowid"], r))
            rows.append(d)
        return rows

    @staticmethod
    def _list(value: Any) -> list | None:
        if isinstance(value, str) and value:
            try:
                parsed = json.loads(value)
                return parsed if isinstance(parsed, list) else None
            except ValueError:
                return None
        return None

    @staticmethod
    def _extras(value: Any) -> dict[str, Any]:
        if isinstance(value, str) and value:
            try:
                parsed = json.loads(value)
                return parsed if isinstance(parsed, dict) else {}
            except ValueError:
                return {}
        return {}

    def load_layer(self, layer: str) -> dict[str, Any]:
        if not SAFE_TABLE.match(layer):
            raise ValueError(f"unsafe table name: {layer!r}")
        if not self._columns(layer):
            return {"type": "FeatureCollection", "features": []}  # not created yet: empty
        _, pk, attrs, _ = self._layout(layer)
        features = []
        for r in self._read_rows(layer):
            props = self._extras(r.get("properties"))
            for k in attrs:
                if r.get(k) is not None:
                    props.setdefault(k, r[k])
            fid = r.get(FID)
            key = fid if fid is not None else f"{ROW_PREFIX}{r[pk] if pk else r['_ofm_rowid']}"
            feat = {
                "type": "Feature",
                "id": str(key),
                "geometry": geometry_from_wkt(r["_ofm_wkt"]),
                "properties": props,
            }
            citations = self._list(r.get(CITATIONS))
            if citations:
                feat[CITATIONS] = citations
            features.append(feat)
        return {"type": "FeatureCollection", "features": features}

    def _ensure_fid(self, layer: str) -> None:
        """Give an older table a stable, unique fid per row."""
        if FID not in self._columns(layer):
            self.conn.execute(f"ALTER TABLE {q(layer)} ADD COLUMN {FID} TEXT")
        missing = self.conn.execute(f"SELECT rowid FROM {q(layer)} WHERE {FID} IS NULL").fetchall()
        for (rowid,) in missing:
            self.conn.execute(f"UPDATE {q(layer)} SET {FID} = ? WHERE rowid = ?", (new_fid(), rowid))
        self.conn.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS {q(ident(layer + '_' + FID + '_idx'))} ON {q(layer)} ({FID})")

    def _ensure_history(self) -> None:
        self.conn.execute(
            f"CREATE TABLE IF NOT EXISTS {HISTORY_TABLE} ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "layer TEXT NOT NULL,"
            "fid TEXT NOT NULL,"
            "op TEXT NOT NULL CHECK (op IN ('insert', 'update', 'delete')),"
            "before TEXT,"
            "after TEXT,"
            "editor TEXT,"
            "at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')))"
        )
        self.conn.execute(
            f"CREATE INDEX IF NOT EXISTS {HISTORY_TABLE}_feature_idx ON {HISTORY_TABLE} (layer, fid)")

    @staticmethod
    def _value(value: Any) -> Any:
        return json.dumps(value) if isinstance(value, (dict, list)) else value

    def save_layer(self, layer: str, feature_collection: dict[str, Any],
                   editor: str | None = None) -> dict[str, Any]:
        if not SAFE_TABLE.match(layer):
            raise ValueError(f"unsafe table name: {layer!r}")
        feats = feature_collection.get("features", [])
        try:
            if not self._columns(layer):
                if not feats:
                    return {"layer": layer, "saved": 0, "inserted": 0, "updated": 0,
                            "deleted": 0, "unchanged": 0, "unstored_attributes": []}
                first = (feats[0].get("geometry") or {}).get("type", "Polygon")
                self._ensure_table(layer, GEOM_TYPE_MAP.get(first, "GEOMETRY"))
            self._ensure_fid(layer)
            self._ensure_history()
            geom_col, pk, attrs, has_props = self._layout(layer)
            gtype = self._geom_type(layer)

            rows = self._read_rows(layer)
            alias = {f"{ROW_PREFIX}{r[pk] if pk else r['_ofm_rowid']}": r[FID] for r in rows}
            stored = [StoredRow(key=str(r[FID]), columns={a: r.get(a) for a in attrs},
                                extras=self._extras(r.get("properties")) if has_props else {},
                                geometry=geometry_from_wkt(r["_ofm_wkt"]),
                                citations=self._list(r.get(CITATIONS))) for r in rows]
            incoming = [{**f, "id": alias.get(str(f.get("id")), f.get("id"))} for f in feats]
            plan = plan_save(stored, incoming, attrs, has_props)
            # Attributes the table has no place for: add typed columns, then plan again.
            added = new_columns(incoming, plan.unstored, "spatialite") if plan.unstored else {}
            if added:
                for name, sql_type in added.items():
                    self.conn.execute(f"ALTER TABLE {q(layer)} ADD COLUMN {q(name)} {sql_type}")
                geom_col, pk, attrs, has_props = self._layout(layer)
                for r in stored:
                    r.columns.update({name: None for name in added})
                plan = plan_save(stored, incoming, attrs, has_props)
                plan.added_columns = set(added)
            if plan.needs_citations_column() and CITATIONS not in self._columns(layer):
                self.conn.execute(f"ALTER TABLE {q(layer)} ADD COLUMN {CITATIONS} TEXT")

            for ch in plan.deletes:
                self.conn.execute(f"DELETE FROM {q(layer)} WHERE {FID} = ?", (ch.key,))
            for ch in plan.inserts:
                names = list(ch.columns) + (["properties"] if ch.extras is not None else [])
                values = [self._value(ch.columns[n]) for n in ch.columns]
                if ch.extras is not None:
                    values.append(json.dumps(ch.extras))
                if ch.citations is not None:
                    names.append(CITATIONS)
                    values.append(json.dumps(ch.citations, ensure_ascii=False))
                col_list = ", ".join(q(n) for n in [FID, *names, geom_col])
                ph = ", ".join(["?"] * (len(names) + 1) + ["GeomFromText(?, 4326)"])
                self.conn.execute(f"INSERT INTO {q(layer)} ({col_list}) VALUES ({ph})",
                                  (ch.fid, *values, wkt2d(coerce_geometry(ch.geometry, gtype))))
            for ch in plan.updates:
                sets, values = [], []
                for n, v in ch.columns.items():
                    sets.append(f"{q(n)} = ?")
                    values.append(self._value(v))
                if ch.extras is not None:
                    sets.append("properties = ?")
                    values.append(json.dumps(ch.extras))
                if ch.citations is not None:
                    sets.append(f"{CITATIONS} = ?")
                    values.append(json.dumps(ch.citations, ensure_ascii=False))
                if ch.geometry is not None:
                    sets.append(f"{q(geom_col)} = GeomFromText(?, 4326)")
                    values.append(wkt2d(coerce_geometry(ch.geometry, gtype)))
                self.conn.execute(f"UPDATE {q(layer)} SET {', '.join(sets)} WHERE {FID} = ?",
                                  (*values, ch.key))
            for op, changes in (("insert", plan.inserts), ("update", plan.updates),
                                ("delete", plan.deletes)):
                for ch in changes:
                    rec = history_record(layer, ch, op, editor)
                    self.conn.execute(
                        f"INSERT INTO {HISTORY_TABLE} (layer, fid, op, before, after, editor) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (layer, rec["fid"], op,
                         json.dumps(rec["before"], default=str) if rec["before"] else None,
                         json.dumps(rec["after"], default=str) if rec["after"] else None, editor))
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return plan.summary(layer)

    def delete_layer(self, layer: str) -> None:
        if not SAFE_TABLE.match(layer):
            raise ValueError(f"unsafe table name: {layer!r}")
        self.conn.execute("SELECT DiscardGeometryColumn(?, 'the_geom')", (layer,))
        self.conn.execute(f"DROP TABLE IF EXISTS {q(layer)}")
        self.conn.commit()
