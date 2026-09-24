"""SpatiaLite storage adapter — column convention: `the_geom`, SRID 4326."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

try:
    import spatialite  # type: ignore
except ImportError:  # pragma: no cover
    spatialite = None

from shapely.geometry import shape, mapping
from shapely import wkt as shapely_wkt

SAFE_TABLE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")

# Map GeoJSON geometry types → SpatiaLite column geometry types
GEOM_TYPE_MAP = {
    "Point": "POINT",
    "MultiPoint": "MULTIPOINT",
    "LineString": "LINESTRING",
    "MultiLineString": "MULTILINESTRING",
    "Polygon": "POLYGON",
    "MultiPolygon": "MULTIPOLYGON",
}


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
            f"CREATE TABLE {layer} ("
            "id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,"
            "name TEXT NULL,"
            "class TEXT NULL,"
            "properties TEXT NULL)"
        )
        self.conn.execute(
            f"SELECT AddGeometryColumn('{layer}', 'the_geom', 4326, '{geom_type}', 'XY', 1)"
        )
        self.conn.commit()

    def list_layers(self) -> list[dict[str, Any]]:
        cur = self.conn.execute(
            "SELECT f_table_name, geometry_type FROM geometry_columns"
        )
        rows = []
        for tbl, gtype in cur.fetchall():
            cnt = self.conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
            rows.append({"name": tbl, "count": cnt, "geometry_type": str(gtype)})
        return rows

    def _columns(self, table: str) -> list[str]:
        """Return ordered list of column names for the given table."""
        if not SAFE_TABLE.match(table):
            raise ValueError(f"unsafe table name: {table!r}")
        cur = self.conn.execute(f"PRAGMA table_info({table})")
        return [r[1] for r in cur.fetchall()]

    def _geom_column(self, table: str) -> str | None:
        cur = self.conn.execute(
            "SELECT f_geometry_column FROM geometry_columns WHERE f_table_name = ?",
            (table,),
        )
        row = cur.fetchone()
        return row[0] if row else None

    def load_layer(self, layer: str) -> dict[str, Any]:
        if not SAFE_TABLE.match(layer):
            raise ValueError(f"unsafe table name: {layer!r}")
        cols = self._columns(layer)
        if not cols:
            raise RuntimeError(f"table not found or empty schema: {layer}")
        geom_col = self._geom_column(layer) or "the_geom"
        other = [c for c in cols if c != geom_col]
        quoted = ", ".join(f'"{c}"' for c in other)
        sql = f'SELECT {quoted}, AsText("{geom_col}") FROM {layer}'
        cur = self.conn.execute(sql)
        features = []
        for row in cur.fetchall():
            row_dict = dict(zip(other + ["_ofm_wkt"], row))
            wkt = row_dict.pop("_ofm_wkt")
            geom = shapely_wkt.loads(wkt) if wkt else None
            fid = row_dict.pop("id", None)
            props_json = row_dict.pop("properties", None)
            props = json.loads(props_json) if isinstance(props_json, str) and props_json else {}
            for k, v in row_dict.items():
                if v is None:
                    continue
                props.setdefault(k, v)
            features.append(
                {
                    "type": "Feature",
                    "id": fid,
                    "geometry": mapping(geom) if geom else None,
                    "properties": props,
                }
            )
        return {"type": "FeatureCollection", "features": features}

    def save_layer(self, layer: str, feature_collection: dict[str, Any]) -> int:
        feats = feature_collection.get("features", [])
        if not feats:
            try:
                self.conn.execute(f"DELETE FROM {layer}")
                self.conn.commit()
            except Exception:
                pass
            return 0
        first = feats[0]
        gtype = (first.get("geometry") or {}).get("type", "POLYGON")
        sl_type = GEOM_TYPE_MAP.get(gtype, "GEOMETRY")
        # Use existing table shape if present; otherwise create the standard one.
        existing = self._columns(layer)
        if not existing:
            self._ensure_table(layer, sl_type)
            existing = self._columns(layer)
        geom_col = self._geom_column(layer) or "the_geom"
        col_set = set(existing)
        writable_scalars = [c for c in ("name", "class", "properties") if c in col_set]
        self.conn.execute(f"DELETE FROM {layer}")
        for f in feats:
            geom = f.get("geometry")
            if not geom:
                continue
            wkt_text = shape(geom).wkt
            props = f.get("properties") or {}
            values = []
            for sc in writable_scalars:
                values.append(json.dumps(props) if sc == "properties" else props.get(sc))
            col_list = ", ".join(f'"{c}"' for c in writable_scalars + [geom_col])
            placeholders = ", ".join(["?"] * len(writable_scalars) + ["GeomFromText(?, 4326)"])
            self.conn.execute(
                f"INSERT INTO {layer} ({col_list}) VALUES ({placeholders})",
                (*values, wkt_text),
            )
        self.conn.commit()
        return len(feats)

    def delete_layer(self, layer: str) -> None:
        if not SAFE_TABLE.match(layer):
            raise ValueError(f"unsafe table name: {layer!r}")
        self.conn.execute(f"SELECT DiscardGeometryColumn('{layer}', 'the_geom')")
        self.conn.execute(f"DROP TABLE IF EXISTS {layer}")
        self.conn.commit()
