"""PostGIS storage adapter — column convention: `geom`, SRID 4326."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover
    psycopg = None  # type: ignore

from shapely import wkt as shapely_wkt
from shapely.geometry import mapping, shape

SAFE_IDENT = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")

GEOM_TYPE_MAP = {
    "Point": "POINT",
    "MultiPoint": "MULTIPOINT",
    "LineString": "LINESTRING",
    "MultiLineString": "MULTILINESTRING",
    "Polygon": "POLYGON",
    "MultiPolygon": "MULTIPOLYGON",
}


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

    def _ensure_table(self, conn, layer: str, geom_type: str) -> None:
        if not SAFE_IDENT.match(layer):
            raise ValueError(f"unsafe table name: {layer!r}")
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {layer} ("
            "id SERIAL PRIMARY KEY,"
            "name TEXT,"
            "class TEXT,"
            "properties JSONB,"
            f"geom geometry({geom_type}, 4326))"
        )
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS {layer}_geom_idx ON {layer} USING GIST (geom)"
        )

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

    def _columns(self, conn, table: str) -> list[tuple[str, str]]:
        """Return [(column_name, data_type), ...] in ordinal order."""
        rows = conn.execute(
            "SELECT column_name, udt_name "
            "FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = %s "
            "ORDER BY ordinal_position",
            (table,),
        ).fetchall()
        return [(r["column_name"], r["udt_name"]) for r in rows]

    def load_layer(self, layer: str) -> dict[str, Any]:
        if not SAFE_IDENT.match(layer):
            raise ValueError(f"unsafe table name: {layer!r}")
        with self._conn() as c:
            cols = self._columns(c, layer)
            if not cols:
                raise RuntimeError(f"table not found or empty schema: {layer}")
            # Find the geometry column (PostGIS uses udt_name = 'geometry').
            geom_col = next((n for n, t in cols if t == "geometry"), None)
            if geom_col is None:
                raise RuntimeError(f"no geometry column on {layer}")

            # Everything else becomes properties or special fields (id).
            other_cols = [n for n, _ in cols if n != geom_col]
            quoted = ", ".join(f'"{n}"' for n in other_cols)
            sql = (
                f'SELECT {quoted}, ST_AsText("{geom_col}") AS _ofm_wkt '
                f"FROM {layer}"
            )
            rows = c.execute(sql).fetchall()

            feats = []
            for r in rows:
                geom = shapely_wkt.loads(r["_ofm_wkt"]) if r["_ofm_wkt"] else None
                fid = r.get("id")
                # Merge a 'properties' JSONB column if it exists, else build
                # properties from every non-id, non-properties column.
                if "properties" in r and isinstance(r["properties"], dict):
                    props = dict(r["properties"])
                else:
                    props = {}
                for col in other_cols:
                    if col in ("id", "properties"):
                        continue
                    val = r[col]
                    if val is None:
                        continue
                    props.setdefault(col, val)
                feats.append(
                    {
                        "type": "Feature",
                        "id": fid,
                        "geometry": mapping(geom) if geom else None,
                        "properties": props,
                    }
                )
            return {"type": "FeatureCollection", "features": feats}

    def save_layer(self, layer: str, feature_collection: dict[str, Any]) -> int:
        feats = feature_collection.get("features", [])
        if not feats:
            with self._conn() as c:
                try:
                    c.execute(f"TRUNCATE {layer}")
                except Exception:
                    pass
            return 0
        first_geom = (feats[0].get("geometry") or {}).get("type", "POLYGON")
        gtype = GEOM_TYPE_MAP.get(first_geom, "GEOMETRY")
        with self._conn() as c:
            # Try to use the existing table shape; create one with the standard
            # shape if the table doesn't exist yet.
            existing = self._columns(c, layer)
            if not existing:
                self._ensure_table(c, layer, gtype)
                existing = self._columns(c, layer)
            col_set = {n for n, _ in existing}
            geom_col = next((n for n, t in existing if t == "geometry"), "geom")
            # Pick which scalar columns to write into — only those the table has.
            writable_scalars = [c_ for c_ in ("name", "class", "properties") if c_ in col_set]
            c.execute(f"TRUNCATE {layer}")
            with c.cursor() as cur:
                for f in feats:
                    geom = f.get("geometry")
                    if not geom:
                        continue
                    wkt_text = shape(geom).wkt
                    props = f.get("properties") or {}
                    values = []
                    for sc in writable_scalars:
                        if sc == "properties":
                            values.append(json.dumps(props))
                        else:
                            values.append(props.get(sc))
                    col_list = ", ".join(f'"{c_}"' for c_ in writable_scalars + [geom_col])
                    placeholders = ", ".join(["%s"] * len(writable_scalars) + [f"ST_GeomFromText(%s, 4326)"])
                    cur.execute(
                        f"INSERT INTO {layer} ({col_list}) VALUES ({placeholders})",
                        (*values, wkt_text),
                    )
        return len(feats)

    def delete_layer(self, layer: str) -> None:
        if not SAFE_IDENT.match(layer):
            raise ValueError(f"unsafe table name: {layer!r}")
        with self._conn() as c:
            c.execute(f"DROP TABLE IF EXISTS {layer}")
