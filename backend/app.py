"""raster-vectorizer backend — FastAPI service.

Routes (all under /api):
  GET    /worlds                              list discovered worlds
  GET    /worlds/{slug}                       full world descriptor (timeline+gaia+map)
  GET    /worlds/{slug}/style                 the world's map.json (Mapbox-GL style)
  GET    /worlds/{slug}/layers                list layers known to the storage backend
  GET    /worlds/{slug}/layers/{layer}        load a layer as FeatureCollection
  PUT    /worlds/{slug}/layers/{layer}        save a layer (non-destructive diff, see storage/diff.py)
  DELETE /worlds/{slug}/layers/{layer}        drop the layer
  GET    /worlds/{slug}/tiles/{z}/{x}/{y}.jpg proxy a local raster tile
  GET    /health                              liveness
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import json as _json

from fastapi import Depends, FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse


_SURROGATE_RX = __import__("re").compile(r"[\ud800-\udfff]")


def _scrub_surrogates(s: str) -> str:
    # Replace lone surrogate halves with U+FFFD. Some real-world inputs
    # (Azgaar FMG exports of "ancient inscription" markers) contain unpaired
    # surrogates that crash utf-8 encoding. We can't keep them; substitute
    # the replacement char so the rest of the payload survives.
    return _SURROGATE_RX.sub("�", s)


class UnicodeJSONResponse(JSONResponse):
    """JSONResponse that preserves non-ASCII (emoji, etc.) end-to-end.

    Pydantic-core's default JSON encoder throws on certain non-BMP code
    points and on lone surrogates. We bypass it by using stdlib json
    (ensure_ascii=False) and pre-scrubbing lone surrogate halves.
    """

    def render(self, content) -> bytes:
        text = _json.dumps(
            content, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        )
        if any("\ud800" <= c <= "\udfff" for c in text):
            text = _scrub_surrogates(text)
        return text.encode("utf-8")

from . import layer_schemas as layer_schemas_mod
from . import manifest as manifest_mod
from . import raster as raster_mod
from . import worlds as world_mod
from .auth import require_user
from .storage import for_world
from .storage.diff import SaveRejected


def _adapter_or_503(wdir):
    """Open the storage adapter for a world; convert connection errors to 503."""
    try:
        return for_world(wdir)
    except Exception as e:
        raise HTTPException(
            status_code=503,
            detail=f"storage backend unavailable for {wdir.name}: {e.__class__.__name__}: {e}",
        )


def _call_or_503(wdir, method_name: str, *args, **kwargs):
    """Open adapter + invoke a method; any adapter error → 503 with detail.

    Without this wrapper, an uncaught adapter exception bubbles up to FastAPI's
    error middleware which returns a response without CORS headers, which the
    browser surfaces as 'Failed to fetch' instead of the actual error.
    """
    adapter = _adapter_or_503(wdir)
    try:
        return getattr(adapter, method_name)(*args, **kwargs)
    except HTTPException:
        raise
    except SaveRejected as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=503,
            detail=f"{adapter.mode} adapter failed on {method_name}: {e.__class__.__name__}: {e}",
        )

app = FastAPI(
    title="OFM raster-vectorizer editor",
    version="0.1.0",
    default_response_class=UnicodeJSONResponse,
)

# CORS — frontend is Netlify-hosted; comma-separated origins via env.
_cors_origins = [
    o.strip()
    for o in os.environ.get("OFM_CORS_ORIGINS", "*").split(",")
    if o.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_methods=["GET", "PUT", "POST", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    allow_credentials=True,
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "ofm_root": str(world_mod.OFM_ROOT)}


@app.get("/api/worlds", dependencies=[Depends(require_user)])
def get_worlds() -> list[dict[str, Any]]:
    return world_mod.list_worlds()


@app.get("/api/worlds/{slug}", dependencies=[Depends(require_user)])
def get_world(slug: str) -> dict[str, Any]:
    try:
        world_mod.world_dir(slug)
    except FileNotFoundError:
        raise HTTPException(404, f"unknown world: {slug}")
    return world_mod.get_world(slug)


@app.get("/api/worlds/{slug}/style", dependencies=[Depends(require_user)])
def get_world_style(slug: str) -> JSONResponse:
    try:
        wdir = world_mod.world_dir(slug)
    except FileNotFoundError:
        raise HTTPException(404, f"unknown world: {slug}")
    mp = wdir / "map.json"
    if not mp.is_file():
        raise HTTPException(404, "no map.json for this world")
    return JSONResponse(content=__import__("json").loads(mp.read_text("utf-8")))


@app.get("/api/worlds/{slug}/layers", dependencies=[Depends(require_user)])
def get_layers(slug: str) -> list[dict[str, Any]]:
    try:
        wdir = world_mod.world_dir(slug)
    except FileNotFoundError:
        raise HTTPException(404, f"unknown world: {slug}")
    return _call_or_503(wdir, "list_layers")


@app.get("/api/worlds/{slug}/layers/{layer}", dependencies=[Depends(require_user)])
def get_layer(slug: str, layer: str) -> Response:
    # Return a Response (not dict) so pydantic doesn't try to serialise the
    # FeatureCollection — pydantic-core throws on certain emoji (non-BMP), and
    # OFM worlds in the wild contain them (Azgaar marker icons, place names).
    try:
        wdir = world_mod.world_dir(slug)
    except FileNotFoundError:
        raise HTTPException(404, f"unknown world: {slug}")
    fc = _call_or_503(wdir, "load_layer", layer)
    return UnicodeJSONResponse(content=fc)


@app.put("/api/worlds/{slug}/layers/{layer}")
def put_layer(slug: str, layer: str, body: dict[str, Any],
              user: str = Depends(require_user)) -> dict[str, Any]:
    """Save a layer. Features are matched by id: changed ones are updated, new ones inserted,
    missing ones deleted; every stored column is kept. Returns what changed, including any
    attributes the layer has no place for (`unstored_attributes`)."""
    try:
        wdir = world_mod.world_dir(slug)
    except FileNotFoundError:
        raise HTTPException(404, f"unknown world: {slug}")
    if body.get("type") != "FeatureCollection":
        raise HTTPException(400, "body must be a GeoJSON FeatureCollection")
    summary = _call_or_503(wdir, "save_layer", layer, body, editor=user)
    return {**summary, "world": slug}


# --- per-layer JSON Schema -------------------------------------------------

@app.get(
    "/api/worlds/{slug}/layers/{layer}/schema",
    dependencies=[Depends(require_user)],
)
def get_layer_schema(slug: str, layer: str) -> dict[str, Any]:
    try:
        wdir = world_mod.world_dir(slug)
    except FileNotFoundError:
        raise HTTPException(404, f"unknown world: {slug}")
    try:
        return layer_schemas_mod.get(wdir, layer)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.put(
    "/api/worlds/{slug}/layers/{layer}/schema",
    dependencies=[Depends(require_user)],
)
def put_layer_schema(slug: str, layer: str, body: dict[str, Any]) -> dict[str, Any]:
    try:
        wdir = world_mod.world_dir(slug)
    except FileNotFoundError:
        raise HTTPException(404, f"unknown world: {slug}")
    try:
        return layer_schemas_mod.put(wdir, layer, body)
    except ValueError as e:
        detail = e.args[0] if e.args else {"validation_errors": [str(e)]}
        if isinstance(detail, dict):
            raise HTTPException(422, detail=detail)
        raise HTTPException(400, str(detail))


@app.delete("/api/worlds/{slug}/layers/{layer}", dependencies=[Depends(require_user)])
def del_layer(slug: str, layer: str) -> dict[str, Any]:
    try:
        wdir = world_mod.world_dir(slug)
    except FileNotFoundError:
        raise HTTPException(404, f"unknown world: {slug}")
    _call_or_503(wdir, "delete_layer", layer)
    return {"deleted": layer, "world": slug}


# --- manifest editor (timeline/gaia/map/render JSON files) -----------------

@app.get("/api/schemas", dependencies=[Depends(require_user)])
def get_schema_kinds() -> dict[str, list[str]]:
    return {"kinds": manifest_mod.list_kinds()}


@app.get("/api/schemas/{kind}", dependencies=[Depends(require_user)])
def get_schema(kind: str) -> dict[str, Any]:
    try:
        return manifest_mod.get_schema(kind)
    except KeyError:
        raise HTTPException(404, f"unknown manifest kind: {kind}")


@app.get(
    "/api/worlds/{slug}/manifest/{kind}",
    dependencies=[Depends(require_user)],
)
def get_manifest(slug: str, kind: str) -> JSONResponse:
    try:
        wdir = world_mod.world_dir(slug)
    except FileNotFoundError:
        raise HTTPException(404, f"unknown world: {slug}")
    try:
        body = manifest_mod.load(wdir, kind)
    except KeyError:
        raise HTTPException(404, f"unknown manifest kind: {kind}")
    return JSONResponse(content={"kind": kind, "world": slug, "content": body})


@app.put(
    "/api/worlds/{slug}/manifest/{kind}",
    dependencies=[Depends(require_user)],
)
def put_manifest(slug: str, kind: str, body: dict[str, Any]) -> dict[str, Any]:
    try:
        wdir = world_mod.world_dir(slug)
    except FileNotFoundError:
        raise HTTPException(404, f"unknown world: {slug}")
    if "content" not in body:
        raise HTTPException(400, "request body must be {content: ...}")
    try:
        return manifest_mod.save(wdir, kind, body["content"])
    except KeyError:
        raise HTTPException(404, f"unknown manifest kind: {kind}")
    except ValueError as e:
        # Schema validation failure — surface the per-path errors so the
        # frontend can highlight them.
        detail = e.args[0] if e.args else {"validation_errors": [str(e)]}
        raise HTTPException(422, detail=detail)


# --- raster sources (pyramids + GeoTIFFs) ----------------------------------

@app.get(
    "/api/worlds/{slug}/rasters",
    dependencies=[Depends(require_user)],
)
def list_rasters(slug: str) -> list[dict[str, Any]]:
    try:
        wdir = world_mod.world_dir(slug)
    except FileNotFoundError:
        raise HTTPException(404, f"unknown world: {slug}")
    return raster_mod.discover(wdir)


@app.get(
    "/api/worlds/{slug}/rasters/{source}/tiles/{z}/{x}/{filename}",
    dependencies=[Depends(require_user)],
)
def get_raster_tile(
    slug: str, source: str, z: int, x: int, filename: str
) -> Response:
    try:
        y_part, ext = filename.rsplit(".", 1)
    except ValueError:
        raise HTTPException(400, "expected {y}.{ext}")
    ext = ext.lower()
    if ext not in ("jpg", "jpeg", "png", "webp"):
        raise HTTPException(400, "unsupported tile extension")
    try:
        wdir = world_mod.world_dir(slug)
    except FileNotFoundError:
        raise HTTPException(404, f"unknown world: {slug}")

    src = raster_mod.find_source(wdir, source)
    if not src:
        raise HTTPException(404, f"unknown raster source: {source}")

    try:
        y = int(y_part)
    except ValueError:
        raise HTTPException(400, "y must be an integer")

    headers = {"Cache-Control": "public, max-age=3600"}

    if src["kind"] == "pyramid":
        # Try the source's native ext first, fall back to the requested ext.
        native_ext = src.get("ext", ext)
        body = raster_mod.read_pyramid_tile(
            wdir, z=z, x=x, y=y, ext=native_ext, path=src["path"],
        )
        if body is None and ext != native_ext:
            body = raster_mod.read_pyramid_tile(
                wdir, z=z, x=x, y=y, ext=ext, path=src["path"],
            )
        if body is None:
            raise HTTPException(404, "tile not found")
        media = f"image/{native_ext}".replace("jpg", "jpeg")
        return Response(content=body, media_type=media, headers=headers)

    if src["kind"] == "geotiff":
        try:
            tif_path = wdir / src["path"]
            body = raster_mod.render_geotiff_tile(tif_path, z, x, y)
        except Exception as e:
            raise HTTPException(404, f"tile out of bounds or render failed: {e}")
        return Response(content=body, media_type="image/png", headers=headers)

    raise HTTPException(500, f"unknown source kind: {src['kind']}")


# Legacy route (kept for the existing planetos map.json rewrite); resolves to
# the first available raster source — mostly the only one for planetos.
@app.get(
    "/api/worlds/{slug}/tiles/{z}/{x}/{filename}",
    dependencies=[Depends(require_user)],
)
def get_tile_legacy(slug: str, z: int, x: int, filename: str) -> Response:
    try:
        wdir = world_mod.world_dir(slug)
    except FileNotFoundError:
        raise HTTPException(404, f"unknown world: {slug}")
    sources = raster_mod.discover(wdir)
    if not sources:
        raise HTTPException(404, "no raster sources for this world")
    return get_raster_tile(slug, sources[0]["name"], z, x, filename)
