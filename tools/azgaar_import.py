#!/usr/bin/env python3
"""Import Azgaar Fantasy Map Generator GeoJSON exports into an OFM world.

Detects up to 5 layers (cells, routes, rivers, markers, zones, burgs) by
case-insensitive substring on filename — Azgaar's exports are typically
named like "<World> <Layer> <YYYY-MM-DD-HH-MM>.geojson", so we don't
require exact filenames.

Output: a complete world dir <OFM_ROOT>/<slug>/ containing
  timeline.json, gaia.json, map.json, render.json,
  raw/geojson/<layer>.geojson  (one per input),
  raw/schemas/<layer>.schema.json  (typed property schemas).

Usage (via docker):
  docker run --rm -v /srv/ofm:/ofm \\
      -v /srv/ofm/raster-vectorizer/tools:/tools \\
      python:3.12-slim python3 /tools/azgaar_import.py \\
      --in /ofm/jarciland/to_import --slug jarciland --name "Jarciland"

Idempotent overwrite: by default, will refuse to clobber an existing
populated raw/geojson; pass --force to overwrite.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Optional

OFM_ROOT = Path(os.environ.get("OFM_ROOT", "/ofm"))

# Azgaar's canonical biome palette — IDs match the FMG source.
BIOMES = [
    (0,  "Marine",                     "#466eab"),
    (1,  "Hot desert",                 "#fbe79f"),
    (2,  "Cold desert",                "#b5b887"),
    (3,  "Savanna",                    "#d2d082"),
    (4,  "Grassland",                  "#c8d68f"),
    (5,  "Tropical seasonal forest",   "#b6d95d"),
    (6,  "Temperate deciduous forest", "#29bc56"),
    (7,  "Tropical rainforest",        "#7dcb35"),
    (8,  "Temperate rainforest",       "#409c43"),
    (9,  "Taiga",                      "#4b6b32"),
    (10, "Tundra",                     "#96784b"),
    (11, "Glacier",                    "#d5e7eb"),
    (12, "Wetland",                    "#0b9131"),
]
BIOME_NAME = {b[0]: b[1] for b in BIOMES}
BIOME_COLOR = {b[0]: b[2] for b in BIOMES}

# Layers we recognise (filename substring → canonical layer name).
LAYER_KEYS = ["cells", "routes", "rivers", "markers", "zones", "burgs"]


def detect_inputs(in_dir: Path) -> dict[str, Path]:
    """Map canonical layer name → file path, by case-insensitive substring."""
    found: dict[str, Path] = {}
    for f in sorted(in_dir.iterdir()):
        if not f.is_file() or f.suffix.lower() not in (".geojson", ".json"):
            continue
        name_l = f.name.lower()
        for key in LAYER_KEYS:
            if key in name_l and key not in found:
                found[key] = f
                break
    return found


def bbox_of(fc: dict) -> Optional[list[float]]:
    mnx, mny, mxx, mxy = float("inf"), float("inf"), float("-inf"), float("-inf")

    def walk(coords):
        nonlocal mnx, mny, mxx, mxy
        if not coords:
            return
        if isinstance(coords[0], (int, float)):
            x, y = coords[0], coords[1]
            mnx, mny = min(mnx, x), min(mny, y)
            mxx, mxy = max(mxx, x), max(mxy, y)
        else:
            for c in coords:
                walk(c)

    for feat in fc.get("features", []):
        g = feat.get("geometry")
        if g and g.get("coordinates"):
            walk(g["coordinates"])
    if mnx == float("inf"):
        return None
    return [mnx, mny, mxx, mxy]


def collect_property_values(fc: dict, key: str) -> set:
    out = set()
    for f in fc.get("features", []):
        v = (f.get("properties") or {}).get(key)
        if v is not None:
            out.add(v)
    return out


def compute_view(bbox: list[float]) -> tuple[float, float, int]:
    cx = (bbox[0] + bbox[2]) / 2
    cy = (bbox[1] + bbox[3]) / 2
    span = max(bbox[2] - bbox[0], bbox[3] - bbox[1]) or 1
    # Web Mercator zoom heuristic — 360° span → z0, halves per zoom
    zoom = max(0, min(12, int(math.floor(math.log2(360 / span)))))
    return cx, cy, zoom


# --- timeline + gaia + render -----------------------------------------------

def build_timeline(slug: str, name: str, date: int, bbox, cx, cy, zoom) -> dict:
    return {
        "size": 3,
        "name": name,
        "url": f"/{slug}",
        "date": date,
        "mode": "geojson",
        "connection": {"db": slug},
        "base": {"zoom": zoom, "lat": round(cy, 6), "lng": round(cx, 6)},
        "tags": ["azgaar", "fantasy", "rpg"],
        "copyright": f"Azgaar's Fantasy Map Generator export — '{name}' is the user's creation",
        "description": (
            f"{name} — imported from Azgaar's Fantasy Map Generator. "
            f"Bounds {bbox[0]:.2f},{bbox[1]:.2f} → {bbox[2]:.2f},{bbox[3]:.2f}."
        ),
        "sources": [{
            "name": slug,
            "label": f"{name} Map",
            "attribution": "Azgaar's Fantasy Map Generator (https://azgaar.github.io/Fantasy-Map-Generator/)",
            "copyright": "Azgaar FMG export; world is the user's creation",
            "basepath": f"/data/{slug}/raw",
        }],
        "wikis": {
            "azgaar": {
                "slug": "azgaar",
                "name": "Azgaar's FMG Wiki",
                "world": slug,
                "description": "Documentation for Azgaar's Fantasy Map Generator features",
                "type": "wiki",
                "base": "https://github.com/Azgaar/Fantasy-Map-Generator/wiki",
            },
        },
    }


def build_gaia(name: str, layers: dict) -> dict:
    biomes_present = sorted(collect_property_values(layers.get("cells", {}), "biome"))
    terrain = [BIOME_NAME[b].lower().replace(" ", "_") for b in biomes_present if b in BIOME_NAME]
    return {
        "version": "1.0",
        "system": [
            f"You are on {name}, a fantasy world.",
            "You do not speak of map generators, games, or 'lore' — you live here.",
            "You are a grounded mortal observer at ground level.",
            "You describe places the way someone would when looking around: weather, smells, the cant of a roof, the feel of the soil.",
            "You never mention maps, coordinates, GeoJSON, geometry, layers, or data.",
            "You avoid modern, technical, or anachronistic language.",
        ],
        "user": [],
        "image": [
            f"you are on the world of {name}",
            "a hand-drawn fantasy world with realms, cities, rivers, and mountains",
        ],
        "inspect_world": f"{name}, a fantasy world of states, cultures, and biomes.",
        "sources":   sorted(layers.keys()),
        "indirect":  [],
        "weather":   ["rain", "snow", "storm", "fog"],
        "terrain":   terrain or ["plains", "forest", "mountain"],
        "agents":    [],
        "inspection_presets": [
            "Local commoner", "Local noble", "Wanderer", "Scholar",
            "Merchant", "Soldier", "Priest", "Outlaw",
        ],
    }


def build_render() -> list:
    return [
        {"asset": "Grass_D_01.jpg", "type": "texture", "weight": -100},
        {"condition": ["water"],   "asset": "Water_01.jpg", "type": "texture", "weight": -90, "blur": 5},
        {"condition": ["forest"],  "asset": "Forest_01.jpg","type": "texture", "weight": -50, "blur": 5},
        {"condition": ["lights"],  "asset": "Lantern_01.png", "type": "point", "weight": 100},
    ]


# --- map.json (the heavyweight one) ----------------------------------------

def build_mapjson(slug: str, name: str, layers: dict) -> dict:
    BASE = "https://statictiles.fantasymaps.org"
    sources: dict = {}
    layers_list: list = [
        {"id": "background", "type": "background",
         "paint": {"background-color": BIOME_COLOR[0]}},  # ocean blue
    ]
    togglable: list = []

    # ---- cells: biome categorical fill + faint outline ---------------------
    if "cells" in layers:
        sources["cells"] = {"type": "geojson", "data": f"{BASE}/{slug}/cells.geojson"}
        biome_match = ["match", ["coalesce", ["to-number", ["get", "biome"]], -1]]
        for bid, _, color in BIOMES:
            biome_match.extend([bid, color])
        biome_match.append("#888888")
        layers_list.append({
            "id": "cells-fill", "type": "fill", "source": "cells",
            "paint": {"fill-color": biome_match, "fill-opacity": 0.9},
        })
        layers_list.append({
            "id": "cells-outline", "type": "line", "source": "cells",
            "minzoom": 5,
            "paint": {"line-color": "#1a1a1a", "line-width": 0.25, "line-opacity": 0.3},
        })
        present_biomes = collect_property_values(layers["cells"], "biome")
        legend = [[BIOME_NAME[b], BIOME_COLOR[b]] for b in sorted(present_biomes) if b in BIOME_NAME]
        togglable.append({
            "label": "Biomes (cells)", "name": "cells",
            "layers": ["cells-fill", "cells-outline"],
            "legend": legend[:10],
        })

        # ---- alternate political view: cells colored by state (hidden by default) ----
        states = sorted(s for s in collect_property_values(layers["cells"], "state")
                        if isinstance(s, (int, float)))
        if len(states) > 1:
            # Deterministic per-state HSL → hex
            def state_color(i):
                h = (i * 37) % 360
                # HSL to hex (S=55%, L=58%)
                h_norm = h / 360.0
                r, g, b = _hsl_to_rgb(h_norm, 0.55, 0.58)
                return "#{:02x}{:02x}{:02x}".format(int(r * 255), int(g * 255), int(b * 255))
            politics_match = ["match", ["coalesce", ["to-number", ["get", "state"]], -1]]
            for s in states:
                politics_match.extend([s, state_color(int(s))])
            politics_match.append("#888888")
            layers_list.append({
                "id": "cells-politics", "type": "fill", "source": "cells",
                "layout": {"visibility": "none"},
                "paint": {"fill-color": politics_match, "fill-opacity": 0.6},
            })
            togglable.append({
                "label": "States (politics)", "name": "politics",
                "layers": ["cells-politics"],
            })

    # ---- rivers: width by widthFactor (Azgaar's hydrology hint) ------------
    if "rivers" in layers:
        sources["rivers"] = {"type": "geojson", "data": f"{BASE}/{slug}/rivers.geojson"}
        # widthFactor is Azgaar's per-river scalar; falls back to discharge ratio
        width_expr = [
            "interpolate", ["linear"],
            ["coalesce",
             ["to-number", ["get", "widthFactor"]],
             ["/", ["to-number", ["get", "discharge"]], 100.0],
             1],
            0, 0.5,
            5, 1.5,
            20, 3,
            80, 6,
        ]
        layers_list.append({
            "id": "rivers-line", "type": "line", "source": "rivers",
            "paint": {
                "line-color": "#3a78a8",
                "line-width": width_expr,
                "line-opacity": 0.9,
            },
        })
        togglable.append({"label": "Rivers", "name": "rivers", "layers": ["rivers-line"]})

    # ---- routes: colored by group (roads/trails/searoutes) -----------------
    if "routes" in layers:
        sources["routes"] = {"type": "geojson", "data": f"{BASE}/{slug}/routes.geojson"}
        route_color = ["match", ["get", "group"],
                       "roads",     "#5a4a3a",
                       "trails",    "#a89a72",
                       "searoutes", "#6b8db5",
                       "#666666"]
        route_dash = ["match", ["get", "group"],
                      "trails", ["literal", [2, 3]],
                      ["literal", [1]]]
        layers_list.append({
            "id": "routes-line", "type": "line", "source": "routes",
            "paint": {
                "line-color": route_color,
                "line-width": ["match", ["get", "group"], "roads", 1.4, "trails", 0.9, 1.0],
                "line-opacity": 0.85,
                "line-dasharray": route_dash,
            },
        })
        togglable.append({"label": "Routes", "name": "routes", "layers": ["routes-line"]})

    # ---- zones: translucent overlay (special regions: cursed lands, etc.) --
    if "zones" in layers:
        sources["zones"] = {"type": "geojson", "data": f"{BASE}/{slug}/zones.geojson"}
        layers_list.append({
            "id": "zones-fill", "type": "fill", "source": "zones",
            "paint": {
                "fill-color": ["coalesce", ["get", "color"], "#aa66cc"],
                "fill-opacity": 0.3,
            },
        })
        layers_list.append({
            "id": "zones-outline", "type": "line", "source": "zones",
            "paint": {"line-color": ["coalesce", ["get", "color"], "#aa66cc"],
                      "line-width": 1.2, "line-dasharray": ["literal", [3, 2]]},
        })
        togglable.append({"label": "Zones", "name": "zones",
                          "layers": ["zones-fill", "zones-outline"]})

    # ---- burgs (if present): circle by size + label by name ---------------
    if "burgs" in layers:
        sources["burgs"] = {"type": "geojson", "data": f"{BASE}/{slug}/burgs.geojson"}
        layers_list.append({
            "id": "burgs-circle", "type": "circle", "source": "burgs",
            "paint": {
                "circle-radius": ["interpolate", ["linear"],
                                  ["coalesce", ["to-number", ["get", "population"]], 1],
                                  1, 2, 10, 4, 100, 7, 1000, 10],
                "circle-color": "#fff8e1",
                "circle-stroke-color": "#1a1a1a",
                "circle-stroke-width": 1,
            },
        })
        layers_list.append({
            "id": "burgs-label", "type": "symbol", "source": "burgs",
            "minzoom": 4,
            "layout": {
                "text-field": ["coalesce", ["get", "name"], ""],
                "text-size": 11, "text-offset": [0, 0.9], "text-anchor": "top",
            },
            "paint": {
                "text-color": "#1a1a1a",
                "text-halo-color": "#ffffff", "text-halo-width": 1.4,
            },
        })
        togglable.append({"label": "Settlements", "name": "burgs",
                          "layers": ["burgs-circle", "burgs-label"]})

    # ---- markers: emoji-as-text (Azgaar's `icon` is a literal emoji char) --
    if "markers" in layers:
        sources["markers"] = {"type": "geojson", "data": f"{BASE}/{slug}/markers.geojson"}
        layers_list.append({
            "id": "markers-symbol", "type": "symbol", "source": "markers",
            "minzoom": 3,
            "layout": {
                "text-field": ["coalesce", ["get", "icon"], ["get", "name"], "•"],
                "text-size": 16,
                "text-allow-overlap": True,
            },
        })
        layers_list.append({
            "id": "markers-label", "type": "symbol", "source": "markers",
            "minzoom": 5,
            "layout": {
                "text-field": ["coalesce", ["get", "name"], ""],
                "text-size": 10, "text-offset": [0, 1.4], "text-anchor": "top",
            },
            "paint": {
                "text-color": "#1a1a1a",
                "text-halo-color": "#ffffff", "text-halo-width": 1.2,
            },
        })
        togglable.append({"label": "Markers", "name": "markers",
                          "layers": ["markers-symbol", "markers-label"]})

    return {
        "version": 8,
        "name": name,
        "metadata": {
            "maputnik:renderer": "mbgljs",
            "ofm": {
                "parentMap": None,
                "parentLocation": [0, 0],
                "type": "map",
                "copyright": f"Azgaar's Fantasy Map Generator export — '{name}' is the user's creation",
                "description": f"{name} — imported via OFM's Azgaar GeoJSON importer.",
                "sources": [{
                    "name": slug,
                    "label": f"{name} Map",
                    "attribution": "Azgaar's Fantasy Map Generator",
                }],
                "togglable": togglable,
            },
        },
        "sources": sources,
        "sprite": "",
        "glyphs": f"{BASE}/fonts/{{fontstack}}/{{range}}.pbf",
        "layers": layers_list,
    }


def _hsl_to_rgb(h: float, s: float, l: float) -> tuple[float, float, float]:
    def hue2rgb(p, q, t):
        if t < 0: t += 1
        if t > 1: t -= 1
        if t < 1/6: return p + (q - p) * 6 * t
        if t < 1/2: return q
        if t < 2/3: return p + (q - p) * (2/3 - t) * 6
        return p
    if s == 0:
        return l, l, l
    q = l * (1 + s) if l < 0.5 else l + s - l * s
    p = 2 * l - q
    return hue2rgb(p, q, h + 1/3), hue2rgb(p, q, h), hue2rgb(p, q, h - 1/3)


# --- layer schemas ----------------------------------------------------------

def build_schemas(layers: dict) -> dict[str, dict]:
    out: dict[str, dict] = {}
    if "cells" in layers:
        out["cells"] = {
            "type": "object", "geometryType": "Polygon",
            "properties": {
                "id":         {"type": "integer", "description": "Azgaar cell id"},
                "biome":      {"type": "integer", "enum": [b[0] for b in BIOMES],
                               "description": "Biome ID (Azgaar palette: 0=Marine, 1=Hot desert, ... 12=Wetland)"},
                "state":      {"type": "integer", "description": "State / realm id"},
                "culture":    {"type": "integer", "description": "Culture id"},
                "religion":   {"type": "integer", "description": "Religion id"},
                "province":   {"type": "integer", "description": "Province id"},
                "population": {"type": "number",  "description": "Population (Azgaar's scale)"},
                "height":     {"type": "number",  "description": "Elevation (signed; <0 is ocean)"},
                "type":       {"type": "string",  "description": "Cell type: 'ocean', 'land', etc."},
            },
        }
    if "routes" in layers:
        groups = sorted(set(filter(None, collect_property_values(layers["routes"], "group"))))
        out["routes"] = {
            "type": "object", "geometryType": "LineString",
            "properties": {
                "id":    {"type": "integer"},
                "group": {"type": "string", "enum": groups or ["roads", "trails", "searoutes"]},
                "name":  {"type": "string"},
            },
        }
    if "rivers" in layers:
        out["rivers"] = {
            "type": "object", "geometryType": "LineString",
            "properties": {
                "id":           {"type": "integer"},
                "name":         {"type": "string"},
                "basin":        {"type": "integer"},
                "parent":       {"type": "integer"},
                "source":       {"type": "integer"},
                "mouth":        {"type": "integer"},
                "type":         {"type": "string"},
                "discharge":    {"type": "number"},
                "sourceWidth":  {"type": "number"},
                "widthFactor":  {"type": "number"},
            },
        }
    if "markers" in layers:
        types = sorted(set(filter(None, collect_property_values(layers["markers"], "type"))))
        out["markers"] = {
            "type": "object", "geometryType": "Point",
            "properties": {
                "id":     {"type": "string"},
                "type":   {"type": "string", "enum": types[:50] if types else []},
                "icon":   {"type": "string", "description": "Emoji icon (used for display)"},
                "name":   {"type": "string"},
                "legend": {"type": "string", "description": "Long-form description"},
            },
        }
    if "zones" in layers:
        out["zones"] = {
            "type": "object", "geometryType": "Polygon",
            "properties": {
                "id":          {"type": "integer"},
                "name":        {"type": "string"},
                "type":        {"type": "string"},
                "color":       {"type": "string", "description": "CSS color"},
                "description": {"type": "string"},
            },
        }
    if "burgs" in layers:
        out["burgs"] = {
            "type": "object", "geometryType": "Point",
            "properties": {
                "id":         {"type": "integer"},
                "name":       {"type": "string"},
                "population": {"type": "number"},
                "state":      {"type": "integer"},
                "culture":    {"type": "integer"},
                "religion":   {"type": "integer"},
                "type":       {"type": "string"},
            },
        }
    return out


# --- driver ----------------------------------------------------------------

def import_world(in_dir: Path, slug: str, name: Optional[str], date: int, force: bool) -> Path:
    inputs = detect_inputs(in_dir)
    if not inputs:
        sys.exit(f"no recognised Azgaar GeoJSONs in {in_dir}")
    if "cells" not in inputs:
        sys.exit(f"missing cells.geojson (or similar) in {in_dir}; got: {list(inputs.keys())}")

    world_dir = OFM_ROOT / slug
    raw_geojson = world_dir / "raw" / "geojson"
    raw_schemas = world_dir / "raw" / "schemas"
    if raw_geojson.exists() and any(raw_geojson.iterdir()) and not force:
        sys.exit(f"{raw_geojson} is non-empty; pass --force to overwrite")
    raw_geojson.mkdir(parents=True, exist_ok=True)
    raw_schemas.mkdir(parents=True, exist_ok=True)

    layers_data: dict[str, dict] = {}
    overall_bbox: Optional[list[float]] = None
    for key, src in inputs.items():
        dst = raw_geojson / f"{key}.geojson"
        shutil.copy2(src, dst)
        fc = json.loads(dst.read_text(encoding="utf-8"))
        layers_data[key] = fc
        b = bbox_of(fc)
        if b:
            overall_bbox = b if overall_bbox is None else [
                min(overall_bbox[0], b[0]),
                min(overall_bbox[1], b[1]),
                max(overall_bbox[2], b[2]),
                max(overall_bbox[3], b[3]),
            ]

    if overall_bbox is None:
        sys.exit("no usable geometries in input — could not compute bbox")

    cx, cy, zoom = compute_view(overall_bbox)
    name = name or _humanise(slug)

    (world_dir / "timeline.json").write_text(
        json.dumps(build_timeline(slug, name, date, overall_bbox, cx, cy, zoom),
                   indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (world_dir / "gaia.json").write_text(
        json.dumps(build_gaia(name, layers_data), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8")
    (world_dir / "render.json").write_text(
        json.dumps(build_render(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8")
    (world_dir / "map.json").write_text(
        json.dumps(build_mapjson(slug, name, layers_data), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8")

    for layer_name, schema in build_schemas(layers_data).items():
        (raw_schemas / f"{layer_name}.schema.json").write_text(
            json.dumps(schema, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(f"imported {len(layers_data)} layer(s) into {world_dir}:")
    for k, fc in layers_data.items():
        print(f"  - {k}: {len(fc.get('features', []))} features")
    print(f"  bbox: {overall_bbox}")
    print(f"  view: lat={cy:.3f} lng={cx:.3f} zoom={zoom}")
    return world_dir


def _humanise(slug: str) -> str:
    return re.sub(r"[-_]+", " ", slug).strip().title()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="in_dir", required=True,
                    help="Directory containing Azgaar GeoJSONs")
    ap.add_argument("--slug", required=True,
                    help="OFM world slug — output will be <OFM_ROOT>/<slug>/")
    ap.add_argument("--name", help="Display name (default: title-cased slug)")
    ap.add_argument("--date", type=int, default=1000, help="In-world year")
    ap.add_argument("--force", action="store_true",
                    help="Overwrite existing raw/geojson/ contents")
    args = ap.parse_args()
    in_dir = Path(args.in_dir)
    if not in_dir.is_dir():
        sys.exit(f"input dir not found: {in_dir}")
    import_world(in_dir, args.slug, args.name, args.date, args.force)
