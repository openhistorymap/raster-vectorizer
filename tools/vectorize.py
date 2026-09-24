#!/usr/bin/env python3
"""raster-vectorizer — turn raster tile pyramids into OFM-shaped GeoJSON layers.

See README.md for design notes. This file is the CLI entrypoint.

Currently implemented: mosaic, palette, segment.
Stubs: trace, ocr, merge, assist.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


# ---------- georef ----------------------------------------------------------

def load_georef(path: str | None):
    """Return a callable (px, py, W, H) -> (lon, lat).

    If `path` is None, returns the identity (lon=px, lat=py) — useful for
    fictional maps with no real-world CRS (Night City, ship decks).
    """
    if not path:
        return lambda px, py, W, H: (px, py)
    cfg = json.loads(Path(path).read_text())
    g = cfg["georef"]
    if g["type"] != "linear":
        raise SystemExit(f"unsupported georef type: {g['type']}")
    lat_top, lng_left = g["top_left"]
    lat_bot, lng_right = g["bottom_right"]

    def project(px, py, W, H):
        u = px / W
        v = py / H
        lng = lng_left + (lng_right - lng_left) * u
        lat = lat_top + (lat_bot - lat_top) * v
        return (lng, lat)

    return project


# ---------- mosaic ----------------------------------------------------------

def cmd_mosaic(args):
    from PIL import Image

    tiles_dir = Path(args.tiles) / str(args.zoom)
    if not tiles_dir.is_dir():
        raise SystemExit(f"no tiles dir at {tiles_dir}")
    n = 1 << args.zoom
    tile_size = args.tile_size
    W, H = n * tile_size, n * tile_size

    out = Image.new("RGB", (W, H), (0, 0, 0))
    missing = 0
    for x in range(n):
        col = tiles_dir / str(x)
        if not col.is_dir():
            missing += n
            continue
        for y in range(n):
            for ext in ("png", "jpg", "jpeg", "webp"):
                p = col / f"{y}.{ext}"
                if p.exists():
                    try:
                        out.paste(Image.open(p), (x * tile_size, y * tile_size))
                    except Exception as e:
                        print(f"  warn: {p}: {e}", file=sys.stderr)
                    break
            else:
                missing += 1
    out.save(args.out)
    print(f"wrote {args.out}  ({W}x{H} px, {missing} missing tiles)")


# ---------- palette ---------------------------------------------------------

def cmd_palette(args):
    from PIL import Image
    import numpy as np

    img = Image.open(args.image).convert("RGB")
    # downsample to make k-means tractable
    img.thumbnail((1024, 1024))
    arr = np.asarray(img).reshape(-1, 3).astype(np.float32)

    try:
        from sklearn.cluster import MiniBatchKMeans

        km = MiniBatchKMeans(n_clusters=args.k, n_init=3, random_state=0)
        km.fit(arr)
        centers = km.cluster_centers_.astype(int).tolist()
        counts = np.bincount(km.labels_, minlength=args.k).tolist()
    except ImportError:
        # tiny fallback: quantize via PIL
        q = img.convert("RGB").quantize(colors=args.k, method=Image.Quantize.FASTOCTREE)
        pal = q.getpalette()[: args.k * 3]
        centers = [pal[i : i + 3] for i in range(0, len(pal), 3)]
        counts = [c for _, c in q.getcolors()]

    palette = [
        {
            "class": f"class_{i}",
            "rgb": rgb,
            "count": int(c),
            "tolerance": 25,
        }
        for i, (rgb, c) in enumerate(sorted(zip(centers, counts), key=lambda p: -p[1]))
    ]
    Path(args.out).write_text(json.dumps(palette, indent=2))
    print(f"wrote {args.out}  ({args.k} classes)")
    print("→ edit the `class` fields to label each colour, then `segment`")


# ---------- segment ---------------------------------------------------------

def cmd_segment(args):
    from PIL import Image
    import numpy as np

    try:
        import cv2
    except ImportError:
        raise SystemExit("`pip install opencv-python-headless` required for segment")

    img = np.asarray(Image.open(args.image).convert("RGB"))
    H, W = img.shape[:2]
    project = load_georef(args.georef)

    palette = json.loads(Path(args.palette).read_text())
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    layers: dict[str, list] = {}
    for entry in palette:
        cls = entry["class"]
        if cls.startswith("class_") or cls in ("ignore", "skip"):
            continue
        rgb = np.array(entry["rgb"], dtype=np.int32)
        tol = int(entry.get("tolerance", 25))
        diff = np.abs(img.astype(np.int32) - rgb).sum(axis=2)
        mask = (diff < tol * 3).astype(np.uint8) * 255

        # smooth small noise
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))

        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        features = []
        for cnt in contours:
            if cv2.contourArea(cnt) < args.min_area:
                continue
            ring = [project(int(p[0][0]), int(p[0][1]), W, H) for p in cnt]
            if len(ring) < 4:
                continue
            if ring[0] != ring[-1]:
                ring.append(ring[0])
            features.append(
                {
                    "type": "Feature",
                    "geometry": {"type": "Polygon", "coordinates": [list(ring)]},
                    "properties": {"class": cls},
                }
            )
        layers.setdefault(cls, []).extend(features)

    for cls, feats in layers.items():
        path = out_dir / f"{cls}.geojson"
        path.write_text(
            json.dumps({"type": "FeatureCollection", "features": feats}, indent=1)
        )
        print(f"wrote {path}  ({len(feats)} polygons)")


# ---------- stubs -----------------------------------------------------------

def cmd_stub(name, note):
    def runner(args):
        print(f"[{name}] not yet implemented — {note}", file=sys.stderr)
        sys.exit(2)

    return runner


# ---------- CLI -------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(description="raster-vectorizer")
    sp = p.add_subparsers(dest="cmd", required=True)

    pm = sp.add_parser("mosaic", help="stitch a {z}/{x}/{y} pyramid into one image")
    pm.add_argument("--tiles", required=True, help="path containing zoom-level dirs")
    pm.add_argument("--zoom", type=int, required=True)
    pm.add_argument("--tile-size", type=int, default=256)
    pm.add_argument("--out", required=True)
    pm.set_defaults(fn=cmd_mosaic)

    pp = sp.add_parser("palette", help="k-means sample the colour palette of an image")
    pp.add_argument("--image", required=True)
    pp.add_argument("--k", type=int, default=8)
    pp.add_argument("--out", required=True)
    pp.set_defaults(fn=cmd_palette)

    ps = sp.add_parser("segment", help="colour-segment an image into polygon GeoJSON layers")
    ps.add_argument("--image", required=True)
    ps.add_argument("--palette", required=True, help="palette JSON with labelled classes")
    ps.add_argument("--georef", help="optional georef config (linear top_left/bottom_right)")
    ps.add_argument("--min-area", type=int, default=20, help="discard polygons under this px area")
    ps.add_argument("--out-dir", required=True)
    ps.set_defaults(fn=cmd_segment)

    for name, note in (
        ("trace",  "skeletonize lines (roads/rivers) — needs scikit-image + shapely"),
        ("ocr",    "extract place-name labels — needs pytesseract + tesseract-ocr"),
        ("merge",  "union per-tile output across seams — needs shapely"),
        ("assist", "Claude vision pass on the mosaic — needs anthropic SDK + API key"),
    ):
        sub = sp.add_parser(name, help=f"(stub) {note}")
        sub.set_defaults(fn=cmd_stub(name, note))

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
