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

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Response, UploadFile
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

from . import cited as cited_mod
from . import georef as georef_mod
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


# --- Cited GeoJSON: sources registry, export, import ---------------------

def _world_or_404(slug: str) -> Path:
    try:
        return world_mod.world_dir(slug)
    except FileNotFoundError:
        raise HTTPException(404, f"unknown world: {slug}")


def _timeline(wdir: Path) -> dict[str, Any]:
    try:
        return _json.loads((wdir / "timeline.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


@app.get("/api/worlds/{slug}/sources", dependencies=[Depends(require_user)])
def get_sources(slug: str) -> dict[str, Any]:
    """The world's source registry: Cited GeoJSON `sources`, keyed by source IRI."""
    return cited_mod.load_sources(_world_or_404(slug))


@app.put("/api/worlds/{slug}/sources", dependencies=[Depends(require_user)])
def put_sources(slug: str, body: dict[str, Any]) -> dict[str, Any]:
    wdir = _world_or_404(slug)
    try:
        return cited_mod.save_sources(wdir, body)
    except cited_mod.CitedGeoJSONError as e:
        raise HTTPException(422, {"errors": e.errors})


@app.get("/api/worlds/{slug}/layers/{layer}/cited-geojson", dependencies=[Depends(require_user)])
def export_cited(slug: str, layer: str) -> Response:
    """The layer as a validated Cited GeoJSON document (422 with reasons if it cannot be)."""
    wdir = _world_or_404(slug)
    fc = _call_or_503(wdir, "load_layer", layer)
    try:
        doc = cited_mod.export_layer(wdir, _timeline(wdir), layer, fc)
    except cited_mod.CitedGeoJSONError as e:
        raise HTTPException(422, {"errors": e.errors})
    return UnicodeJSONResponse(content=doc, media_type="application/geo+json",
                               headers={"Content-Disposition": f'attachment; filename="{slug}-{layer}.geojson"'})


@app.put("/api/worlds/{slug}/layers/{layer}/cited-geojson")
def import_cited(slug: str, layer: str, body: dict[str, Any],
                 user: str = Depends(require_user)) -> dict[str, Any]:
    """Import a Cited GeoJSON document into a layer: its sources are merged into the registry
    (existing entries win; differences are reported), its features saved like a normal save."""
    wdir = _world_or_404(slug)
    try:
        fc, report = cited_mod.prepare_import(wdir, body)
    except cited_mod.CitedGeoJSONError as e:
        raise HTTPException(422, {"errors": e.errors})
    summary = _call_or_503(wdir, "save_layer", layer, fc, editor=user)
    return {**summary, "world": slug, "sources": report}


# --- scans and georeferencing ----------------------------------------------

def _scan_or_404(wdir: Path, name: str) -> Path:
    try:
        return georef_mod.scan_path(wdir, name)
    except georef_mod.GeorefError as e:
        raise HTTPException(400, str(e))
    except FileNotFoundError:
        raise HTTPException(404, f"unknown scan: {name}")


@app.get("/api/worlds/{slug}/scans", dependencies=[Depends(require_user)])
def get_scans(slug: str) -> list[dict[str, Any]]:
    return georef_mod.list_scans(_world_or_404(slug))


@app.post("/api/worlds/{slug}/scans", dependencies=[Depends(require_user)])
async def upload_scan(slug: str, file: UploadFile = File(...), name: str | None = Form(None),
                      source: str | None = Form(None)) -> dict[str, Any]:
    """Upload a scanned map. With `source` (an IRI in the world's registry) the scan is also
    recorded as a file of that source, with its checksum, so citations can point into it."""
    wdir = _world_or_404(slug)
    ext = Path(file.filename or "").suffix.lower()
    if ext not in georef_mod.IMAGE_EXTS:
        raise HTTPException(400, f"unsupported image type {ext!r}; use {', '.join(georef_mod.IMAGE_EXTS)}")
    stem = name or Path(file.filename).stem
    if not georef_mod.SAFE_NAME.match(stem):
        raise HTTPException(400, f"invalid scan name {stem!r}")
    registry = cited_mod.load_sources(wdir)
    if source and source not in registry:
        raise HTTPException(422, f"source {source} is not in the world's source registry")
    d = georef_mod.scans_dir(wdir)
    d.mkdir(parents=True, exist_ok=True)
    if any((d / f"{stem}{e}").exists() for e in georef_mod.IMAGE_EXTS):
        raise HTTPException(409, f"a scan named {stem!r} already exists")
    path = d / f"{stem}{ext}"
    tmp = path.with_suffix(ext + ".part")
    with tmp.open("wb") as fh:
        while chunk := await file.read(1 << 20):
            fh.write(chunk)
    try:
        georef_mod.image_size(tmp)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise HTTPException(400, "the upload is not a readable image")
    tmp.replace(path)
    info = georef_mod.scan_info(wdir, path)
    if source:
        entry = {"id": stem, "checksum": georef_mod.sha256(path),
                 "mediaType": file.content_type or "application/octet-stream", "label": file.filename}
        files = [f for f in registry[source].get("files", []) if f.get("id") != stem] + [entry]
        registry[source] = {**registry[source], "files": files}
        cited_mod.save_sources(wdir, registry)
        info["source"] = source
    return info


@app.get("/api/worlds/{slug}/scans/{name}/image", dependencies=[Depends(require_user)])
def get_scan_image(slug: str, name: str, size: int = Query(2048, alias="max", ge=64, le=8192)) -> Response:
    """The scan as PNG, downscaled so its longer side is at most `max` px. The response header
    X-Scale is original px per returned px, to convert clicks back to full-resolution pixels."""
    from io import BytesIO
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = None
    path = _scan_or_404(_world_or_404(slug), name)
    with Image.open(path) as im:
        w, h = im.size
        scale = max(w, h) / size if max(w, h) > size else 1.0
        im = im.convert("RGBA")
        if scale > 1:
            im = im.resize((round(w / scale), round(h / scale)), Image.LANCZOS)
        buf = BytesIO()
        im.save(buf, "PNG")
    return Response(buf.getvalue(), media_type="image/png",
                    headers={"X-Scale": f"{scale:.6f}", "X-Original-Size": f"{w}x{h}",
                             "Access-Control-Expose-Headers": "X-Scale, X-Original-Size"})


def _fit_request(wdir: Path, path: Path, body: dict[str, Any]) -> tuple[dict, list, int, int, str, float | None]:
    method = body.get("transformation", "polynomial1")
    expected = body.get("expectedWidthM")
    w, h = georef_mod.image_size(path)
    try:
        result = georef_mod.fit(body.get("gcps", []), method)
    except georef_mod.GeorefError as e:
        raise HTTPException(422, str(e))
    check_list = georef_mod.checks(result, w, h, _timeline(wdir), float(expected) if expected else None)
    return result, check_list, w, h, method, float(expected) if expected else None


def _fit_response(result: dict, check_list: list, w: int, h: int) -> dict[str, Any]:
    ring = georef_mod.footprint(result["forward"], w, h)
    return {"accuracy": result["accuracy"], "points": result["points"], "checks": check_list,
            "footprint": {"type": "Polygon", "coordinates": [ring.round(8).tolist() + [ring[0].round(8).tolist()]]}}


@app.post("/api/worlds/{slug}/scans/{name}/georef/fit", dependencies=[Depends(require_user)])
def fit_georef(slug: str, name: str, body: dict[str, Any]) -> dict[str, Any]:
    """Dry run: fit control points, return accuracy, per-point errors, checks and footprint."""
    wdir = _world_or_404(slug)
    result, check_list, w, h, *_ = _fit_request(wdir, _scan_or_404(wdir, name), body)
    return _fit_response(result, check_list, w, h)


@app.get("/api/worlds/{slug}/scans/{name}/georef", dependencies=[Depends(require_user)])
def get_georef(slug: str, name: str) -> dict[str, Any]:
    wdir = _world_or_404(slug)
    _scan_or_404(wdir, name)
    gp = georef_mod.georef_path(wdir, name)
    if not gp.exists():
        raise HTTPException(404, f"scan {name} has no georeference yet")
    return _json.loads(gp.read_text(encoding="utf-8"))


@app.put("/api/worlds/{slug}/scans/{name}/georef", dependencies=[Depends(require_user)])
def put_georef(slug: str, name: str, body: dict[str, Any]) -> dict[str, Any]:
    """Fit and save the georeference annotation. Refused (422) when a check fails at error level."""
    wdir = _world_or_404(slug)
    result, check_list, w, h, method, expected = _fit_request(wdir, _scan_or_404(wdir, name), body)
    errors = [c["message"] for c in check_list if c["level"] == "error"]
    if errors:
        raise HTTPException(422, {"errors": errors, "checks": check_list})
    ann = georef_mod.annotation(name, w, h, method, result, check_list, expected)
    gp = georef_mod.georef_path(wdir, name)
    gp.parent.mkdir(parents=True, exist_ok=True)
    tmp = gp.with_suffix(".json.tmp")
    tmp.write_text(_json.dumps(ann, indent=2), encoding="utf-8")
    tmp.replace(gp)
    return {"annotation": ann, **_fit_response(result, check_list, w, h)}


@app.post("/api/worlds/{slug}/scans/{name}/warp", dependencies=[Depends(require_user)])
def warp_scan(slug: str, name: str) -> dict[str, Any]:
    """Warp the scan with its saved georeference into cogs/<name>.tif (a basemap source)."""
    wdir = _world_or_404(slug)
    path = _scan_or_404(wdir, name)
    gp = georef_mod.georef_path(wdir, name)
    if not gp.exists():
        raise HTTPException(409, "save a georeference first")
    ann = _json.loads(gp.read_text(encoding="utf-8"))
    result = georef_mod.fit(georef_mod.gcps_of(ann), georef_mod.method_of(ann))
    out = georef_mod.warp(path, wdir / "cogs" / f"{name}.tif", result["forward"], result["backward"])
    return {**out, "accuracy": ann.get("accuracy"), "raster_source": name}


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
