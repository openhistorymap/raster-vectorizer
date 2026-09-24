"""Georeferencing scanned maps: control points in, accuracy and a warped GeoTIFF out.

A scan lives in `<world>/raw/scans/<name>.<ext>`. Its control points (pixel ↔ lon/lat) are stored as
an IIIF Georeference Annotation (the Allmaps format, also what Cited GeoJSON's `georeferences`
member carries) in `<world>/raw/georef/<name>.json`, with the fitted model's accuracy and the
checks that were run.

Models are fitted, as Allmaps does, from pixels to Web Mercator metres, with a separate backward
model (metres → pixels) fitted on the same points for warping. Accuracy is reported two ways:

- residuals: how far each control point lands from where it was placed (0 for thin-plate splines,
  which interpolate exactly, so they say nothing there);
- leave-one-out: each point predicted by a model fitted without it — an honest estimate of the error
  between control points, and the figure to publish.

The warp is done here, with the same backward model, so the published accuracy describes exactly
the image that was produced. Output: `<world>/cogs/<name>.tif` (EPSG:3857), which the raster
discovery already serves as a basemap.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any

import numpy as np

SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp")
R = 6378137.0
MAX_LAT = 85.05112878
METHODS = {"polynomial1": 3, "polynomial2": 6, "polynomial3": 10, "tps": 3}
GEOREF_CONTEXT = "http://iiif.io/api/extension/georef/1/context.json"


class GeorefError(ValueError):
    pass


# --------------------------------------------------------------------------- projections

def to_merc(lonlat: np.ndarray) -> np.ndarray:
    lon, lat = lonlat[:, 0], np.clip(lonlat[:, 1], -MAX_LAT, MAX_LAT)
    return np.column_stack([R * np.radians(lon), R * np.log(np.tan(np.pi / 4 + np.radians(lat) / 2))])


def to_lonlat(merc: np.ndarray) -> np.ndarray:
    return np.column_stack([np.degrees(merc[:, 0] / R),
                            np.degrees(2 * np.arctan(np.exp(merc[:, 1] / R)) - np.pi / 2)])


# --------------------------------------------------------------------------- models

class Polynomial:
    """Least-squares polynomial of order 1–3, fitted on normalised coordinates."""

    def __init__(self, order: int):
        self.order = order

    def _terms(self, p: np.ndarray) -> np.ndarray:
        x, y = ((p - self.shift) / self.scale).T
        cols = [np.ones_like(x), x, y]
        if self.order >= 2:
            cols += [x * x, x * y, y * y]
        if self.order >= 3:
            cols += [x ** 3, x * x * y, x * y * y, y ** 3]
        return np.column_stack(cols)

    def fit(self, src: np.ndarray, dst: np.ndarray) -> "Polynomial":
        self.shift = src.mean(axis=0)
        self.scale = max(float(np.abs(src - self.shift).max()), 1e-12)
        a = self._terms(src)
        if np.linalg.matrix_rank(a) < a.shape[1]:
            raise GeorefError("control points are collinear or too clustered for this transformation")
        self.coef, *_ = np.linalg.lstsq(a, dst, rcond=None)
        return self

    def apply(self, p: np.ndarray) -> np.ndarray:
        return self._terms(p) @ self.coef


class ThinPlateSpline:
    """Exact-interpolating thin-plate spline (with its affine part), on normalised coordinates."""

    def fit(self, src: np.ndarray, dst: np.ndarray) -> "ThinPlateSpline":
        self.shift = src.mean(axis=0)
        self.scale = max(float(np.abs(src - self.shift).max()), 1e-12)
        s = (src - self.shift) / self.scale
        n = len(s)
        k = self._kernel(s, s)
        p = np.column_stack([np.ones(n), s])
        if np.linalg.matrix_rank(p) < 3:
            raise GeorefError("control points are collinear: a thin-plate spline needs them spread out")
        big = np.zeros((n + 3, n + 3))
        big[:n, :n], big[:n, n:], big[n:, :n] = k, p, p.T
        rhs = np.zeros((n + 3, 2))
        rhs[:n] = dst
        self.weights = np.linalg.solve(big, rhs)
        self.ctrl = s
        return self

    @staticmethod
    def _kernel(a: np.ndarray, b: np.ndarray) -> np.ndarray:
        d2 = ((a[:, None, :] - b[None, :, :]) ** 2).sum(-1)
        with np.errstate(divide="ignore", invalid="ignore"):
            k = 0.5 * d2 * np.log(d2)
        return np.nan_to_num(k)

    def apply(self, p: np.ndarray) -> np.ndarray:
        s = (p - self.shift) / self.scale
        n = len(self.ctrl)
        return self._kernel(s, self.ctrl) @ self.weights[:n] + \
            np.column_stack([np.ones(len(s)), s]) @ self.weights[n:]


def make_model(method: str):
    if method == "tps":
        return ThinPlateSpline()
    if method in METHODS:
        return Polynomial(int(method[-1]))
    raise GeorefError(f"unknown transformation {method!r}; use one of {', '.join(METHODS)}")


# --------------------------------------------------------------------------- fitting

def parse_gcps(gcps: list[dict[str, Any]]) -> tuple[np.ndarray, np.ndarray]:
    try:
        pix = np.array([[float(g["pixel"][0]), float(g["pixel"][1])] for g in gcps])
        ll = np.array([[float(g["lonlat"][0]), float(g["lonlat"][1])] for g in gcps])
    except (KeyError, TypeError, ValueError, IndexError):
        raise GeorefError("each control point needs pixel: [x, y] and lonlat: [lon, lat]")
    if len(gcps) and (np.abs(ll[:, 0]).max() > 180 or np.abs(ll[:, 1]).max() > MAX_LAT):
        raise GeorefError("control point coordinates must be lon in ±180 and lat in ±85.05")
    return pix, ll


def fit(gcps: list[dict[str, Any]], method: str) -> dict[str, Any]:
    """Fit the model and measure it. Returns models plus per-point residuals and summary accuracy."""
    need = METHODS.get(method)
    if need is None:
        make_model(method)  # raises with the list of methods
    pix, ll = parse_gcps(gcps)
    if len(pix) < need:
        raise GeorefError(f"{method} needs at least {need} control points; {len(pix)} given")
    merc = to_merc(ll)
    forward = make_model(method).fit(pix, merc)
    backward = make_model(method).fit(merc, pix)

    residuals = np.linalg.norm(forward.apply(pix) - merc, axis=1)
    loo = []
    if len(pix) > need:
        for i in range(len(pix)):
            keep = np.arange(len(pix)) != i
            try:
                m = make_model(method).fit(pix[keep], merc[keep])
                loo.append(float(np.linalg.norm(m.apply(pix[i:i + 1])[0] - merc[i])))
            except GeorefError:
                loo.append(float("nan"))
    # Ground scale: Web Mercator stretches distances by 1/cos(lat); report metres on the ground.
    scale = np.cos(np.radians(ll[:, 1]))
    ground_res = residuals * scale
    ground_loo = np.array(loo) * scale if loo else np.array([])
    accuracy = {
        "rmse": round(float(np.sqrt((ground_res ** 2).mean())), 3),
        "unit": "m",
        "method": "residuals",
        "controlPoints": int(len(pix)),
    }
    if len(ground_loo) and np.isfinite(ground_loo).all():
        accuracy["leaveOneOutRmse"] = round(float(np.sqrt((ground_loo ** 2).mean())), 3)
        if method == "tps":  # residuals of an interpolating spline are 0 by construction
            accuracy["rmse"] = accuracy["leaveOneOutRmse"]
            accuracy["method"] = "leave-one-out"
    return {
        "forward": forward, "backward": backward,
        "points": [{"pixel": pix[i].tolist(), "lonlat": ll[i].tolist(),
                    "residual_m": round(float(ground_res[i]), 3),
                    **({"leave_one_out_m": round(float(ground_loo[i]), 3)} if len(ground_loo) else {})}
                   for i in range(len(pix))],
        "accuracy": accuracy,
    }


# --------------------------------------------------------------------------- checks

def footprint(forward, width: int, height: int, steps: int = 16) -> np.ndarray:
    """The image outline mapped to lon/lat (a closed ring, sampled along the edges)."""
    t = np.linspace(0, 1, steps)
    edge = np.concatenate([
        np.column_stack([t * width, np.zeros(steps)]), np.column_stack([np.full(steps, width), t * height]),
        np.column_stack([(1 - t) * width, np.full(steps, height)]), np.column_stack([np.zeros(steps), (1 - t) * height]),
    ])
    return to_lonlat(forward.apply(edge))


def _ground_distance(a: np.ndarray, b: np.ndarray) -> float:
    (lon1, lat1), (lon2, lat2) = np.radians(a), np.radians(b)
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371008.8 * math.asin(math.sqrt(h))


def checks(result: dict[str, Any], width: int, height: int, timeline: dict[str, Any],
           expected_width_m: float | None = None) -> list[dict[str, str]]:
    """Sanity checks on a fitted georeference. level: error | warning | info."""
    out = []
    fwd = result["forward"]
    ring = footprint(fwd, width, height)
    if not np.isfinite(ring).all() or np.abs(ring[:, 1]).max() >= MAX_LAT or np.abs(ring[:, 0]).max() > 180:
        out.append({"level": "error", "message": "the image maps outside valid coordinates: check the control points"})
        return out

    mid = to_lonlat(fwd.apply(np.array([[0, height / 2], [width, height / 2], [width / 2, 0], [width / 2, height]])))
    width_m = _ground_distance(mid[0], mid[1])
    height_m = _ground_distance(mid[2], mid[3])
    if expected_width_m:
        ratio = width_m / expected_width_m
        level = "error" if ratio < 0.5 or ratio > 2 else "info"
        out.append({"level": level, "message": f"image spans {width_m:,.1f} m across; expected about "
                                               f"{expected_width_m:,.1f} m (ratio {ratio:.2f})"})
    else:
        out.append({"level": "info", "message": f"image spans about {width_m:,.1f} × {height_m:,.1f} m"})
    if width_m < 1:
        out.append({"level": "warning", "message": f"the whole image is only {width_m * 100:.1f} cm across: "
                                                   "is the scale right?"})

    # Folding: a model that flips orientation between control points has gone wrong.
    xs, ys = np.meshgrid(np.linspace(0, width, 9), np.linspace(0, height, 9))
    grid = fwd.apply(np.column_stack([xs.ravel(), ys.ravel()])).reshape(9, 9, 2)
    ex, ey = grid[:, 1:, :] - grid[:, :-1, :], grid[1:, :, :] - grid[:-1, :, :]
    cross = ex[:-1, :, 0] * ey[:, :-1, 1] - ex[:-1, :, 1] * ey[:, :-1, 0]
    if (np.sign(cross) != np.sign(np.median(cross))).any():
        out.append({"level": "warning", "message": "the transformation folds over itself somewhere in the "
                                                   "image: add control points or use a lower order"})

    base = timeline.get("base") or {}
    if "lat" in base and "lng" in base:
        from shapely.geometry import Point, Polygon
        inside = Polygon(ring).contains(Point(float(base["lng"]), float(base["lat"])))
        if not inside:
            out.append({"level": "info", "message": "the world's default view (timeline.json base) is outside "
                                                    "this image"})
    acc = result["accuracy"]
    overfit = not isinstance(fwd, ThinPlateSpline) and "leaveOneOutRmse" in acc and \
        acc["leaveOneOutRmse"] > 3 * max(acc["rmse"], 1e-9) and acc["leaveOneOutRmse"] > 1
    if overfit:
        out.append({"level": "warning", "message": "leave-one-out error is much larger than the residuals: "
                                                   "the model may be over-fitting; try a lower order"})
    return out


# --------------------------------------------------------------------------- scans on disk

def scans_dir(world_dir: Path) -> Path:
    return world_dir / "raw" / "scans"


def scan_path(world_dir: Path, name: str) -> Path:
    if not SAFE_NAME.match(name):
        raise GeorefError(f"invalid scan name {name!r}")
    for ext in IMAGE_EXTS:
        p = scans_dir(world_dir) / f"{name}{ext}"
        if p.exists():
            return p
    raise FileNotFoundError(name)


def georef_path(world_dir: Path, name: str) -> Path:
    return world_dir / "raw" / "georef" / f"{name}.json"


def image_size(path: Path) -> tuple[int, int]:
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = None
    with Image.open(path) as im:
        return im.size


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def scan_info(world_dir: Path, path: Path) -> dict[str, Any]:
    w, h = image_size(path)
    info = {"name": path.stem, "file": path.name, "width": w, "height": h, "bytes": path.stat().st_size}
    gp = georef_path(world_dir, path.stem)
    if gp.exists():
        ann = json.loads(gp.read_text(encoding="utf-8"))
        info["georeference"] = {"accuracy": ann.get("accuracy"),
                                "transformation": ann.get("body", {}).get("transformation")}
    if (world_dir / "cogs" / f"{path.stem}.tif").exists():
        info["warped"] = f"cogs/{path.stem}.tif"
    return info


def list_scans(world_dir: Path) -> list[dict[str, Any]]:
    d = scans_dir(world_dir)
    if not d.is_dir():
        return []
    return [scan_info(world_dir, p) for p in sorted(d.iterdir()) if p.suffix.lower() in IMAGE_EXTS]


def annotation(name: str, width: int, height: int, method: str, result: dict[str, Any],
               check_list: list[dict[str, str]], expected_width_m: float | None) -> dict[str, Any]:
    transformation = ({"type": "thinPlateSpline"} if method == "tps"
                      else {"type": "polynomial", "options": {"order": int(method[-1])}})
    ann = {
        "@context": GEOREF_CONTEXT,
        "id": f"georef:{name}",
        "type": "Annotation",
        "motivation": "georeferencing",
        "target": {"type": "SpecificResource", "source": {"id": name, "type": "Image", "width": width, "height": height}},
        "body": {
            "type": "FeatureCollection",
            "transformation": transformation,
            "features": [{"type": "Feature", "properties": {"resourceCoords": p["pixel"]},
                          "geometry": {"type": "Point", "coordinates": p["lonlat"]}} for p in result["points"]],
        },
        "accuracy": result["accuracy"],
        "checks": check_list,
    }
    if expected_width_m:
        ann["expectedWidthM"] = expected_width_m
    return ann


def method_of(ann: dict[str, Any]) -> str:
    t = ann.get("body", {}).get("transformation", {})
    if t.get("type") == "thinPlateSpline":
        return "tps"
    return f"polynomial{t.get('options', {}).get('order', 1)}"


def gcps_of(ann: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"pixel": f["properties"]["resourceCoords"], "lonlat": f["geometry"]["coordinates"]}
            for f in ann.get("body", {}).get("features", [])]


# --------------------------------------------------------------------------- warping

def warp(src_path: Path, out_path: Path, forward, backward, max_size: int = 8192,
         block_rows: int = 512) -> dict[str, Any]:
    """Resample the scan into EPSG:3857 with the fitted backward model; write a tiled GeoTIFF."""
    import cv2
    import rasterio
    from PIL import Image
    from rasterio.enums import Resampling
    from rasterio.transform import from_origin

    Image.MAX_IMAGE_PIXELS = None
    with Image.open(src_path) as im:
        img = np.asarray(im.convert("RGBA"))
    h, w = img.shape[:2]

    ring = forward.apply(np.array([[x, y] for x in np.linspace(0, w, 33) for y in (0, h)] +
                                  [[x, y] for y in np.linspace(0, h, 33) for x in (0, w)]))
    minx, miny = ring.min(axis=0)
    maxx, maxy = ring.max(axis=0)
    # Native resolution: the median ground size of a source pixel, capped by max_size.
    centre = np.array([[w / 2, h / 2], [w / 2 + 1, h / 2], [w / 2, h / 2 + 1]])
    c = forward.apply(centre)
    res = float(np.mean([np.linalg.norm(c[1] - c[0]), np.linalg.norm(c[2] - c[0])]))
    res = max(res, (maxx - minx) / max_size, (maxy - miny) / max_size)
    out_w, out_h = int(math.ceil((maxx - minx) / res)), int(math.ceil((maxy - miny) / res))

    out = np.zeros((4, out_h, out_w), dtype=np.uint8)
    xs = minx + (np.arange(out_w) + 0.5) * res
    for r0 in range(0, out_h, block_rows):
        rows = np.arange(r0, min(out_h, r0 + block_rows))
        ys = maxy - (rows + 0.5) * res
        gx, gy = np.meshgrid(xs, ys)
        src = backward.apply(np.column_stack([gx.ravel(), gy.ravel()]))
        map_x = src[:, 0].reshape(gx.shape).astype(np.float32)
        map_y = src[:, 1].reshape(gx.shape).astype(np.float32)
        block = cv2.remap(img, map_x, map_y, interpolation=cv2.INTER_LINEAR,
                          borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0, 0))
        out[:, rows[0]:rows[-1] + 1, :] = np.moveaxis(block, -1, 0)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(".tif.tmp")
    profile = dict(driver="GTiff", width=out_w, height=out_h, count=4, dtype="uint8", crs="EPSG:3857",
                   transform=from_origin(minx, maxy, res, res), tiled=True, blockxsize=512, blockysize=512,
                   compress="deflate", photometric="RGB", alpha="YES")
    with rasterio.open(tmp, "w", **profile) as dst:
        dst.write(out)
        levels = [f for f in (2, 4, 8, 16, 32, 64) if min(out_w, out_h) // f >= 256]
        if levels:
            dst.build_overviews(levels, Resampling.average)
    tmp.replace(out_path)
    return {"path": str(out_path), "width": out_w, "height": out_h, "resolution_m": round(res, 4),
            "bounds_3857": [float(minx), float(miny), float(maxx), float(maxy)]}
