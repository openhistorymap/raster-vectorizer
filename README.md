# raster-vectorizer

OFM-data-structure-aware web editor for vector layers. Each OFM world has a
raster (or GeoTIFF) basemap and a set of vector layers stored somewhere — this
tool lets you draw, edit, and save those layers on top of the basemap from a
browser, writing back to whatever storage the world's `timeline.json` declares.

```
┌─────────────────────────────────┐         ┌──────────────────────────────┐
│  frontend (Netlify-publishable) │ <─REST─>│  backend (FastAPI in Docker) │
│  MapLibre GL + Terra Draw       │         │   /srv/ofm mounted at /ofm   │
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
│   └── tests/                     pytest suite (API, adapters, PostGIS saves)
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

# build + run tests (starts a throwaway PostGIS 12 for the PostGIS save tests).
# Use a separate project name: `down` on the default project would also stop
# the deployed editor and frontend.
docker compose -p rv-test --profile test run --rm --build test
docker compose -p rv-test --profile test down -v

# launch the editor backend (bound to localhost:8765)
docker compose up --build editor
```

Environment variables (copy `.env.example` to `.env`, which is git-ignored;
`docker compose` refuses to start the editor without the two passwords):

| var                  | purpose                                          | default          |
|----------------------|--------------------------------------------------|------------------|
| `OFM_EDITOR_USER`    | HTTP basic auth user                             | required         |
| `OFM_EDITOR_PASSWORD`| HTTP basic auth password                         | required         |
| `OFM_EDITOR_OPEN`    | `1` = no auth, for local development only        | unset (closed)   |
| `OFM_PG_PASSWORD`    | password of `OFM_PG_USER` on the shared PostGIS  | required         |
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

All routes under `/api` require HTTP basic auth. Without configured credentials
the API refuses every request (503) unless `OFM_EDITOR_OPEN=1`.

| route                                       | method | purpose |
|---------------------------------------------|--------|---------|
| `/health`                                   | GET    | liveness |
| `/api/worlds`                               | GET    | list discovered worlds |
| `/api/worlds/{slug}`                        | GET    | timeline + gaia + map.json descriptor |
| `/api/worlds/{slug}/style`                  | GET    | the world's Mapbox-GL style |
| `/api/worlds/{slug}/layers`                 | GET    | layers known to the storage backend |
| `/api/worlds/{slug}/layers/{layer}`         | GET    | FeatureCollection |
| `/api/worlds/{slug}/layers/{layer}`         | PUT    | save layer (non-destructive, see below); returns what changed |
| `/api/worlds/{slug}/layers/{layer}`         | DELETE | drop the layer |
| `/api/worlds/{slug}/layers/{layer}/features` | POST  | add features (always inserts; the rest of the layer is untouched) |
| `/api/worlds/{slug}/sources`                | GET/PUT | the world's source registry (Cited GeoJSON `sources`), validated |
| `/api/worlds/{slug}/scans`                  | GET/POST | list / upload scanned maps (optionally registering the file under a source, with checksum) |
| `/api/worlds/{slug}/scans/{name}/image?max=N` | GET  | PNG preview; `X-Scale` header converts preview px to full-resolution px |
| `/api/worlds/{slug}/scans/{name}/georef/fit` | POST  | dry run: fit control points → accuracy, per-point errors, checks, footprint |
| `/api/worlds/{slug}/scans/{name}/georef`    | GET/PUT | the saved Georeference Annotation / fit and save (422 if a check fails) |
| `/api/worlds/{slug}/scans/{name}/warp`      | POST   | warp with the saved georeference into `cogs/<name>.tif` (a basemap) |
| `/api/assist`                               | GET    | which assistants are available (label reading: provider + model, never the key) |
| `/api/worlds/{slug}/scans/{name}/trace`     | POST   | flood-fill from `seed` → lon/lat polygon citing the traced pixels |
| `/api/worlds/{slug}/scans/{name}/read`      | POST   | read the labels in region `xywh` with the vision model → labels + a citation |
| `/api/worlds/{slug}/layers/{layer}/cited-geojson` | GET | the layer as a validated Cited GeoJSON document (422 + reasons if it cannot be) |
| `/api/worlds/{slug}/layers/{layer}/cited-geojson` | PUT | import a Cited GeoJSON document: merge its sources, save its features |
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

## Citations (Cited GeoJSON)

Features can cite their sources in the
[Cited GeoJSON](https://www.openhistorymap.org/cited-geojson/) format:

- **Source registry** — `<world>/raw/sources.json`, the same object as a Cited
  GeoJSON document's `sources` (keyed by source IRI; for OpenHistoryMap, Zotero
  item IRIs). Validated against the vendored 0.1 schema
  (`backend/schemas/cited-geojson-0.1.schema.json`).
- **Citations** travel as the feature-level `citations` member. PostGIS and
  SpatiaLite layers get a `citations` JSON column on the first save that needs
  it; GeoJSON files keep the member as is. Citations are versioned in the edit
  history like everything else. An absent `citations` member leaves stored ones
  alone; `[]` clears them.
- **Export** assembles a layer as a Cited GeoJSON document with only the
  sources it cites (plus their `derivedFrom` ancestors) and the georeferences of
  the scans it cites, and validates it (schema and cross-references) before
  returning it. **Import** merges a document's sources into the registry
  (existing entries win; differences are reported) and saves its features,
  keeping their ids.

## Georeferencing scans

Scanned maps are uploaded to `<world>/raw/scans/`. Control points (pixel ↔
lon/lat) are saved as an IIIF Georeference Annotation — the
[Allmaps](https://allmaps.org) format, and what Cited GeoJSON's `georeferences`
carries — in `<world>/raw/georef/<name>.json` (`backend/georef.py`).

- **Transformations:** polynomial of order 1–3 (least squares) or thin-plate
  spline, fitted like Allmaps from pixels to Web Mercator metres, with a
  separate backward model for warping.
- **Accuracy, honestly:** per-point residuals and a leave-one-out error (each
  point predicted by a model fitted without it), both in metres on the ground.
  Thin-plate splines interpolate exactly, so for them only the leave-one-out
  figure is published. The accuracy is stored in the annotation and travels
  into Cited GeoJSON exports.
- **Checks:** too few or collinear points, an image that maps outside valid
  coordinates, a size far from the declared `expectedWidthM` (the error that
  left starfleet-defiant's decks ~1 cm long), images a few centimetres across,
  a transformation that folds over itself, over-fitting, and whether the
  world's default view falls inside the image. Error-level checks block saving.
- **Warping** uses the same backward model as the reported accuracy and writes
  an EPSG:3857 tiled GeoTIFF with overviews to `<world>/cogs/<name>.tif`, which
  the raster discovery serves as a basemap.

## Georeferencing workbench (`georef.html`)

A second page next to the editor (header link *georeference ↗*), sharing its
sign-in:

- **Scan ↔ map side by side**: upload a scan (optionally registering it under a
  source), zoom and pan it, and click matching places on the scan and on the map
  (the world's style or OpenStreetMap) to add control points.
- **Live accuracy**: every change re-fits and shows the RMSE, the leave-one-out
  error, per-point errors (the worst point highlighted), the checks, and the
  image's footprint on the map. Save the georeference, then *Warp to basemap*.
- **Trace**: click inside a shape on the scan; the traced polygon appears on
  both sides and can be added to any layer (or a new one) with its citation.
- **Read labels**: drag a box around labels; the configured model transcribes
  them, and *use as name* names the traced feature and adds a `transcribed`
  citation of that region.

In the layer editor, the selected feature's panel lists its **citations**
(source, where, method, what they support) and can add or remove them using
the world's source registry. A save sends citations only for features whose
citations were edited, so nothing else is rewritten.

## Assisted tracing

- **Trace** (`backend/trace.py`) — plain computer vision: flood-fill from the
  clicked pixel within a colour tolerance, outline with holes, simplify, map
  through the scan's georeference. The polygon cites the exact pixels it came
  from (an `SvgSelector` with the outline and the bounding box as a
  `FragmentSelector`). Fills that leak through a gap are flagged.
- **Read labels** (`backend/llm.py`) — the region is cropped and sent to a
  vision model, which transcribes the labels as written (no translation or
  modernisation) and guesses what each names. The response includes a ready
  citation (`method: "transcribed"`) of that region, noting which model read it.
  Geometry never comes from the model.

The model is configured only by environment, through the standard
OpenAI-compatible `/chat/completions` API, so public and private models are
interchangeable:

| var | default | |
|---|---|---|
| `OFM_LLM_BASE_URL` | `https://openrouter.ai/api/v1` | any OpenAI-compatible endpoint (OpenRouter, OpenAI, vLLM, Ollama, LM Studio…) |
| `OFM_LLM_API_KEY` | unset | bearer key; optional for private servers |
| `OFM_LLM_MODEL` | `google/gemini-2.5-flash` | any vision-capable model id on that endpoint |
| `OFM_LLM_TIMEOUT` | `120` | seconds |

Label reading is enabled when a key is set, or when the base URL points
somewhere other than OpenRouter. The key stays on the server; `/api/assist`
only reports the provider host and the model.

## Saving: non-destructive, with history

A save never rewrites the layer wholesale (`backend/storage/diff.py`):

- **Stable ids.** Every feature has a permanent `fid` (uuid). Older tables get a
  `fid` column, backfilled, on their first save; until then they load with
  `row:<pk>` ids, so merely reading a layer never alters it.
- **Only what changed is written.** Incoming features are matched by id:
  changed ones are updated, new ones inserted, missing ones deleted, identical
  ones left alone. A property absent from a feature leaves the stored value
  alone; an explicit `null` clears it.
- **Every column is kept.** Properties go to the column of the same name (so
  `from_time`, `to_time`, `subclass`, `wiki`, … survive), otherwise into the
  `properties` JSON column when there is one. Anything with no place to go is
  returned as `unstored_attributes` and shown in the editor instead of being
  dropped.
- **All or nothing.** The save is one transaction; a feature whose geometry
  does not fit the layer (a line in a polygon layer) rejects the whole save with
  HTTP 422. Single geometries are promoted to Multi when the column is Multi, and
  Z/M coordinates are dropped (layers are XY).
- **History.** Every insert, update and delete is recorded with full before and
  after images and the editor's user name: in an `_ofm_history` table (PostGIS,
  SpatiaLite) or `raw/history/<layer>.jsonl` (GeoJSON files).

The PUT response reports `inserted`, `updated`, `deleted`, `unchanged` and
`unstored_attributes`. The editor sends feature ids back, rejoins the parts of
multi-part features it split for editing, and reloads after saving so new
features pick up their permanent ids.

Before this, saving truncated the table and re-inserted only `name`, `class`
and `properties`: on PostGIS/SpatiaLite layers that keep attributes as columns
one save erased them. `backend/tests/test_save_nondestructive.py` is the
regression suite for that.

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

- **`?storage=` override**: API currently follows `timeline.json#mode` strictly.
  Letting the user pick a non-default backend per-edit is on the roadmap.
- **Line tracing**: click-to-trace fills areas; roads, rivers and walls drawn as
  lines on a scan are not yet traced to centre lines.
- **Citation editing** in the layer editor covers source, method, page, quote
  and `supports`; section and image-region selectors come from the
  georeferencing workbench only.
- **Temporal (atDate)**: OFM supports temporal queries; the editor ignores
  the date dimension for now.
