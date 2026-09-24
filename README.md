# raster-vectorizer

OFM-data-structure-aware web editor for vector layers. Each OFM world has a
raster (or GeoTIFF) basemap and a set of vector layers stored somewhere — this
tool lets you draw, edit, and save those layers on top of the basemap from a
browser, writing back to whatever storage the world's `timeline.json` declares.

```
┌─────────────────────────────────┐         ┌──────────────────────────────┐
│  frontend (Netlify-publishable) │ <─REST─>│  backend (FastAPI in Docker) │
│  MapLibre GL + mapbox-gl-draw   │         │   /srv/ofm mounted at /ofm   │
│  loads each world's map.json    │         │   PostGIS / SpatiaLite /     │
│  directly as a Mapbox-GL style  │         │   GeoJSON-file adapters      │
└─────────────────────────────────┘         └──────────────────────────────┘
```

## Layout

```
raster-vectorizer/
├── backend/                       FastAPI service
│   ├── app.py                     REST API (/api/worlds/.../layers/...)
│   ├── auth.py                    HTTP-basic, env-configured single user
│   ├── worlds.py                  walks /ofm/*/timeline.json
│   ├── storage/
│   │   ├── base.py                Adapter protocol
│   │   ├── geojson_file.py        writes raw/geojson/<layer>.geojson
│   │   ├── spatialite.py          writes <world>/<world>.db (column: the_geom)
│   │   └── postgis.py             writes shared PG host (column: geom)
│   └── tests/                     23 pytest tests covering API + adapters
├── frontend/                      static SPA, Netlify-ready
│   ├── index.html
│   ├── editor.js                  MapLibre setup + draw lifecycle
│   ├── style.css                  dark editor theme
│   ├── config.js                  runtime config (OFM_API_URL)
│   ├── config.js.tmpl             envsubst template for Netlify
│   ├── netlify.toml
│   └── _redirects
├── tools/                         retained batch CLI (mosaic, palette, segment)
├── Dockerfile                     multi-stage: `base` (service) + `test`
├── docker-compose.yml             dev/test orchestration
└── requirements.txt
```

## Quickstart

### Backend (Docker)

```bash
cd /srv/ofm/raster-vectorizer

# build + run tests
docker compose --profile test run --rm --build test

# launch the editor backend (bound to localhost:8765)
docker compose up --build editor
```

Environment variables (set in `.env` or your shell):

| var                  | purpose                                          | default          |
|----------------------|--------------------------------------------------|------------------|
| `OFM_EDITOR_USER`    | HTTP basic auth user (omit for open access)      | unset = open     |
| `OFM_EDITOR_PASSWORD`| HTTP basic auth password                          | unset = open     |
| `OFM_CORS_ORIGINS`   | comma-separated allowed origins (Netlify URL)    | `*`              |
| `OFM_PG_HOST/PORT/…` | override shared PG host for PostGIS adapter      | `51.15.160.236`  |

### Frontend (Netlify)

The `frontend/` directory is a pure-static SPA. Deploy it standalone:

```bash
cd frontend
netlify deploy --dir . --prod
# in Netlify dashboard → site settings → env vars:
#   OFM_API_URL = https://your-editor-backend.example.com
```

`netlify.toml` does envsubst on `config.js.tmpl` at build time so the deployed
JS points at your chosen backend.

For local frontend dev:

```bash
cd frontend
python3 -m http.server 5173    # any static server works
# open http://localhost:5173, backend must be running on :8765
```

## REST API

All routes under `/api`; require HTTP basic auth if `OFM_EDITOR_USER` is set.

| route                                       | method | purpose |
|---------------------------------------------|--------|---------|
| `/health`                                   | GET    | liveness |
| `/api/worlds`                               | GET    | list discovered worlds |
| `/api/worlds/{slug}`                        | GET    | timeline + gaia + map.json descriptor |
| `/api/worlds/{slug}/style`                  | GET    | the world's Mapbox-GL style |
| `/api/worlds/{slug}/layers`                 | GET    | layers known to the storage backend |
| `/api/worlds/{slug}/layers/{layer}`         | GET    | FeatureCollection |
| `/api/worlds/{slug}/layers/{layer}`         | PUT    | replace layer (FeatureCollection body) |
| `/api/worlds/{slug}/layers/{layer}`         | DELETE | drop the layer |
| `/api/worlds/{slug}/rasters`                                | GET    | list raster sources for this world (pyramids + GeoTIFFs) with zoom/bounds/extension |
| `/api/worlds/{slug}/rasters/{source}/tiles/{z}/{x}/{y}.{ext}` | GET    | serve a tile from a named source (raw pyramid or rendered from GeoTIFF) |
| `/api/worlds/{slug}/tiles/{z}/{x}/{y}.{ext}`                | GET    | legacy alias — first raster source |
| `/api/schemas`                              | GET    | list of manifest kinds (timeline\|gaia\|map\|render) |
| `/api/schemas/{kind}`                       | GET    | JSON Schema for that kind |
| `/api/worlds/{slug}/manifest/{kind}`        | GET    | current contents of that manifest file |
| `/api/worlds/{slug}/manifest/{kind}`        | PUT    | validate (422 on schema fail) + atomic write, rolling `.bak` |
| `/api/worlds/{slug}/layers/{layer}/schema`  | GET    | per-layer feature-properties JSON Schema (empty stub if none) |
| `/api/worlds/{slug}/layers/{layer}/schema`  | PUT    | replace layer schema (422 if body isn't a valid JSON Schema) |

## Storage adapters

Selected per-world from `timeline.json#mode`:

- **`mode: "geojson"`** (default) — one `.geojson` per layer under `<world>/raw/geojson/`
- **`mode: "spatialite"`** — uses `<world>/<world>.db` with column `the_geom`, SRID 4326
- **`mode: "postgis"`** — connects to the shared OFM PG host (`51.15.160.236:45432`), column `geom`, SRID 4326

If the configured adapter can't connect (e.g. PG unreachable from the
container), the API returns 503 with a useful diagnostic message rather than 500.

## E2E confirmed against planetos (2026-05-16)

Listed 80 worlds, loaded 245-feature `locations` layer, drew + saved a new
`test_polygon`, reloaded, file persisted at
`/srv/ofm/planetos/raw/geojson/test_polygon.geojson`, then deleted cleanly.

## Raster basemaps

Two basemap source kinds are supported per world. Discovery walks the world
dir and reports everything it finds.

**Pre-baked tile pyramids** — folders under `raw/tiles/`, `aerial/`, or
`tiles/`. Two layouts on disk are recognised:

- `{layer}/{z}/{x}/{y}.{ext}` — standard XYZ (planetos, sta, alien)
- `{layer}/{z}/{x}_{y}.{ext}` — piggyback layout (cyberpunk, zelda-botw)

**GeoTIFFs** — any `.tif`/`.tiff` at the world root, in `raw/`, or in
`cogs/`. Rendered to PNG tiles on demand via `rio-tiler` (which uses the
file's own georeferencing); ideally a Cloud Optimized GeoTIFF for fast
random tile access.

`GET /api/worlds/{slug}/rasters` returns each source with `min_zoom`,
`max_zoom`, `ext`, plus (for GeoTIFFs) `bounds` in EPSG:4326 and the
dataset's native `crs`, so the frontend can position the map and pick
the right tile URL.

The frontend's **Basemap** dropdown lists all discovered sources; picking
one swaps it into the active MapLibre style as a raster layer at the
bottom of the stack (vector overlays sit on top).

## Manifest editor

The editor has a second mode for editing a world's four manifest files
directly (no map). Frontend uses Monaco with the JSON Schema wired in so you
get inline validation + autocomplete + hover-descriptions. Saves go through
`/api/worlds/{slug}/manifest/{kind}` which re-validates server-side and
rejects (422) with per-path error messages before writing.

Files covered:

| kind        | file in `<world>/`  | schema (`backend/schemas/`) |
|-------------|---------------------|-----------------------------|
| `timeline`  | `timeline.json`     | `timeline.schema.json` |
| `gaia`      | `gaia.json`         | `gaia.schema.json` |
| `map`       | `map.json`          | `map.schema.json` (Mapbox-GL v8 + ofm extensions) |
| `render`    | `render.json`       | `render.schema.json` |

Schemas were derived from a sweep of all 80 worlds — `mode` enums, the
ofm-extension fields under `map.metadata.ofm`, the `texture`/`point` render
types, and the wiki-block shape all come from observed usage. Required
fields are minimal so older worlds with quirky content still load; new
fields can be added by editing the schemas without breaking anything.

Each save writes atomically (tmp + `os.replace`) and rotates a single
`.bak`, so a bad save can always be recovered from `<file>.bak`.

## Layer schemas

Each layer can carry a per-layer JSON Schema that describes the typed
shape of its features' `properties` object. Schemas live in a sidecar
file at `<world>/raw/schemas/<layer>.schema.json` — independent of the
storage backend, so they work the same for GeoJSON / SpatiaLite / PostGIS
layers.

When a schema is present:

- The map view's feature-property panel renders typed inputs (text /
  number / checkbox / `<select>` for enums) instead of free-form text.
- Property descriptions appear as tooltips on the labels (ⓘ marker).
- The **+ field** button in the side panel prompts for a field name, type
  (`string` / `integer` / `number` / `boolean` / `enum`), and optional
  description, then PUTs the updated schema.

When no schema is present, the property panel falls back to free-form
key/value editing — existing layers keep working without modification.

## Form-based manifest editor

In manifest mode the side has a **Raw / Form** toggle:

- **Raw** — Monaco JSON editor with inline schema validation and autocomplete
- **Form** — `@json-editor/json-editor` renders a form from the schema with
  typed inputs, enum dropdowns, descriptions, and nested array/object
  add-row buttons

Switching panes copies the data both ways, so you can start in the form,
swap to raw to paste a snippet, then back to the form. The Form button is
disabled for `map.json` (Mapbox-GL Style Spec — too complex for a useful
form) and `orbitals.json` (recursive `satellites` confuses generators).

## View controls (togglable groups + time slider)

The editor honours the `metadata.ofm` extensions in each world's `map.json`:

- **Togglable groups** — `metadata.ofm.togglable[]` renders as a checkbox
  per group in the side panel. Toggling hides/shows the named style layers
  via `setLayoutProperty(layerId, 'visibility')`. Optional `legend` arrays
  render as color swatches under the toggle (used by sta to display the
  star-class color key).
- **Time slider** — when `metadata.ofm.timeline` declares a `[min, max]`
  date range, a slider + date input appear above the map. Moving them
  substitutes `{atDate}` in any source URL that contains the template and
  calls `setData()` / `setTiles()` to re-fetch — so time-bounded layers
  (e.g. sta's `politics` source served with `?atDate={atDate}`) update
  live as you drag.

The map.json schema mirrors observed usage across the 73 worlds that have
one — `togglable` items require `label`/`name`/`layers` and optionally
carry `height`, `legend` (array of `[label, color]` pairs), `sources`.
`metadata.ofm.type` is an enum: `galaxy | map | starbase | world | system | deck`.
The full `sta/map.json` is the canonical reference; it exercises every
extension at once.

## Azgaar FMG GeoJSON importer

`tools/azgaar_import.py` ingests the 5-file GeoJSON export from Azgaar's
Fantasy Map Generator and produces a fully-furnished OFM world (`timeline.json`,
`gaia.json`, `map.json` with biome-categorical fill / river width-by-discharge /
emoji marker symbols / togglable groups, and per-layer JSON Schemas).

```bash
# Drop the GeoJSONs in /srv/ofm/<slug>/to_import/ (or anywhere), then:
docker run --rm \
    -v /srv/ofm:/ofm \
    -v /srv/ofm/raster-vectorizer/tools:/tools \
    python:3.12-slim python3 /tools/azgaar_import.py \
    --in /ofm/<slug>/to_import --slug <slug> --name "<Display Name>"
```

It detects layers by case-insensitive filename substring, so `Jarciland Cells
2026-05-18-10-44.geojson` resolves to `cells`. Recognised layer names:
`cells`, `routes`, `rivers`, `markers`, `zones`, `burgs` — any subset works,
but `cells` is required. The world's `base` lat/lng/zoom comes from the
data's bbox.

The resulting `map.json` includes 6 togglable groups out of the box:
**Biomes (cells)** with the Azgaar palette + legend, **States (politics)** as
a hidden-by-default alternate view that colours cells by `state` with
deterministic HSL hues, **Rivers** with `widthFactor`-driven line widths,
**Routes** colour-coded by `group` (roads/trails/searoutes), **Zones** as
translucent overlays using each zone's own `color` property, and **Markers**
that render the literal emoji `icon` field as text via MapLibre symbols.

## Unicode handling

OFM accepts arbitrary Unicode in feature properties (emoji marker icons,
non-Latin place names) and serialises it through a custom `UnicodeJSONResponse`
class — `json.dumps(ensure_ascii=False)` over the dict, then UTF-8 encode.
Lone surrogate halves (a real-world bug in some Azgaar exports of "ancient
inscription" markers) get scrubbed to U+FFFD before encoding so they don't
crash the encode step.

## What's not done yet

- **GeoTIFF support**: the tile route currently serves pre-baked tile pyramids.
  GeoTIFF rendering via `rio-tiler`/`titiler` is the next iteration.
- **`?storage=` override**: API currently follows `timeline.json#mode` strictly.
  Letting the user pick a non-default backend per-edit is on the roadmap.
- **Vision-assist**: the old `segment`/`palette` CLI lives in `tools/` for batch
  use; not yet surfaced as a "trace by color" button in the editor UI.
- **Schema-driven property forms**: the editor exposes raw key/value editing.
  Loading a per-layer schema (from `gaia.json` or a sibling file) for typed
  forms is on the roadmap.
- **Temporal (atDate)**: OFM supports temporal queries; the editor ignores
  the date dimension for now.
