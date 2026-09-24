"""Click-to-trace on scanned maps: a region of similar colour becomes a cited polygon.

Plain computer vision, no model: flood-fill from the clicked pixel within a colour tolerance, take
the region's outline (with holes), simplify it in pixel space, and map it to lon/lat with the scan's
saved georeference. The result carries a Cited GeoJSON citation pointing at the exact pixels it came
from (an SvgSelector with the traced outline, plus the bounding box as a FragmentSelector), so the
traced geometry can always be checked against the scan.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from . import georef

MAX_SHARE = 0.25  # a region covering more of the scan than this probably leaked through a gap


def load_rgb(path: Path) -> np.ndarray:
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = None
    with Image.open(path) as im:
        return np.asarray(im.convert("RGB")).copy()


def flood_region(img: np.ndarray, seed: tuple[int, int], tolerance: int) -> np.ndarray:
    """Boolean mask of the 4-connected region around `seed` within `tolerance` per channel."""
    import cv2
    h, w = img.shape[:2]
    x, y = seed
    if not (0 <= x < w and 0 <= y < h):
        raise georef.GeorefError("the seed point is outside the scan")
    mask = np.zeros((h + 2, w + 2), np.uint8)
    t = (int(tolerance),) * 3
    cv2.floodFill(img.copy(), mask, (int(x), int(y)), (0, 0, 0), t, t,
                  flags=4 | cv2.FLOODFILL_FIXED_RANGE | cv2.FLOODFILL_MASK_ONLY | (255 << 8))
    return mask[1:-1, 1:-1] > 0


def outline(mask: np.ndarray, simplify_px: float) -> list[list[list[float]]]:
    """Polygon rings (outer first, then holes) in pixel coordinates, simplified."""
    import cv2
    from shapely.geometry import Polygon
    contours, hierarchy = cv2.findContours(mask.astype(np.uint8), cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        raise georef.GeorefError("nothing to trace here")
    outer_idx = max((i for i in range(len(contours)) if hierarchy[0][i][3] == -1),
                    key=lambda i: cv2.contourArea(contours[i]))
    # findContours returns pixel indices; +0.5 puts vertices on pixel centres, so the outline lies
    # within half a pixel of the region's true edge.
    ring = lambda c: [[float(px) + 0.5, float(py) + 0.5] for px, py in c.reshape(-1, 2)]  # noqa: E731
    holes = [ring(contours[i]) for i in range(len(contours))
             if hierarchy[0][i][3] == outer_idx and len(contours[i]) >= 3]
    poly = Polygon(ring(contours[outer_idx]), [h for h in holes if len(h) >= 3])
    poly = poly.buffer(0).simplify(simplify_px, preserve_topology=True)
    if poly.geom_type != "Polygon" or poly.is_empty:
        raise georef.GeorefError("the traced region is too thin to form a polygon")
    return [[list(c) for c in poly.exterior.coords]] + [[list(c) for c in r.coords] for r in poly.interiors]


def trace(world_dir: Path, scan: str, seed: tuple[int, int], tolerance: int = 32,
          simplify_px: float = 1.5, source: str | None = None) -> dict[str, Any]:
    import json
    path = georef.scan_path(world_dir, scan)
    gp = georef.georef_path(world_dir, scan)
    if not gp.exists():
        raise FileNotFoundError("georeference the scan before tracing on it")
    ann = json.loads(gp.read_text(encoding="utf-8"))
    model = georef.fit(georef.gcps_of(ann), georef.method_of(ann))["forward"]

    img = load_rgb(path)
    mask = flood_region(img, seed, tolerance)
    share = float(mask.mean())
    rings_px = outline(mask, simplify_px)
    rings_ll = [georef.to_lonlat(model.apply(np.array(r))).round(8).tolist() for r in rings_px]

    xs, ys = np.array(rings_px[0])[:, 0], np.array(rings_px[0])[:, 1]
    x0, y0 = int(np.floor(xs.min())), int(np.floor(ys.min()))
    bbox = f"xywh={x0},{y0},{int(np.ceil(xs.max())) - x0},{int(np.ceil(ys.max())) - y0}"
    h, w = img.shape[:2]
    svg_path = " ".join("M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in r) + " Z" for r in rings_px)
    svg = f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}"><path d="{svg_path}"/></svg>'
    citation: dict[str, Any] = {
        "file": scan,
        "method": "traced",
        "supports": ["geometry"],
        "selector": [{"type": "SvgSelector", "value": svg}, {"type": "FragmentSelector", "value": bbox}],
    }
    if source:
        citation = {"source": source, **citation}
    warnings = []
    if not source:
        warnings.append("this scan is not registered under a source, so the citation has no `source` yet: "
                        "register the scan in the source registry to make it complete")
    if share > MAX_SHARE:
        warnings.append(f"the region covers {share:.0%} of the scan: it probably leaked through a gap; "
                        "lower the tolerance or click inside a closed shape")
    return {
        "feature": {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": rings_ll},
                    "properties": {}, "citations": [citation]},
        "pixels": int(mask.sum()),
        "share_of_scan": round(share, 4),
        "warnings": warnings,
    }


def crop_png(path: Path, xywh: list[int], max_side: int = 1536) -> bytes:
    """A region of the scan as PNG (downscaled for the model if large)."""
    from io import BytesIO
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = None
    x, y, w, h = (int(v) for v in xywh)
    if w <= 0 or h <= 0:
        raise georef.GeorefError("the region needs a positive width and height")
    with Image.open(path) as im:
        W, H = im.size
        if x < 0 or y < 0 or x + w > W or y + h > H:
            raise georef.GeorefError(f"the region lies outside the {W}×{H} scan")
        region = im.convert("RGB").crop((x, y, x + w, y + h))
        if max(w, h) > max_side:
            f = max_side / max(w, h)
            region = region.resize((max(1, round(w * f)), max(1, round(h * f))), Image.LANCZOS)
        buf = BytesIO()
        region.save(buf, "PNG")
        return buf.getvalue()


def source_of_scan(registry: dict[str, Any], scan: str) -> str | None:
    """The registry source that lists this scan among its files, if any."""
    for iri, src in registry.items():
        if any(f.get("id") == scan for f in src.get("files", [])):
            return iri
    return None
