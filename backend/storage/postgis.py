"""PostGIS storage adapter — column convention: `geom`, SRID 4326.

Saves are non-destructive (see diff.py): rows are matched by a stable `fid` (uuid) column, only
changed rows are written, every attribute column the table has is kept, and each change is
recorded in `_ofm_history`. Tables created before stable ids get a `fid` column, backfilled, on
their first save; until then they load with `row:<pk>` ids.
"""
from __future__ import annotations

import json
import os
import re
from functools import partial
from pathlib import Path
from typing import Any

try:
    import psycopg
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb
except ImportError:  # pragma: no cover
    psycopg = None  # type: ignore

import shapely
from shapely.geometry import shape

from .diff import (FID, HISTORY_TABLE, ROW_PREFIX, SaveRejected, StoredRow, coerce_geometry,
                   geometry_from_wkt, history_record, plan_save)

SAFE_IDENT = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")

GEOM_TYPE_MAP = {
    "Point": "POINT",
    "MultiPoint": "MULTIPOINT",
    "LineString": "LINESTRING",
    "MultiLineString": "MULTILINESTRING",
    "Polygon": "POLYGON",
    "MultiPolygon": "MULTIPOLYGON",
}

JSON_TYPES = {"json", "jsonb"}


def q(name: str) -> str:
    """Quote a column name read from the database (they may contain spaces or capitals)."""
    return '"' + name.replace('"', '""') + '"'


def wkt2d(geometry: dict) -> str:
    """WKT without Z/M: OFM layers are 2-D, and some sources pad coordinates with a dummy Z."""
    return shapely.force_2d(shape(geometry)).wkt


def jsonb(value: Any) -> Any:
    """Jsonb that tolerates Decimal, dates and other database values."""
    return Jsonb(value, dumps=partial(json.dumps, default=str))


class PostGISAdapter:
    mode = "postgis"

    def __init__(self, world_dir: Path, connection_cfg: dict[str, Any]):
        if psycopg is None:
            raise RuntimeError(
                "psycopg not installed (pip install psycopg[binary])"
            )
        self.db = connection_cfg.get("db") or world_dir.name
        # Allow per-world override of host/port/user via timeline.json.connection;
        # fall back to env vars (the shared OFM PostGIS host).
        self.dsn = " ".join(
            f"{k}={v}"
            for k, v in {
                "host": connection_cfg.get("host", os.environ.get("OFM_PG_HOST", "51.15.160.236")),
                "port": connection_cfg.get("port", os.environ.get("OFM_PG_PORT", "45432")),
                "user": connection_cfg.get("user", os.environ.get("OFM_PG_USER", "postgres")),
                "password": connection_cfg.get("password", os.environ.get("OFM_PG_PASSWORD", "")),
                "dbname": self.db,
            }.items()
            if v != ""
        )

    def _conn(self):
        return psycopg.connect(self.dsn, row_factory=dict_row)

    @staticmethod
    def _check(name: str) -> str:
        if not SAFE_IDENT.match(name):
            raise ValueError(f"unsafe identifier: {name!r}")
        return name

    def _ensure_table(self, conn, layer: str, geom_type: str) -> None:
        self._check(layer)
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {layer} ("
            "id SERIAL PRIMARY KEY,"
            f"{FID} uuid NOT NULL UNIQUE,"
            "name TEXT,"
            "class TEXT,"
            "properties JSONB,"
            f"geom geometry({geom_type}, 4326))"
        )
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS {layer}_geom_idx ON {layer} USING GIST (geom)"
        )

    def _ensure_fid(self, conn, layer: str, cols: dict[str, str]) -> None:
        """Give an older table a stable, unique fid per row (PostgreSQL 12: no gen_random_uuid)."""
        if FID in cols:
            conn.execute(
                f"UPDATE {layer} SET {FID} = md5(random()::text || clock_timestamp()::text)::uuid "
                f"WHERE {FID} IS NULL")
            return
        conn.execute(f"ALTER TABLE {layer} ADD COLUMN {FID} uuid")
        conn.execute(
            f"UPDATE {layer} SET {FID} = md5(random()::text || clock_timestamp()::text || "
            f"ctid::text)::uuid")
        conn.execute(f"ALTER TABLE {layer} ALTER COLUMN {FID} SET NOT NULL")
        conn.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS {layer}_{FID}_idx ON {layer} ({FID})")

    def _ensure_history(self, conn) -> None:
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {HISTORY_TABLE} ("
            "id BIGSERIAL PRIMARY KEY,"
            "layer TEXT NOT NULL,"
            "fid TEXT NOT NULL,"
            "op TEXT NOT NULL CHECK (op IN ('insert', 'update', 'delete')),"
            "before JSONB,"
            "after JSONB,"
            "editor TEXT,"
            "at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS {HISTORY_TABLE}_feature_idx ON {HISTORY_TABLE} (layer, fid)")

    def list_layers(self) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT f_table_name, type FROM geometry_columns "
                "WHERE f_geometry_column = 'geom'"
            ).fetchall()
            out = []
            for r in rows:
                tbl = r["f_table_name"]
                if not SAFE_IDENT.match(tbl):
                    continue
                cnt = c.execute(f"SELECT COUNT(*) AS n FROM {tbl}").fetchone()["n"]
                out.append({"name": tbl, "count": cnt, "geometry_type": r["type"]})
            return out

    def _columns(self, conn, table: str) -> dict[str, str]:
        """Return {column_name: udt_name} in ordinal order."""
        rows = conn.execute(
            "SELECT column_name, udt_name "
            "FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = %s "
            "ORDER BY ordinal_position",
            (table,),
        ).fetchall()
        return {r["column_name"]: r["udt_name"] for r in rows}

    def _pk_needs_value(self, conn, table: str, pk: str | None) -> bool:
        """True when the primary key has no default, so inserts must allocate it themselves."""
        if pk is None:
            return False
        row = conn.execute(
            "SELECT column_default, is_identity FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = %s AND column_name = %s",
            (table, pk)).fetchone()
        return bool(row) and row["column_default"] is None and row["is_identity"] != "YES"

    def _geom_type(self, conn, table: str, geom_col: str) -> str | None:
        row = conn.execute(
            "SELECT type FROM geometry_columns WHERE f_table_schema = current_schema() "
            "AND f_table_name = %s AND f_geometry_column = %s", (table, geom_col)).fetchone()
        return row["type"] if row else None

    @staticmethod
    def _layout(cols: dict[str, str]) -> tuple[str, str | None, list[str]]:
        """(geometry column, primary-key column or None, attribute columns)."""
        geom_col = next((n for n, t in cols.items() if t == "geometry"), None)
        if geom_col is None:
            raise RuntimeError("no geometry column")
        pk = "id" if "id" in cols else None
        attrs = [n for n in cols if n not in (geom_col, pk, FID, "properties")]
        return geom_col, pk, attrs

    def _read_rows(self, conn, layer: str, cols: dict[str, str]) -> list[dict[str, Any]]:
        geom_col, _, _ = self._layout(cols)
        other = [n for n in cols if n != geom_col]
        quoted = ", ".join(q(n) for n in other)
        return conn.execute(
            f'SELECT {quoted}, ST_AsText({q(geom_col)}) AS _ofm_wkt FROM {layer}').fetchall()

    def load_layer(self, layer: str) -> dict[str, Any]:
        self._check(layer)
        with self._conn() as c:
            cols = self._columns(c, layer)
            if not cols:
                raise RuntimeError(f"table not found or empty schema: {layer}")
            _, pk, attrs = self._layout(cols)
            feats = []
            for r in self._read_rows(c, layer, cols):
                props = dict(r["properties"]) if isinstance(r.get("properties"), dict) else {}
                for col in attrs:
                    if r[col] is not None:
                        props.setdefault(col, r[col])
                fid = r.get(FID)
                feats.append({
                    "type": "Feature",
                    "id": str(fid) if fid is not None else f"{ROW_PREFIX}{r[pk]}" if pk else None,
                    "geometry": geometry_from_wkt(r["_ofm_wkt"]),
                    "properties": props,
                })
            return {"type": "FeatureCollection", "features": feats}

    def _value(self, udt: str, value: Any) -> Any:
        if udt in JSON_TYPES:
            return jsonb(value)
        if isinstance(value, (dict, list)):
            return json.dumps(value)
        return value

    def save_layer(self, layer: str, feature_collection: dict[str, Any],
                   editor: str | None = None) -> dict[str, Any]:
        self._check(layer)
        feats = feature_collection.get("features", [])
        with self._conn() as c:  # one transaction: all of the save, or none of it
            cols = self._columns(c, layer)
            if not cols:
                if not feats:
                    return {"layer": layer, "saved": 0, "inserted": 0, "updated": 0,
                            "deleted": 0, "unchanged": 0, "unstored_attributes": []}
                first = (feats[0].get("geometry") or {}).get("type", "Polygon")
                self._ensure_table(c, layer, GEOM_TYPE_MAP.get(first, "GEOMETRY"))
                cols = self._columns(c, layer)
            geom_col, pk, attrs = self._layout(cols)
            self._ensure_fid(c, layer, cols)
            cols = self._columns(c, layer)
            self._ensure_history(c)
            gtype = self._geom_type(c, layer, geom_col)
            fid_cast = "::uuid" if cols[FID] == "uuid" else ""
            alloc_pk = self._pk_needs_value(c, layer, pk)
            if alloc_pk and feats:  # serialise pk allocation with other writers
                c.execute(f"LOCK TABLE {layer} IN SHARE ROW EXCLUSIVE MODE")

            rows = self._read_rows(c, layer, cols)
            pk_to_fid = {f"{ROW_PREFIX}{r[pk]}": str(r[FID]) for r in rows} if pk else {}
            stored = [StoredRow(
                key=str(r[FID]),
                columns={a: r[a] for a in attrs},
                extras=dict(r["properties"]) if isinstance(r.get("properties"), dict) else {},
                geometry=geometry_from_wkt(r["_ofm_wkt"])) for r in rows]
            incoming = [{**f, "id": pk_to_fid.get(str(f.get("id")), f.get("id"))} for f in feats]
            plan = plan_save(stored, incoming, attrs, "properties" in cols)

            with c.cursor() as cur:
                for ch in plan.deletes:
                    cur.execute(f"DELETE FROM {layer} WHERE {FID}::text = %s", (ch.key,))
                next_pk = None
                if alloc_pk and plan.inserts:
                    next_pk = cur.execute(f"SELECT COALESCE(MAX({q(pk)}), 0) + 1 AS n FROM {layer}").fetchone()["n"]
                for ch in plan.inserts:
                    geom = coerce_geometry(ch.geometry, gtype)
                    names = list(ch.columns) + (["properties"] if ch.extras is not None else [])
                    values = [self._value(cols[n], ch.columns[n]) for n in ch.columns]
                    if ch.extras is not None:
                        values.append(self._value(cols["properties"], ch.extras))
                    if next_pk is not None:
                        names.insert(0, pk)
                        values.insert(0, next_pk)
                        next_pk += 1
                    col_list = ", ".join(q(n) for n in [FID, *names, geom_col])
                    ph = ", ".join([f"%s{fid_cast}", *(["%s"] * len(names)), "ST_GeomFromText(%s, 4326)"])
                    cur.execute(f"INSERT INTO {layer} ({col_list}) VALUES ({ph})",
                                (ch.fid, *values, wkt2d(geom)))
                for ch in plan.updates:
                    sets, values = [], []
                    for n, v in ch.columns.items():
                        sets.append(f"{q(n)} = %s")
                        values.append(self._value(cols[n], v))
                    if ch.extras is not None:
                        sets.append('"properties" = %s')
                        values.append(self._value(cols["properties"], ch.extras))
                    if ch.geometry is not None:
                        sets.append(f"{q(geom_col)} = ST_GeomFromText(%s, 4326)")
                        values.append(wkt2d(coerce_geometry(ch.geometry, gtype)))
                    cur.execute(f"UPDATE {layer} SET {', '.join(sets)} WHERE {FID}::text = %s",
                                (*values, ch.key))
                for op, changes in (("insert", plan.inserts), ("update", plan.updates),
                                    ("delete", plan.deletes)):
                    for ch in changes:
                        rec = history_record(layer, ch, op, editor)
                        cur.execute(
                            f"INSERT INTO {HISTORY_TABLE} (layer, fid, op, before, after, editor) "
                            "VALUES (%s, %s, %s, %s, %s, %s)",
                            (rec["layer"], rec["fid"], op, jsonb(rec["before"]) if rec["before"] else None,
                             jsonb(rec["after"]) if rec["after"] else None, editor))
        return plan.summary(layer)

    def delete_layer(self, layer: str) -> None:
        self._check(layer)
        with self._conn() as c:
            c.execute(f"DROP TABLE IF EXISTS {layer}")


__all__ = ["PostGISAdapter", "SaveRejected"]
