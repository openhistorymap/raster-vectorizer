// OFM editor — MapLibre map editing + Monaco JSON manifest editing,
// driven by a single FastAPI backend.

const API = window.OFM_API_URL;

// --- auth: sessionStorage-backed basic auth with login overlay ---------

const CREDS_KEY = "ofm.editor.creds";

function getCreds() {
  // Optionally seeded by config.js for dev convenience.
  if (window.OFM_API_USER && window.OFM_API_PASS) {
    return { user: window.OFM_API_USER, pass: window.OFM_API_PASS };
  }
  try {
    const raw = sessionStorage.getItem(CREDS_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
}

function setCreds(user, pass) {
  sessionStorage.setItem(CREDS_KEY, JSON.stringify({ user, pass }));
}

function clearCreds() {
  sessionStorage.removeItem(CREDS_KEY);
}

function authHeader() {
  const c = getCreds();
  return c ? "Basic " + btoa(`${c.user}:${c.pass}`) : null;
}

function showLogin(errMsg) {
  const ov = document.getElementById("login-overlay");
  const err = document.getElementById("login-error");
  ov.hidden = false;
  if (errMsg) {
    err.textContent = errMsg;
    err.hidden = false;
  } else {
    err.hidden = true;
  }
  document.getElementById("login-user").focus();
}

function hideLogin() {
  document.getElementById("login-overlay").hidden = true;
}

document.getElementById("login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const user = document.getElementById("login-user").value.trim();
  const pass = document.getElementById("login-pass").value;
  setCreds(user, pass);
  // Verify against backend before dismissing.
  try {
    const r = await fetch(`${API}/api/worlds`, {
      headers: { Authorization: authHeader() },
    });
    if (r.status === 401) {
      clearCreds();
      showLogin("Wrong credentials.");
      return;
    }
    if (!r.ok) {
      clearCreds();
      showLogin(`Backend error: ${r.status} ${r.statusText}`);
      return;
    }
    hideLogin();
    document.getElementById("login-pass").value = "";
    init().catch((e) => setStatus(e.message, "bad"));
  } catch (err) {
    clearCreds();
    showLogin(`Network error: ${err.message}`);
  }
});

function fetchJSON(path, opts = {}) {
  const headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
  const auth = authHeader();
  if (auth) headers.Authorization = auth;
  return fetch(`${API}${path}`, { ...opts, headers }).then(async (r) => {
    if (r.status === 401) {
      clearCreds();
      showLogin("Session expired — sign in again.");
      const err = new Error("unauthenticated");
      err.status = 401;
      throw err;
    }
    if (!r.ok) {
      let body = null;
      try { body = await r.json(); } catch {}
      const err = new Error(`${r.status} ${r.statusText} on ${path}`);
      err.status = r.status;
      err.body = body;
      throw err;
    }
    return r.status === 204 ? null : r.json();
  });
}

// --- DOM ---------------------------------------------------------------
const $world = document.getElementById("world-select");
const $layer = document.getElementById("layer-select");
const $basemap = document.getElementById("basemap-select");
const $save = document.getElementById("save-btn");
const $reload = document.getElementById("reload-btn");
const $newLayer = document.getElementById("new-layer-btn");
const $status = document.getElementById("status");
const $info = document.getElementById("layer-info");
const $props = document.getElementById("props");
const $mapView = document.getElementById("map-view");
const $manifestView = document.getElementById("manifest-view");
const $manifestTitle = document.getElementById("manifest-title");
const $manifestErrors = document.getElementById("manifest-errors");
const $manifestSaveInfo = document.getElementById("manifest-save-info");
const $manifestEditor = document.getElementById("manifest-editor");
const $manifestForm = document.getElementById("manifest-form");
const $manifestFormBtn = document.getElementById("manifest-form-btn");
const $modeBtns = [...document.querySelectorAll(".mode-btn")];
const $paneBtns = [...document.querySelectorAll(".pane-btn")];
const $addFieldBtn = document.getElementById("add-field-btn");
const $addFeatureBtn = document.getElementById("add-feature-btn");
const $schemaSummary = document.getElementById("schema-summary");
const $togglables = document.getElementById("togglables");
const $timeBar = document.getElementById("time-bar");
const $timeInput = document.getElementById("time-input");
const $timeSlider = document.getElementById("time-slider");
const $timeReadout = document.getElementById("time-readout");

// Kinds that have a usable form view (the others — map.json + orbitals.json —
// are too complex/recursive for a generated form; raw-only).
const FORM_SUPPORTED = new Set(["timeline", "gaia", "render"]);

// --- URL state ------------------------------------------------------------
// ?world=…&mode=…&layer=… reflects the current selection so reloads and
// bookmarks restore the same view.

function readUrlState() {
  const p = new URLSearchParams(window.location.search);
  return {
    world: p.get("world"),
    mode:  p.get("mode"),    // "map" | "timeline" | "gaia" | "map_json" | "render" | "orbitals"
    layer: p.get("layer"),
  };
}

function writeUrlState(opts = {}) {
  const params = new URLSearchParams();
  if (currentWorld) params.set("world", currentWorld);
  if (currentMode && currentMode !== "map") params.set("mode", currentMode);
  if (currentMode === "map" && currentLayer) params.set("layer", currentLayer);
  const qs = params.toString();
  const url = qs ? `?${qs}` : window.location.pathname;
  if (opts.replace) {
    history.replaceState(null, "", url);
  } else {
    // Avoid filling history with no-op pushes.
    if (window.location.search !== `?${qs}` && window.location.search !== url) {
      history.pushState(null, "", url);
    }
  }
}

window.addEventListener("popstate", async () => {
  const s = readUrlState();
  // Apply without re-pushing.
  if (s.world && s.world !== currentWorld) {
    $world.value = s.world;
    await onWorldChange(s.world, { skipUrl: true });
  }
  if (s.mode && s.mode !== currentMode) await switchMode(s.mode, { skipUrl: true });
  if (s.layer && s.layer !== currentLayer && currentMode === "map") {
    $layer.value = s.layer;
    await loadLayer(s.layer);
  }
});

// kind selector for the manifest mode — uses the data-view attr
const MANIFEST_KINDS = {
  timeline: "timeline",
  gaia:     "gaia",
  map_json: "map",
  render:   "render",
  orbitals: "orbitals",
};

let map = null;
let draw = null;
let monacoEditor = null;
let monacoReady = false;
let jsonEditor = null;
let manifestSchema = null;       // schema for the currently-open manifest
let currentManifestKind = null;  // "timeline" | "gaia" | etc.
let currentWorld = null;
let currentLayer = null;
let currentLayerSchema = null;   // per-layer feature-properties schema
let currentMode = "map";   // "map" or one of MANIFEST_KINDS keys
let currentPane = "raw";   // "raw" or "form" — only meaningful in manifest mode
let dirty = false;

function setStatus(msg, level = "muted") {
  $status.textContent = msg || "";
  $status.style.color =
    level === "bad" ? "var(--danger)" :
    level === "ok"  ? "var(--ok)" :
                       "var(--muted)";
}

function setDirty(d) {
  dirty = d;
  $save.disabled = !d;
}

window.addEventListener("beforeunload", (e) => {
  if (dirty) { e.preventDefault(); e.returnValue = ""; }
});

// --- mode switching ----------------------------------------------------

$modeBtns.forEach((btn) => {
  btn.addEventListener("click", () => switchMode(btn.dataset.view));
});

async function switchMode(view, opts = {}) {
  if (dirty && !confirm("Discard unsaved changes?")) return;
  setDirty(false);
  currentMode = view;
  $modeBtns.forEach((b) => b.classList.toggle("active", b.dataset.view === view));
  if (view === "map") {
    document.body.classList.remove("mode-manifest");
    $mapView.classList.add("active");
    $manifestView.classList.remove("active");
    if (currentWorld) await loadWorldMap(currentWorld);
  } else {
    document.body.classList.add("mode-manifest");
    $manifestView.classList.add("active");
    $mapView.classList.remove("active");
    await loadManifest(currentWorld, MANIFEST_KINDS[view]);
  }
  if (!opts.skipUrl) writeUrlState({ replace: true });
}

// --- world picker ------------------------------------------------------

async function init() {
  setStatus(`backend: ${API}`);
  const worlds = await fetchJSON("/api/worlds");
  worlds.sort((a, b) => a.name.localeCompare(b.name));
  $world.innerHTML = worlds
    .map((w) => `<option value="${w.slug}">${w.name} (${w.mode})</option>`)
    .join("");
  $world.addEventListener("change", () => onWorldChange($world.value));

  // Honour URL state on first load: ?world=…&mode=…&layer=…
  const urlState = readUrlState();
  const initialSlug = (urlState.world && worlds.find((w) => w.slug === urlState.world))
    ? urlState.world
    : worlds[0]?.slug;
  if (initialSlug) {
    $world.value = initialSlug;
    await onWorldChange(initialSlug, { skipUrl: true });
    if (urlState.mode && urlState.mode !== "map") {
      await switchMode(urlState.mode, { skipUrl: true });
    }
    if (urlState.layer && currentMode === "map") {
      $layer.value = urlState.layer;
      await loadLayer(urlState.layer);
    }
    writeUrlState({ replace: true });  // canonicalise (drops unknowns)
  }
}

async function onWorldChange(slug, opts = {}) {
  currentWorld = slug;
  if (currentMode === "map") await loadWorldMap(slug);
  else await loadManifest(slug, MANIFEST_KINDS[currentMode]);
  if (!opts.skipUrl) writeUrlState();  // push — user can back-button to previous world
}

function onLayerSelectChange(e) {
  // Layer dropdown handler. Stable top-level reference so addEventListener
  // can be safely add/removed across loadWorldMap calls. The status update
  // makes "did the change event actually fire?" diagnosable from the UI.
  const v = $layer.value;
  setStatus(`layer change → ${v}`);
  loadLayer(v);
}

// --- map mode ----------------------------------------------------------

let currentRasters = [];
let currentBasemap = null;
let currentStyle = null;        // the rewritten Mapbox-GL style currently loaded
let templatedSources = [];      // [{id, urlTemplate, kind: 'data'|'tiles'}] for {atDate} sources
let currentAtDate = null;       // ISO date string driving timed sources

// Feature state owned by us (NOT terra-draw). Terra Draw is only the drawing
// tool — when the user finishes drawing, the new feature migrates into here.
// Display + selection + property editing all flow through this state, so
// click handling and saves don't depend on whatever helper features Terra
// Draw has in its own snapshot at the moment.
let loadedFeatures = [];        // [{type:'Feature', id, geometry, properties}]
let editedProperties = new Map(); // id -> partial properties (overrides)
let selectedFeatureId = null;
let nextLocalId = 1;

async function loadWorldMap(slug) {
  setStatus(`loading world ${slug}…`);
  const [info, layers, rasters] = await Promise.all([
    fetchJSON(`/api/worlds/${slug}`),
    fetchJSON(`/api/worlds/${slug}/layers`).catch((e) => {
      setStatus(`layers unavailable: ${e.body?.detail || e.message}`, "bad");
      return [];
    }),
    fetchJSON(`/api/worlds/${slug}/rasters`).catch(() => []),
  ]);
  currentRasters = rasters;
  $basemap.innerHTML =
    `<option value="">(map.json default)</option>` +
    rasters
      .map((r) => `<option value="${r.name}">${r.name} (${r.kind}, z${r.min_zoom}–${r.max_zoom})</option>`)
      .join("");
  // pick the first raster as default if map.json doesn't reference statictiles
  currentBasemap = rasters[0]?.name || null;
  $basemap.value = currentBasemap || "";
  $basemap.onchange = () => {
    currentBasemap = $basemap.value || null;
    loadWorldMap(slug);  // simplest: rebuild the map with the new basemap
  };

  const style = await fetchJSON(`/api/worlds/${slug}/style`).catch(() => null);
  const safeStyle = style || {
    version: 8, sources: {},
    layers: [{ id: "bg", type: "background", paint: { "background-color": "#1a1f2b" } }],
  };
  rewriteStyleUrls(safeStyle, slug);
  if (currentBasemap) injectBasemap(safeStyle, slug, currentBasemap, info);
  currentStyle = safeStyle;

  // Capture {atDate}-templated sources BEFORE substitution — the slider needs
  // the original template to re-substitute as the user drags.
  templatedSources = [];
  for (const [id, s] of Object.entries(safeStyle.sources || {})) {
    if (typeof s.data === "string" && s.data.includes("{atDate}")) {
      templatedSources.push({ id, urlTemplate: s.data, kind: "data" });
    }
    if (Array.isArray(s.tiles) && s.tiles.some((t) => t.includes("{atDate}"))) {
      templatedSources.push({ id, urlTemplate: [...s.tiles], kind: "tiles" });
    }
  }

  // Substitute templates with an initial atDate so MapLibre's first fetch
  // doesn't fire with the literal {atDate} in the URL.
  currentAtDate = initialAtDateFromStyle(safeStyle);
  applyAtDateToStyle(safeStyle, currentAtDate);

  if (map) map.remove();
  map = new maplibregl.Map({
    container: "map",
    style: safeStyle,
    center: info.timeline?.base ? [info.timeline.base.lng, info.timeline.base.lat] : [0, 0],
    zoom: info.timeline?.base?.zoom ?? 3,
    // MapLibre fetches tiles + GeoJSON sources itself (not via our fetchJSON
    // wrapper), so basic-auth wouldn't reach the backend without this hook.
    // We attach the Authorization header to any request pointed at API.
    transformRequest: (url) => {
      if (!url.startsWith(API)) return { url };
      const auth = authHeader();
      return auth ? { url, headers: { Authorization: auth } } : { url };
    },
  });
  map.addControl(new maplibregl.NavigationControl(), "top-right");

  // Terra Draw — modern, MapLibre-native replacement for mapbox-gl-draw.
  // Lazy-init after map.load to ensure the style is attached first.
  map.once("load", () => {
    initTerraDraw();
  });

  // Layer dropdown is informed by map.json#sources (the canonical layers of
  // this world) unioned with whatever storage actually contains. A source
  // declared in map.json but empty in storage is a real, addable layer; a
  // table in storage not referenced by map.json is a legitimate edit target
  // too (just unstyled — features still load into the draw tool).
  const styleLayers = (safeStyle.layers || []);
  const layerOptions = mergeLayerSources(safeStyle.sources || {}, layers, styleLayers);
  $layer.innerHTML = layerOptions
    .map((o) => `<option value="${o.name}">${o.label}</option>`)
    .join("");
  // Use a stable single handler — replace prior binding by removing then
  // re-adding (addEventListener stacks, so previous loadWorldMap calls would
  // pile up handlers and fire loadLayer multiple times per change).
  if (onLayerSelectChange.__bound) {
    $layer.removeEventListener("change", onLayerSelectChange);
  }
  $layer.addEventListener("change", onLayerSelectChange);
  onLayerSelectChange.__bound = true;
  if (layerOptions.length) await loadLayer(layerOptions[0].name);
  else { $info.textContent = "(no layers yet — click + layer)"; currentLayer = null; }

  // Wire metadata.ofm features (togglable groups + time slider) once the map
  // is ready. We need to wait for the style 'load' event before manipulating
  // layer visibility.
  map.once("load", () => setupViewControls(safeStyle));
  setStatus(`world: ${slug}`);
}

// --- view controls: togglable groups + time slider --------------------------

function setupViewControls(style) {
  const ofm = style?.metadata?.ofm || {};
  renderTogglables(ofm.togglable || []);
  setupTimeBar(ofm.timeline);
}

function renderTogglables(groups) {
  $togglables.innerHTML = "";
  if (!groups.length) {
    $togglables.className = "hint";
    $togglables.textContent = "(none defined in map.json)";
    return;
  }
  $togglables.className = "";
  for (const g of groups) {
    const wrap = document.createElement("div");
    wrap.className = "toggle-group";

    const lbl = document.createElement("label");
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = true;  // default visible — matches current layer visibility
    cb.addEventListener("change", () => {
      for (const layerId of g.layers || []) {
        if (!map.getLayer(layerId)) continue;
        map.setLayoutProperty(layerId, "visibility", cb.checked ? "visible" : "none");
      }
    });
    const txt = document.createElement("span");
    txt.textContent = g.label || g.name;
    lbl.appendChild(cb);
    lbl.appendChild(txt);
    wrap.appendChild(lbl);

    const meta = document.createElement("div");
    meta.className = "group-meta";
    meta.textContent = `${(g.layers || []).length} layer${(g.layers || []).length === 1 ? "" : "s"}`;
    wrap.appendChild(meta);

    if (Array.isArray(g.legend) && g.legend.length) {
      const legend = document.createElement("div");
      legend.className = "legend";
      for (const entry of g.legend) {
        if (!Array.isArray(entry) || entry.length < 2) continue;
        const [label, color] = entry;
        const sw = document.createElement("span");
        sw.className = "swatch";
        sw.style.setProperty("--swatch", color);
        sw.textContent = label;
        legend.appendChild(sw);
      }
      wrap.appendChild(legend);
    }

    $togglables.appendChild(wrap);
  }
}

function setupTimeBar(timeline) {
  if (!Array.isArray(timeline) || timeline.length < 2) {
    $timeBar.hidden = true;
    return;
  }
  const [min, max] = timeline;
  const minD = parseDate(min);
  const maxD = parseDate(max);
  if (!minD || !maxD || minD > maxD) {
    $timeBar.hidden = true;
    return;
  }
  $timeBar.hidden = false;
  // HTML date input handles BC/AD natively only via "year-month-day" with a
  // year that's >=0001; for our purposes we use a numeric slider and a date
  // picker as a convenience for plain present-era ranges.
  $timeSlider.min = String(Math.floor(minD.getTime() / 86400000));
  $timeSlider.max = String(Math.floor(maxD.getTime() / 86400000));
  const initial = currentAtDate
    ? parseDate(currentAtDate)
    : new Date(minD.getTime() + (maxD.getTime() - minD.getTime()) / 2);
  $timeSlider.value = String(Math.floor(initial.getTime() / 86400000));
  syncDateInputs(initial);
  $timeSlider.oninput = () => {
    const d = new Date(parseInt($timeSlider.value, 10) * 86400000);
    syncDateInputs(d);
    setAtDate(toIsoDate(d));
  };
  $timeInput.onchange = () => {
    const d = new Date($timeInput.value);
    if (!isNaN(d)) {
      $timeSlider.value = String(Math.floor(d.getTime() / 86400000));
      $timeReadout.textContent = toIsoDate(d);
      setAtDate(toIsoDate(d));
    }
  };
  setAtDate(toIsoDate(initial));
}

function parseDate(s) {
  // Tolerate both "1995-06-15" and "-200000-01-01".
  if (typeof s !== "string") return null;
  const m = s.match(/^(-?\d+)-(\d{2})-(\d{2})/);
  if (!m) return null;
  const d = new Date(Date.UTC(+m[1], +m[2] - 1, +m[3]));
  return isNaN(d) ? null : d;
}

function toIsoDate(d) {
  const y = d.getUTCFullYear();
  const m = String(d.getUTCMonth() + 1).padStart(2, "0");
  const day = String(d.getUTCDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

function syncDateInputs(d) {
  $timeReadout.textContent = toIsoDate(d);
  // Native <input type="date"> rejects negative years; only set if in range.
  if (d.getUTCFullYear() >= 1 && d.getUTCFullYear() <= 9999) {
    $timeInput.valueAsDate = d;
  }
}

function setAtDate(isoDate) {
  currentAtDate = isoDate;
  if (!templatedSources.length || !map) return;
  for (const t of templatedSources) {
    const src = map.getSource(t.id);
    if (!src) continue;
    if (t.kind === "data" && src.setData) {
      const url = t.urlTemplate.replace("{atDate}", encodeURIComponent(isoDate));
      src.setData(url);
    } else if (t.kind === "tiles" && src.setTiles) {
      const urls = t.urlTemplate.map((u) => u.replace("{atDate}", encodeURIComponent(isoDate)));
      src.setTiles(urls);
    }
  }
}

function injectBasemap(style, slug, basemap, info) {
  // Replace whatever raster source map.json declared with one pointing at
  // /api/worlds/<slug>/rasters/<basemap>/tiles/... using the source's native
  // extension. Inserts the layer at the bottom so vector layers stay on top.
  const src = currentRasters.find((r) => r.name === basemap);
  if (!src) return;
  const ext = src.ext || "png";
  style.sources = style.sources || {};
  style.sources["ofm-basemap"] = {
    type: "raster",
    tiles: [`${API}/api/worlds/${slug}/rasters/${basemap}/tiles/{z}/{x}/{y}.${ext}`],
    tileSize: 256,
    minzoom: src.min_zoom ?? 0,
    maxzoom: src.max_zoom ?? 22,
    attribution: `OFM/${basemap}`,
  };
  // remove any existing raster layers that target sources we just replaced
  style.layers = (style.layers || []).filter((l) => l.type !== "raster");
  // background → first, basemap → second, rest follow
  const bg = style.layers.find((l) => l.type === "background");
  const rest = style.layers.filter((l) => l.type !== "background");
  style.layers = [
    bg || { id: "bg", type: "background", paint: { "background-color": "#1a1f2b" } },
    {
      id: "ofm-basemap",
      type: "raster",
      source: "ofm-basemap",
      minzoom: src.min_zoom ?? 0,
      maxzoom: src.max_zoom ?? 22,
    },
    ...rest,
  ];
}

function mergeLayerSources(sources, storageLayers, styleLayers) {
  // Returns an ordered list [{name, label}] suitable for the Layer dropdown.
  // Sources declared in map.json come first (they are the canonical model);
  // storage-only layers follow. Each label shows feature count + how many
  // style layers render this source.
  const declared = [];
  const seen = new Set();
  const storageBy = Object.fromEntries(storageLayers.map((l) => [l.name, l]));
  const rendererCount = new Map();
  for (const sl of styleLayers) {
    if (!sl.source) continue;
    rendererCount.set(sl.source, (rendererCount.get(sl.source) || 0) + 1);
  }
  for (const [name, src] of Object.entries(sources)) {
    if (src.type && src.type !== "geojson") continue;  // raster/vector tiles aren't editable here
    seen.add(name);
    const st = storageBy[name];
    const count = st?.count ?? 0;
    const gt = st?.geometry_type ?? "?";
    const rn = rendererCount.get(name) || 0;
    declared.push({
      name,
      label: `${name} (${count}, ${gt}${rn ? `, ${rn} styled` : ""})`,
    });
  }
  // anything in storage that map.json doesn't declare
  for (const l of storageLayers) {
    if (seen.has(l.name)) continue;
    declared.push({
      name: l.name,
      label: `${l.name} (${l.count}, ${l.geometry_type}, ⚠ not in map.json)`,
    });
  }
  return declared;
}

function rewriteStyleUrls(style, slug) {
  // Retarget statictiles.fantasymaps.org/<slug>/... URLs at the editor backend.
  // Preserve any query string (notably ?atDate={atDate}) so time-aware sources
  // keep working after rewrite.
  const re = new RegExp(`https?://statictiles\\.fantasymaps\\.org/${slug}/`);
  for (const k of Object.keys(style.sources || {})) {
    const s = style.sources[k];
    if (typeof s.data === "string" && re.test(s.data)) {
      const after = s.data.replace(re, "");
      const qIdx = after.indexOf("?");
      const path = qIdx === -1 ? after : after.slice(0, qIdx);
      const qs   = qIdx === -1 ? "" : after.slice(qIdx);
      const layer = path.replace(/\.geojson$/, "");
      s.data = `${API}/api/worlds/${slug}/layers/${layer}${qs}`;
    }
    if (Array.isArray(s.tiles)) {
      s.tiles = s.tiles.map((t) =>
        re.test(t)
          ? `${API}/api/worlds/${slug}/tiles/{z}/{x}/{y}.${t.match(/\.(\w+)$/)?.[1] || "jpg"}`
          : t,
      );
    }
  }
}

function initialAtDateFromStyle(style) {
  // Pick a sensible initial value for {atDate} substitution BEFORE MapLibre
  // starts fetching sources. Without this, the first fetch fires with the
  // template still literal and the backend 503s on an unknown layer name.
  const tl = style?.metadata?.ofm?.timeline;
  if (Array.isArray(tl) && tl.length >= 2) {
    const a = parseDate(tl[0]);
    const b = parseDate(tl[1]);
    if (a && b) return toIsoDate(new Date((a.getTime() + b.getTime()) / 2));
  }
  const t = style?.metadata?.ofm?.time;
  if (typeof t === "string") {
    const d = parseDate(t);
    if (d) return toIsoDate(d);
  }
  return toIsoDate(new Date());
}

function applyAtDateToStyle(style, isoDate) {
  // Substitute {atDate} in any source URL up front. MapLibre never sees the
  // literal template; the time slider replaces it later via setData().
  const enc = encodeURIComponent(isoDate);
  for (const s of Object.values(style.sources || {})) {
    if (typeof s.data === "string" && s.data.includes("{atDate}")) {
      s.data = s.data.replace("{atDate}", enc);
    }
    if (Array.isArray(s.tiles)) {
      s.tiles = s.tiles.map((t) => t.replace("{atDate}", enc));
    }
  }
}

async function loadLayer(name) {
  if (!currentWorld) return;
  // Restore visibility of layers we hid for the previously-edited source.
  restoreHiddenLayers();

  currentLayer = name;
  writeUrlState({ replace: true });  // keep URL in sync with selected layer
  setStatus(`loading layer ${name}…`);
  const [fc, schema] = await Promise.all([
    fetchJSON(`/api/worlds/${currentWorld}/layers/${name}`),
    fetchJSON(`/api/worlds/${currentWorld}/layers/${name}/schema`).catch(() => ({
      type: "object", properties: {},
    })),
  ]);
  currentLayerSchema = schema;
  // Schema may have explicit geometryType; otherwise infer from first feature.
  if (!schema.geometryType && fc.features?.[0]?.geometry?.type) {
    currentLayerSchema.geometryType = fc.features[0].geometry.type;
  }
  loadFeaturesIntoState(fc);
  refreshAddFeatureBtn();

  // Hide any map.json style layer that renders this source — Terra Draw
  // already shows the features on top, and seeing both is confusing.
  hideStyleLayersForSource(name);

  const rendered = (currentStyle?.layers || [])
    .filter((l) => l.source === name)
    .map((l) => `${l.id} (${l.type})`);
  $info.textContent = JSON.stringify(
    { name, count: fc.features?.length || 0, rendered_by: rendered },
    null,
    2,
  );
  renderSchemaSummary();
  setDirty(false);
  setStatus(`layer: ${name}`);
}

// Track layers we hide so we can restore them when the user switches layers.
let hiddenLayerIds = [];

function hideStyleLayersForSource(sourceName) {
  if (!map) return;
  const run = () => {
    const targets = (currentStyle?.layers || [])
      .filter((l) => l.source === sourceName)
      .map((l) => l.id);
    for (const id of targets) {
      if (map.getLayer(id)) {
        map.setLayoutProperty(id, "visibility", "none");
        hiddenLayerIds.push(id);
      }
    }
  };
  // If we're called before MapLibre has finished loading the style, defer.
  if (map.isStyleLoaded()) run();
  else map.once("load", run);
}

function restoreHiddenLayers() {
  if (!map) { hiddenLayerIds = []; return; }
  for (const id of hiddenLayerIds) {
    if (map.getLayer(id)) map.setLayoutProperty(id, "visibility", "visible");
  }
  hiddenLayerIds = [];
}

function renderSchemaSummary() {
  const props = currentLayerSchema?.properties || {};
  const keys = Object.keys(props);
  if (keys.length === 0) {
    $schemaSummary.textContent =
      "(no schema yet — features can have any properties; click \"+ field\" to start defining one)";
    return;
  }
  const required = new Set(currentLayerSchema.required || []);
  const lines = keys.map((k) => {
    const p = props[k];
    const t = p.type || (p.enum ? "enum" : "any");
    const enums = p.enum ? ` ∈ {${p.enum.slice(0, 4).join(", ")}${p.enum.length > 4 ? ", …" : ""}}` : "";
    const req = required.has(k) ? "*" : " ";
    return `${req} ${k}: ${t}${enums}`;
  });
  $schemaSummary.textContent = lines.join("\n");
}

function onSelectionChange(e) {
  const f = e.features?.[0];
  if (!f) { $props.textContent = "select a feature to edit its properties"; return; }
  const props = { ...(f.properties || {}) };
  const schemaProps = currentLayerSchema?.properties || {};
  // Union of schema-declared fields + ad-hoc fields on the feature, so the
  // user sees everything they've got — but inputs are typed per schema where
  // a definition exists.
  const allKeys = Array.from(new Set([...Object.keys(schemaProps), ...Object.keys(props)]));
  $props.innerHTML = "";
  for (const k of allKeys) {
    const defn = schemaProps[k] || {};
    const v = props[k];
    const row = document.createElement("div");
    row.className = "row";
    const label = document.createElement("label");
    label.textContent = k + (defn.description ? " ⓘ" : "");
    if (defn.description) label.title = defn.description;
    const ctrl = makeInput(defn, v);
    ctrl.dataset.key = k;
    row.appendChild(label);
    row.appendChild(ctrl);
    $props.appendChild(row);
  }
  $props.querySelectorAll("[data-key]").forEach((el) => {
    el.addEventListener("change", () => {
      const k = el.dataset.key;
      const defn = schemaProps[k] || {};
      let val;
      if (el.type === "checkbox") val = el.checked;
      else if (defn.type === "integer") val = el.value === "" ? null : parseInt(el.value, 10);
      else if (defn.type === "number")  val = el.value === "" ? null : parseFloat(el.value);
      else val = el.value;
      // Write into editedProperties (our own state, not terra-draw).
      const cur = editedProperties.get(String(f.id)) || {};
      cur[k] = val;
      editedProperties.set(String(f.id), cur);
      refreshFeatureSource();
      setDirty(true);
    });
  });
}

function initTerraDraw() {
  if (!window.terraDraw || !window.terraDrawMaplibreGlAdapter) {
    setStatus("Terra Draw failed to load (CDN issue?)", "bad");
    return;
  }
  const TD = window.terraDraw;
  const adapter = new window.terraDrawMaplibreGlAdapter.TerraDrawMapLibreGLAdapter({
    map, lib: maplibregl,
  });

  // Smaller than default (40px) so accidental polygon-close / close-line near
  // the first vertex stops happening on a slightly imprecise click.
  const POINTER_DISTANCE = 10;

  // Custom snap function — pulls candidate vertices from our editing source
  // (Terra Draw doesn't know about it natively). Returns the snapped [lng,lat]
  // or null. Threshold in pixels: same as POINTER_DISTANCE + a touch more.
  const SNAP_PX = 14;
  const snapToOurFeatures = (event) => {
    if (!map) return null;
    const cursorPx = (event && typeof event.containerX === "number")
      ? { x: event.containerX, y: event.containerY }
      : (event && typeof event.lng === "number"
          ? map.project([event.lng, event.lat])
          : null);
    if (!cursorPx) return null;
    const bbox = [
      [cursorPx.x - SNAP_PX, cursorPx.y - SNAP_PX],
      [cursorPx.x + SNAP_PX, cursorPx.y + SNAP_PX],
    ];
    const presentLayers = [
      "_ofm_features_fill", "_ofm_features_line",
      "_ofm_features_circle", "_ofm_features_outline",
    ].filter((id) => map.getLayer(id));
    const nearby = map.queryRenderedFeatures(bbox, { layers: presentLayers });
    if (!nearby.length) return null;
    let best = null;
    let bestDist = Infinity;
    for (const f of nearby) {
      for (const c of allVertices(f.geometry)) {
        const p = map.project(c);
        const d = Math.hypot(p.x - cursorPx.x, p.y - cursorPx.y);
        if (d < bestDist && d <= SNAP_PX) {
          bestDist = d;
          best = c;
        }
      }
    }
    return best;
  };

  const snappingCfg = { toCustom: snapToOurFeatures };

  draw = new TD.TerraDraw({
    adapter,
    modes: [
      new TD.TerraDrawSelectMode({
        flags: {
          polygon:    { feature: { draggable: true, coordinates: { midpoints: true, draggable: true, deletable: true } } },
          linestring: { feature: { draggable: true, coordinates: { midpoints: true, draggable: true, deletable: true } } },
          point:      { feature: { draggable: true } },
        },
        pointerDistance: POINTER_DISTANCE,
      }),
      new TD.TerraDrawPolygonMode({ snapping: snappingCfg, pointerDistance: POINTER_DISTANCE }),
      new TD.TerraDrawLineStringMode({ snapping: snappingCfg, pointerDistance: POINTER_DISTANCE }),
      new TD.TerraDrawPointMode({ snapping: snappingCfg, pointerDistance: POINTER_DISTANCE }),
    ],
  });
  draw.start();
  draw.setMode("select");

  // When the user finishes drawing a feature (closes a polygon, dbl-clicks a
  // line, single-clicks a point), seed it with schema defaults, switch to
  // select mode, surface property form.
  draw.on("finish", (id) => onFinishDraw(id));
  // Any change (drag, vertex move, etc.) dirties the editor.
  draw.on("change", (_ids, type) => {
    if (type !== "styling") setDirty(true);
  });
  draw.on("select", (id) => onTerraSelect(id));
  draw.on("deselect", () => $props.textContent = "select a feature to edit its properties");

  // If a layer was loaded before draw initialised, refresh display now.
  refreshFeatureSource();
}

function uuidv4() {
  // Terra Draw's default idStrategy requires RFC 4122 UUIDs. Modern browsers
  // expose crypto.randomUUID(); fall back to a tiny implementation otherwise.
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0;
    const v = c === "x" ? r : (r & 0x3) | 0x8;
    return v.toString(16);
  });
}

function modeFor(geomType) {
  return {
    Point: "point", MultiPoint: "point",
    LineString: "linestring", MultiLineString: "linestring",
    Polygon: "polygon", MultiPolygon: "polygon",
  }[geomType] || null;
}

function loadFeaturesIntoState(fc) {
  loadedFeatures = (fc.features || [])
    .filter((f) => f.geometry?.coordinates)
    .flatMap(flattenMulti)
    .map((f) => ({
      type: "Feature",
      id: f.id != null ? String(f.id) : `feat_${nextLocalId++}`,
      _multi: f._multi || false,
      geometry: f.geometry,
      properties: { ...(f.properties || {}) },
    }));
  editedProperties.clear();
  selectedFeatureId = null;
  // Clear terra-draw so any leftover in-progress geometry doesn't bleed in.
  if (draw) { try { draw.clear(); } catch {} }
  refreshFeatureSource();
  setStatus(`loaded ${loadedFeatures.length} features`);
}

function refreshFeatureSource() {
  if (!map) return;
  if (!map.isStyleLoaded()) {
    // 'load' fires once; 'styledata' fires repeatedly so it's safer for the
    // case where additional sources are loaded after initial style.load.
    map.once("styledata", refreshFeatureSource);
    return;
  }
  const features = loadedFeatures.map((f) => ({
    type: "Feature",
    id: f.id,
    geometry: f.geometry,
    properties: {
      ...(f.properties || {}),
      ...(editedProperties.get(f.id) || {}),
      _ofm_id: f.id,                                     // for click-handler lookup
      _ofm_selected: f.id === selectedFeatureId,
    },
  }));
  const fc = { type: "FeatureCollection", features };
  const srcId = "_ofm_features";
  if (map.getSource(srcId)) {
    map.getSource(srcId).setData(fc);
    return;
  }
  // First-time setup: source + 4 visual layers + click handlers.
  // NOTE: no promoteId — maplibre would then look for properties.id (which
  // we don't set) and the click handler's feature.id would come back null.
  // Top-level `id` on each feature is already picked up by maplibre for
  // GeoJSON sources.
  try {
    map.addSource(srcId, { type: "geojson", data: fc });
  } catch (e) {
    setStatus(`addSource failed: ${e.message}`, "bad");
    return;
  }
  const SEL = ["case", ["==", ["get", "_ofm_selected"], true], "#ff9933", "#5fa8d3"];
  map.addLayer({
    id: "_ofm_features_fill", type: "fill", source: srcId,
    filter: ["==", ["geometry-type"], "Polygon"],
    paint: { "fill-color": SEL, "fill-opacity": 0.25 },
  });
  map.addLayer({
    id: "_ofm_features_outline", type: "line", source: srcId,
    filter: ["==", ["geometry-type"], "Polygon"],
    paint: { "line-color": SEL, "line-width": 2 },
  });
  map.addLayer({
    id: "_ofm_features_line", type: "line", source: srcId,
    filter: ["==", ["geometry-type"], "LineString"],
    paint: { "line-color": SEL, "line-width": 3 },
  });
  map.addLayer({
    id: "_ofm_features_circle", type: "circle", source: srcId,
    filter: ["==", ["geometry-type"], "Point"],
    paint: {
      "circle-radius": ["case", ["==", ["get", "_ofm_selected"], true], 8, 5],
      "circle-color": SEL,
      "circle-stroke-color": "#ffffff",
      "circle-stroke-width": 1.5,
    },
  });
  // Single map-wide click handler instead of per-layer bindings. The
  // per-layer pattern (`map.on("click", "layerid", h)`) has subtle race
  // issues when the layer was just added — switching to a manual
  // queryRenderedFeatures dispatch eliminates them.
  map.on("click", (e) => {
    const ourLayers = ["_ofm_features_fill", "_ofm_features_line", "_ofm_features_circle", "_ofm_features_outline"];
    const presentLayers = ourLayers.filter((id) => map.getLayer(id));
    const all = map.queryRenderedFeatures(e.point);
    const ours = map.queryRenderedFeatures(e.point, { layers: presentLayers });
    if (ours.length) {
      onMapFeatureClick({ features: ours });
      return;
    }
    // Diagnostic so we can see what got hit if our layers don't match.
    if (all.length) {
      const seen = [...new Set(all.map((f) => f.layer?.id || "?"))].join(", ");
      setStatus(`click hit [${seen}] but none in our editing layers (${presentLayers.length} present)`);
    }
    if (selectedFeatureId) {
      selectedFeatureId = null;
      refreshFeatureSource();
      $props.textContent = "select a feature to edit its properties";
    }
  });
  map.on("mousemove", (e) => {
    const presentLayers = ["_ofm_features_fill", "_ofm_features_line", "_ofm_features_circle", "_ofm_features_outline"]
      .filter((id) => map.getLayer(id));
    const hits = map.queryRenderedFeatures(e.point, { layers: presentLayers });
    map.getCanvas().style.cursor = hits.length ? "pointer" : "";
  });
  setStatus(`rendered ${features.length} features in editing source`);
}

function onMapFeatureClick(e) {
  const f = e.features?.[0];
  if (!f) return;
  // Prefer top-level id, fall back to our stashed properties._ofm_id.
  const id = (f.id != null ? String(f.id) : null) || f.properties?._ofm_id || null;
  if (!id) {
    setStatus(`click hit feature without _ofm_id — bug`, "bad");
    return;
  }
  let mine = loadedFeatures.find((x) => x.id === id);
  if (!mine) {
    // Fallback: match by geometry (covers state-vs-source desync). If we find
    // it that way, ALSO normalise — adopt the maplibre-known id so future
    // clicks match.
    mine = loadedFeatures.find((x) => sameGeom(x.geometry, f.geometry));
    if (mine) {
      const sample = loadedFeatures.slice(0, 3).map((x) => x.id).join(",");
      setStatus(
        `id ${id} not in state (state has ${loadedFeatures.length}: ${sample}…) — matched by geometry, fixing`,
        "muted",
      );
      mine.id = id;  // adopt the source's id
    } else {
      const sample = loadedFeatures.slice(0, 5).map((x) => x.id).join(",");
      setStatus(
        `clicked id=${id} but no matching feature in state (state has ${loadedFeatures.length} features: ${sample}…)`,
        "bad",
      );
      console.warn("OFM click mismatch", { clicked: id, state: loadedFeatures.map((x) => x.id) });
      return;
    }
  }
  selectedFeatureId = mine.id;
  refreshFeatureSource();
  onSelectionChange({
    features: [{
      id: mine.id,
      geometry: mine.geometry,
      properties: { ...(mine.properties || {}), ...(editedProperties.get(mine.id) || {}) },
    }],
  });
}

function sameGeom(a, b) {
  // Cheap-but-sufficient comparison via stringified coords. We only need to
  // recognise identity, not equivalence up to ring rotation/etc.
  if (!a || !b || a.type !== b.type) return false;
  try { return JSON.stringify(a.coordinates) === JSON.stringify(b.coordinates); }
  catch { return false; }
}

function allVertices(geom) {
  // Yield every [lng,lat] vertex of any GeoJSON geometry (point/line/polygon
  // and their Multi cousins). Used by the snap function to find candidates.
  if (!geom) return [];
  const c = geom.coordinates;
  switch (geom.type) {
    case "Point":           return [c];
    case "MultiPoint":      return c;
    case "LineString":      return c;
    case "MultiLineString": return c.flat();
    case "Polygon":         return c.flat();              // rings → coords
    case "MultiPolygon":    return c.flat(2);             // polys → rings → coords
    default:                return [];
  }
}

// Multi geometries are edited part by part. Every part keeps the feature's id
// and is marked `_multi`, so joinParts() can put the feature back together on
// save instead of turning each part into a separate feature.
function flattenMulti(f) {
  const g = f.geometry;
  if (!g) return [f];
  if (g.type === "MultiPoint")
    return g.coordinates.map((c) => ({ ...f, _multi: true, geometry: { type: "Point", coordinates: c } }));
  if (g.type === "MultiLineString")
    return g.coordinates.map((c) => ({ ...f, _multi: true, geometry: { type: "LineString", coordinates: c } }));
  if (g.type === "MultiPolygon")
    return g.coordinates.map((c) => ({ ...f, _multi: true, geometry: { type: "Polygon", coordinates: c } }));
  return [f];
}

// Inverse of flattenMulti: one feature per id, parts rejoined as a Multi geometry.
function joinParts(features) {
  const groups = new Map();
  for (const f of features) {
    if (!groups.has(f.id)) groups.set(f.id, []);
    groups.get(f.id).push(f);
  }
  return [...groups.values()].map((parts) => {
    const first = parts[0];
    if (parts.length === 1 && !first._multi) return first;
    return {
      ...first,
      geometry: { type: `Multi${first.geometry.type}`, coordinates: parts.map((p) => p.geometry.coordinates) },
    };
  });
}

function geometryTypeForLayer() {
  // Schema-declared geometryType wins. Otherwise infer from first feature.
  const t = currentLayerSchema?.geometryType;
  if (t) return t;
  return loadedFeatures[0]?.geometry?.type || null;
}

function drawModeFor(geomType) {
  return {
    Point: "point", MultiPoint: "point",
    LineString: "linestring", MultiLineString: "linestring",
    Polygon: "polygon", MultiPolygon: "polygon",
  }[geomType] || null;
}

function refreshAddFeatureBtn() {
  const geom = geometryTypeForLayer();
  if (!geom) {
    $addFeatureBtn.disabled = true;
    $addFeatureBtn.textContent = "+ feature (set type first)";
    $addFeatureBtn.title = "Add at least one feature to infer geometry type, or set `geometryType` on the layer schema.";
    return;
  }
  $addFeatureBtn.disabled = false;
  $addFeatureBtn.textContent = `+ ${drawModeFor(geom)} feature`;
  $addFeatureBtn.title = `Click to start drawing a new ${geom} on the map.`;
}

$addFeatureBtn.addEventListener("click", () => {
  const geom = geometryTypeForLayer();
  const mode = drawModeFor(geom);
  if (!mode || !draw) return;
  draw.setMode(mode);
  setStatus(`drawing ${mode} — click to add vertices, double-click (or close) to finish; ESC to cancel`, "ok");
});

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && draw) {
    draw.setMode("select");
    setStatus("draw cancelled");
  }
});

function onTerraSelect(id) {
  const f = draw.getSnapshot().find((x) => x.id === id);
  if (!f) return;
  onSelectionChange({ features: [f] });
}

function onFinishDraw(id) {
  setDirty(true);
  const f = draw.getSnapshot().find((x) => x.id === id);
  if (!f) return;
  // Strip Terra Draw's internal `mode` prop, apply schema defaults.
  const { mode, ...userProps } = f.properties || {};
  const schemaProps = currentLayerSchema?.properties || {};
  for (const [k, defn] of Object.entries(schemaProps)) {
    if (userProps[k] !== undefined) continue;
    userProps[k] =
      defn.default !== undefined ? defn.default :
      defn.enum ? defn.enum[0] :
      defn.type === "boolean" ? false :
      defn.type === "integer" || defn.type === "number" ? 0 :
      "";
  }
  const newId = `new_${nextLocalId++}`;
  loadedFeatures.push({
    type: "Feature",
    id: newId,
    geometry: f.geometry,
    properties: userProps,
  });
  // Migrate the feature into our own state; remove it from terra-draw so we
  // don't render it twice (terra-draw shows its own version while drawing).
  try { draw.removeFeatures([id]); } catch {}
  draw.setMode("select");
  selectedFeatureId = newId;
  refreshFeatureSource();
  onSelectionChange({
    features: [{ id: newId, geometry: f.geometry, properties: userProps }],
  });
  setStatus(`added new ${f.geometry.type} — set its properties on the right, then click Save`, "ok");
}

function makeInput(defn, value) {
  // enum → <select>
  if (defn.enum && Array.isArray(defn.enum)) {
    const sel = document.createElement("select");
    sel.appendChild(new Option("", ""));
    for (const v of defn.enum) sel.appendChild(new Option(String(v), String(v)));
    sel.value = value == null ? "" : String(value);
    return sel;
  }
  if (defn.type === "boolean") {
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = !!value;
    return cb;
  }
  if (defn.type === "integer" || defn.type === "number") {
    const i = document.createElement("input");
    i.type = "number";
    if (defn.type === "integer") i.step = "1";
    if (defn.minimum != null) i.min = String(defn.minimum);
    if (defn.maximum != null) i.max = String(defn.maximum);
    i.value = value == null ? "" : String(value);
    return i;
  }
  // default: text
  const t = document.createElement("input");
  t.type = "text";
  t.value = value == null
    ? ""
    : (typeof value === "object" ? JSON.stringify(value) : String(value));
  return t;
}

// --- "+ field" — add a typed property to the current layer's schema -------

$addFieldBtn.addEventListener("click", async () => {
  if (!currentWorld || !currentLayer) {
    alert("pick a world + layer first");
    return;
  }
  const name = prompt("New field name (letters/digits/underscore):");
  if (!name || !/^[a-zA-Z_][a-zA-Z0-9_]*$/.test(name)) return;
  const type = prompt(
    "Field type? one of: string | integer | number | boolean | enum",
    "string",
  );
  if (!type) return;
  const okTypes = ["string", "integer", "number", "boolean", "enum"];
  if (!okTypes.includes(type)) {
    alert(`type must be one of: ${okTypes.join(", ")}`);
    return;
  }
  let defn = {};
  if (type === "enum") {
    const csv = prompt("Allowed values, comma-separated:", "A, B, C");
    if (!csv) return;
    defn = { type: "string", enum: csv.split(",").map((s) => s.trim()).filter(Boolean) };
  } else {
    defn = { type };
  }
  const desc = prompt("Description (optional):", "");
  if (desc) defn.description = desc;

  const schema = currentLayerSchema || { type: "object", properties: {} };
  schema.properties = schema.properties || {};
  if (schema.properties[name]) {
    if (!confirm(`Field "${name}" already exists; overwrite definition?`)) return;
  }
  schema.properties[name] = defn;
  schema.type = "object";

  try {
    await fetchJSON(`/api/worlds/${currentWorld}/layers/${currentLayer}/schema`, {
      method: "PUT",
      body: JSON.stringify(schema),
    });
    currentLayerSchema = schema;
    renderSchemaSummary();
    setStatus(`added field "${name}" (${type})`, "ok");
  } catch (e) {
    setStatus(`schema save failed: ${e.body?.detail || e.message}`, "bad");
  }
});

// --- manifest mode (Monaco) -------------------------------------------

function ensureMonaco() {
  if (monacoReady) return Promise.resolve();
  return new Promise((resolve) => {
    require.config({ paths: { vs: "https://cdn.jsdelivr.net/npm/monaco-editor@0.50.0/min/vs" } });
    require(["vs/editor/editor.main"], () => { monacoReady = true; resolve(); });
  });
}

async function loadManifest(slug, kind) {
  if (!slug || !kind) return;
  setStatus(`loading ${kind}.json…`);
  await ensureMonaco();
  const [schema, body] = await Promise.all([
    fetchJSON(`/api/schemas/${kind}`),
    fetchJSON(`/api/worlds/${slug}/manifest/${kind}`),
  ]);
  manifestSchema = schema;
  currentManifestKind = kind;
  // wire schema for validation + autocomplete
  monaco.languages.json.jsonDefaults.setDiagnosticsOptions({
    validate: true,
    schemas: [
      {
        uri: schema.$id || `http://ofm/schemas/${kind}.json`,
        fileMatch: ["*"],
        schema: schema,
      },
    ],
  });

  const content = body.content == null ? (kind === "render" || kind === "orbitals" ? [] : {}) : body.content;
  const text = JSON.stringify(content, null, 2);
  const oldModel = monacoEditor?.getModel();
  if (monacoEditor) monacoEditor.dispose();
  if (oldModel) oldModel.dispose();
  monacoEditor = monaco.editor.create($manifestEditor, {
    value: text,
    language: "json",
    theme: "vs-dark",
    automaticLayout: true,
    tabSize: 2,
    minimap: { enabled: false },
    formatOnPaste: true,
    fontSize: 13,
  });
  monacoEditor.onDidChangeModelContent(() => {
    if (currentPane === "raw") setDirty(true);
    updateValidationDisplay();
  });
  monaco.editor.onDidChangeMarkers(updateValidationDisplay);

  // Reset form pane; Raw/Form toggle re-mounts it lazily on switch.
  destroyJsonEditor();
  $manifestFormBtn.disabled = !FORM_SUPPORTED.has(kind);
  // Default to raw pane on every load; user can toggle.
  switchPane("raw");

  $manifestTitle.textContent = `${kind}.json — ${slug}`;
  showStyleToolsForKind(kind);
  setDirty(false);
  setStatus(`editing ${kind}.json on ${slug}`);
  updateValidationDisplay();
}

// --- style preset creator (map.json only) ---------------------------------

const STYLE_PRESETS = {
  "polygon-fill-outline": {
    label: "Polygon: fill + outline",
    help: "Two layers — a translucent fill and a stroked outline — both reading from the same source.",
    inputs: [
      { key: "fillColor",   label: "Fill color",    type: "color",  default: "#5fa8d3" },
      { key: "lineColor",   label: "Outline color", type: "color",  default: "#1a1a1a" },
      { key: "fillOpacity", label: "Fill opacity",  type: "number", default: 0.3, min: 0, max: 1, step: 0.05 },
      { key: "lineWidth",   label: "Outline width", type: "number", default: 1.5, min: 0, step: 0.25 },
    ],
    build: (src, p) => [
      { id: `${src}-fill`, type: "fill", source: src,
        paint: { "fill-color": p.fillColor, "fill-opacity": parseFloat(p.fillOpacity) } },
      { id: `${src}-outline`, type: "line", source: src,
        paint: { "line-color": p.lineColor, "line-width": parseFloat(p.lineWidth) } },
    ],
  },
  "line-basic": {
    label: "Line: simple stroke",
    help: "A single line layer with constant colour and width.",
    inputs: [
      { key: "lineColor", label: "Color", type: "color",  default: "#1a1a1a" },
      { key: "lineWidth", label: "Width", type: "number", default: 2, min: 0, step: 0.25 },
    ],
    build: (src, p) => [
      { id: `${src}-line`, type: "line", source: src,
        paint: { "line-color": p.lineColor, "line-width": parseFloat(p.lineWidth) } },
    ],
  },
  "point-circle": {
    label: "Point: circle marker",
    help: "Filled circle with stroke. Radius can scale with zoom by editing the layer later.",
    inputs: [
      { key: "circleColor", label: "Fill color",   type: "color",  default: "#5fa8d3" },
      { key: "circleRadius",label: "Radius (px)",  type: "number", default: 5, min: 1, step: 0.5 },
      { key: "strokeColor", label: "Stroke color", type: "color",  default: "#ffffff" },
      { key: "strokeWidth", label: "Stroke width", type: "number", default: 1, min: 0, step: 0.25 },
    ],
    build: (src, p) => [
      { id: `${src}-circle`, type: "circle", source: src,
        paint: {
          "circle-color": p.circleColor,
          "circle-radius": parseFloat(p.circleRadius),
          "circle-stroke-color": p.strokeColor,
          "circle-stroke-width": parseFloat(p.strokeWidth),
        } },
    ],
  },
  "label": {
    label: "Symbol: text label",
    help: "Reads a property name and renders it as text. Works on any geometry.",
    inputs: [
      { key: "field",     label: "Property name", type: "text",   default: "name" },
      { key: "color",     label: "Text color",    type: "color",  default: "#1a1a1a" },
      { key: "size",      label: "Size (px)",     type: "number", default: 11, min: 6, max: 48 },
      { key: "haloColor", label: "Halo color",    type: "color",  default: "#ffffff" },
      { key: "minzoom",   label: "Min zoom",      type: "number", default: 0, min: 0, max: 22, step: 1 },
    ],
    build: (src, p) => [
      { id: `${src}-label`, type: "symbol", source: src, minzoom: parseInt(p.minzoom, 10),
        layout: {
          "text-field": ["coalesce", ["get", p.field], ""],
          "text-size": parseFloat(p.size),
          "text-offset": [0, 0.8],
          "text-anchor": "top",
        },
        paint: {
          "text-color": p.color,
          "text-halo-color": p.haloColor,
          "text-halo-width": 1.2,
        } },
    ],
  },
  "polygon-categorical": {
    label: "Polygon: fill by category",
    help: "Match expression — fill color depends on a property value. One value:color pair per line.",
    inputs: [
      { key: "field",    label: "Property name", type: "text",     default: "class" },
      { key: "cases",    label: "value color (one per line)", type: "textarea",
        default: "north #cccccc\nsouth #66aa66\neast #aa6666\nwest #6666aa" },
      { key: "fallback", label: "Fallback color", type: "color",  default: "#888888" },
      { key: "fillOpacity", label: "Fill opacity", type: "number", default: 0.4, min: 0, max: 1, step: 0.05 },
    ],
    build: (src, p) => {
      const expr = ["match", ["get", p.field]];
      for (const line of String(p.cases).split("\n")) {
        const m = line.trim().split(/\s+/);
        if (m.length >= 2 && m[0] && m[1]) expr.push(m[0], m[1]);
      }
      expr.push(p.fallback);
      return [
        { id: `${src}-fill-categorical`, type: "fill", source: src,
          paint: { "fill-color": expr, "fill-opacity": parseFloat(p.fillOpacity) } },
      ];
    },
  },
  "fill-3d-constant": {
    label: "3D fill: constant height",
    help: "fill-extrusion at a fixed height. Tilt the map (right-click + drag, or Ctrl+drag) to see the volume.",
    inputs: [
      { key: "color",    label: "Color",          type: "color",  default: "#a09f9c" },
      { key: "height",   label: "Height (m)",     type: "number", default: 10, min: 0, step: 0.5 },
      { key: "base",     label: "Base (m)",       type: "number", default: 0,  min: 0, step: 0.5 },
      { key: "opacity",  label: "Opacity",        type: "number", default: 0.85, min: 0, max: 1, step: 0.05 },
      { key: "gradient", label: "Shaded sides",   type: "checkbox", default: true },
    ],
    build: (src, p) => [
      { id: `${src}-fill-extrusion`, type: "fill-extrusion", source: src,
        paint: {
          "fill-extrusion-color":   p.color,
          "fill-extrusion-height":  parseFloat(p.height),
          "fill-extrusion-base":    parseFloat(p.base),
          "fill-extrusion-opacity": parseFloat(p.opacity),
          "fill-extrusion-vertical-gradient": p.gradient === true || p.gradient === "true" || p.gradient === "on",
        } },
    ],
  },
  "fill-3d-from-property": {
    label: "3D fill: height from property",
    help: "fill-extrusion that reads a per-feature height. A multiplier lets you scale e.g. 'levels' (1 level ≈ 3 m) into metres.",
    inputs: [
      { key: "color",        label: "Color",                    type: "color",  default: "#a09f9c" },
      { key: "heightField",  label: "Height property",          type: "text",   default: "height" },
      { key: "heightMul",    label: "Height multiplier (×)",    type: "number", default: 1, min: 0, step: 0.1 },
      { key: "heightFallback", label: "Fallback height (m)",    type: "number", default: 5, min: 0, step: 0.5 },
      { key: "baseField",    label: "Base property (optional)", type: "text",   default: "" },
      { key: "opacity",      label: "Opacity",                  type: "number", default: 0.9, min: 0, max: 1, step: 0.05 },
    ],
    build: (src, p) => {
      const mul = parseFloat(p.heightMul) || 1;
      const fallback = parseFloat(p.heightFallback) || 0;
      const hExpr = mul === 1
        ? ["coalesce", ["to-number", ["get", p.heightField]], fallback]
        : ["*", mul, ["coalesce", ["to-number", ["get", p.heightField]], fallback]];
      const paint = {
        "fill-extrusion-color":   p.color,
        "fill-extrusion-height":  hExpr,
        "fill-extrusion-opacity": parseFloat(p.opacity),
        "fill-extrusion-vertical-gradient": true,
      };
      if (p.baseField && p.baseField.trim()) {
        paint["fill-extrusion-base"] = ["coalesce", ["to-number", ["get", p.baseField.trim()]], 0];
      }
      return [
        { id: `${src}-fill-extrusion`, type: "fill-extrusion", source: src, paint },
      ];
    },
  },
  "fill-3d-categorical": {
    label: "3D fill: color + height by category",
    help: "Match expression — colour AND height depend on a property value. One 'value color height' per line.",
    inputs: [
      { key: "field",    label: "Property name", type: "text", default: "class" },
      { key: "cases",    label: "value color height (one per line)", type: "textarea",
        default: "house    #c2a878  6\ntower    #8f9aa6 30\ntemple   #d4cba2 15\nshop     #b8a16b  8" },
      { key: "fallbackColor",  label: "Fallback color",  type: "color",  default: "#888888" },
      { key: "fallbackHeight", label: "Fallback height", type: "number", default: 5, min: 0, step: 0.5 },
      { key: "opacity", label: "Opacity", type: "number", default: 0.9, min: 0, max: 1, step: 0.05 },
    ],
    build: (src, p) => {
      const colorExpr  = ["match", ["get", p.field]];
      const heightExpr = ["match", ["get", p.field]];
      for (const line of String(p.cases).split("\n")) {
        const m = line.trim().split(/\s+/);
        if (m.length >= 3 && m[0] && m[1] && m[2]) {
          colorExpr.push(m[0], m[1]);
          heightExpr.push(m[0], parseFloat(m[2]));
        }
      }
      colorExpr.push(p.fallbackColor);
      heightExpr.push(parseFloat(p.fallbackHeight) || 0);
      return [
        { id: `${src}-fill-extrusion-categorical`, type: "fill-extrusion", source: src,
          paint: {
            "fill-extrusion-color":   colorExpr,
            "fill-extrusion-height":  heightExpr,
            "fill-extrusion-opacity": parseFloat(p.opacity),
            "fill-extrusion-vertical-gradient": true,
          } },
      ];
    },
  },
};

const $presetModal = document.getElementById("preset-modal");
const $presetForm = document.getElementById("preset-form");
const $presetSource = document.getElementById("preset-source");
const $presetKind = document.getElementById("preset-kind");
const $presetInputs = document.getElementById("preset-inputs");
const $presetHelp = document.getElementById("preset-help");
const $openPresetBtn = document.getElementById("open-preset-btn");
const $mapStyleTools = document.getElementById("map-style-tools");

function showStyleToolsForKind(kind) {
  $mapStyleTools.hidden = kind !== "map";
}

function currentMapJsonFromEditor() {
  // Read the manifest's current value from whichever pane is active.
  if (currentPane === "form" && jsonEditor) {
    return jsonEditor.getValue();
  }
  if (monacoEditor) {
    try { return JSON.parse(monacoEditor.getValue()); }
    catch (e) { setStatus(`raw JSON invalid: ${e.message}`, "bad"); return null; }
  }
  return null;
}

function writeMapJsonToEditor(obj) {
  if (currentPane === "form" && jsonEditor) {
    jsonEditor.setValue(obj);
  }
  if (monacoEditor) {
    monacoEditor.setValue(JSON.stringify(obj, null, 2));
  }
  setDirty(true);
}

$openPresetBtn.addEventListener("click", () => {
  const mj = currentMapJsonFromEditor();
  if (!mj) return;
  // Populate source dropdown from current map.json#sources (any type).
  const sourceKeys = Object.keys(mj.sources || {});
  $presetSource.innerHTML = sourceKeys
    .map((k) => `<option value="${k}">${k} (${mj.sources[k]?.type || "?"})</option>`)
    .join("");
  $presetKind.innerHTML = Object.entries(STYLE_PRESETS)
    .map(([k, p]) => `<option value="${k}">${p.label}</option>`)
    .join("");
  $presetKind.value = Object.keys(STYLE_PRESETS)[0];
  rebuildPresetInputs();
  $presetModal.hidden = false;
});

$presetKind.addEventListener("change", rebuildPresetInputs);

function rebuildPresetInputs() {
  const preset = STYLE_PRESETS[$presetKind.value];
  if (!preset) return;
  $presetHelp.textContent = preset.help || "";
  $presetInputs.innerHTML = "";
  for (const input of preset.inputs) {
    const row = document.createElement("div");
    row.className = "field-row";
    const label = document.createElement("label");
    label.textContent = input.label;
    let ctrl;
    if (input.type === "textarea") {
      ctrl = document.createElement("textarea");
      ctrl.value = input.default ?? "";
    } else if (input.type === "checkbox") {
      ctrl = document.createElement("input");
      ctrl.type = "checkbox";
      ctrl.checked = !!input.default;
    } else {
      ctrl = document.createElement("input");
      ctrl.type = input.type;
      ctrl.value = input.default ?? "";
      if (input.min  != null) ctrl.min  = input.min;
      if (input.max  != null) ctrl.max  = input.max;
      if (input.step != null) ctrl.step = input.step;
    }
    ctrl.dataset.key = input.key;
    row.appendChild(label);
    row.appendChild(ctrl);
    $presetInputs.appendChild(row);
  }
}

document.getElementById("preset-cancel").addEventListener("click", () => {
  $presetModal.hidden = true;
});

$presetForm.addEventListener("submit", (e) => {
  e.preventDefault();
  const preset = STYLE_PRESETS[$presetKind.value];
  if (!preset) return;
  const source = $presetSource.value;
  if (!source) { alert("pick a source first"); return; }
  const params = {};
  for (const el of $presetInputs.querySelectorAll("[data-key]")) {
    params[el.dataset.key] = el.type === "checkbox" ? el.checked : el.value;
  }
  const newLayers = preset.build(source, params);
  const mj = currentMapJsonFromEditor();
  if (!mj) return;
  mj.layers = Array.isArray(mj.layers) ? mj.layers : [];
  // Avoid colliding ids — append a suffix if needed.
  const existing = new Set(mj.layers.map((l) => l.id));
  for (const l of newLayers) {
    let id = l.id, i = 2;
    while (existing.has(id)) { id = `${l.id}-${i++}`; }
    l.id = id;
    existing.add(id);
    mj.layers.push(l);
  }
  writeMapJsonToEditor(mj);
  $presetModal.hidden = true;
  setStatus(`appended ${newLayers.length} layer(s) to map.json`, "ok");
});

// --- Raw / Form pane toggle ------------------------------------------------

$paneBtns.forEach((btn) => {
  btn.addEventListener("click", () => switchPane(btn.dataset.pane));
});

function switchPane(pane) {
  if (pane === "form" && !FORM_SUPPORTED.has(currentManifestKind)) return;
  // When switching, sync data between panes so unsaved edits aren't lost.
  if (pane === currentPane) return;
  if (pane === "form") {
    // raw → form: parse current Monaco text, mount JSONEditor with it
    let parsed;
    try {
      parsed = JSON.parse(monacoEditor.getValue());
    } catch (e) {
      setStatus(`raw JSON invalid: ${e.message} — fix before switching`, "bad");
      return;
    }
    mountJsonEditor(manifestSchema, parsed);
  } else {
    // form → raw: copy JSONEditor value back to Monaco
    if (jsonEditor) {
      const val = jsonEditor.getValue();
      monacoEditor.setValue(JSON.stringify(val, null, 2));
    }
  }
  currentPane = pane;
  $paneBtns.forEach((b) => b.classList.toggle("active", b.dataset.pane === pane));
  $manifestEditor.hidden = pane !== "raw";
  $manifestForm.hidden = pane !== "form";
}

function destroyJsonEditor() {
  if (jsonEditor) {
    try { jsonEditor.destroy(); } catch {}
    jsonEditor = null;
  }
  $manifestForm.innerHTML = "";
}

function mountJsonEditor(schema, value) {
  destroyJsonEditor();
  // JSONEditor (the npm pkg @json-editor/json-editor) registers global window.JSONEditor
  jsonEditor = new window.JSONEditor($manifestForm, {
    schema: schema,
    startval: value,
    theme: "html",
    iconlib: null,
    disable_array_delete_last_row: false,
    disable_array_delete_all_rows: true,
    disable_collapse: false,
    disable_edit_json: true,    // raw JSON edit is the *other* tab
    disable_properties: false,
    show_errors: "interaction",
    no_additional_properties: false,
  });
  jsonEditor.on("change", () => {
    if (currentPane === "form") setDirty(true);
    const errs = jsonEditor.validate();
    if (errs.length === 0) {
      $manifestErrors.className = "ok";
      $manifestErrors.textContent = "no errors";
    } else {
      $manifestErrors.className = "bad";
      $manifestErrors.innerHTML =
        "<ul>" +
        errs.slice(0, 20).map((e) => `<li>${e.path}: ${e.message}</li>`).join("") +
        "</ul>";
    }
  });
}

function updateValidationDisplay() {
  if (!monacoEditor) return;
  const model = monacoEditor.getModel();
  const markers = monaco.editor.getModelMarkers({ resource: model.uri });
  const errs = markers.filter((m) => m.severity >= monaco.MarkerSeverity.Warning);
  if (errs.length === 0) {
    $manifestErrors.className = "ok";
    $manifestErrors.textContent = "no errors";
    return;
  }
  $manifestErrors.className = "bad";
  $manifestErrors.innerHTML =
    "<ul>" +
    errs.slice(0, 20).map((m) => `<li>L${m.startLineNumber}: ${m.message}</li>`).join("") +
    "</ul>";
}

async function saveManifest() {
  const kind = MANIFEST_KINDS[currentMode];
  let parsed;
  if (currentPane === "form" && jsonEditor) {
    parsed = jsonEditor.getValue();
  } else {
    const text = monacoEditor.getValue();
    try { parsed = JSON.parse(text); }
    catch (e) {
      setStatus(`JSON parse error: ${e.message}`, "bad");
      return;
    }
  }
  setStatus("saving…");
  try {
    const r = await fetchJSON(`/api/worlds/${currentWorld}/manifest/${kind}`, {
      method: "PUT",
      body: JSON.stringify({ content: parsed }),
    });
    setDirty(false);
    setStatus(`saved ${kind}.json (${r.bytes}B)`, "ok");
    $manifestSaveInfo.textContent = JSON.stringify(r, null, 2);
  } catch (e) {
    if (e.status === 422 && e.body?.detail?.validation_errors) {
      const errs = e.body.detail.validation_errors;
      $manifestErrors.className = "bad";
      $manifestErrors.innerHTML =
        "<ul>" + errs.map((s) => `<li>${s}</li>`).join("") + "</ul>";
      setStatus(`server rejected: ${errs.length} validation error(s)`, "bad");
    } else {
      setStatus(e.message, "bad");
    }
  }
}

// --- save / reload buttons (dispatch on current mode) -----------------

$save.addEventListener("click", async () => {
  if (currentMode === "map") {
    if (!currentWorld || !currentLayer) return;
    setStatus("saving…");
    try {
      // Build the FeatureCollection from our own state. Loaded features get
      // their pending edits merged. Ids go back to the server, which matches
      // them against the stored features: changed ones are updated, missing
      // ones deleted, and new ones (id "new_N") inserted with a permanent id.
      const features = joinParts(loadedFeatures).map((f) => ({
        type: "Feature",
        id: f.id,
        geometry: f.geometry,
        properties: {
          ...(f.properties || {}),
          ...(editedProperties.get(f.id) || {}),
        },
      }));
      const fc = { type: "FeatureCollection", features };

      // Sanity guard against accidentally wiping a layer.
      const loadedCount = parseInt(
        $layer.options[$layer.selectedIndex]?.text?.match(/\((\d+),/)?.[1] || "0",
        10,
      );
      if (loadedCount > 0 && fc.features.length < loadedCount && !confirm(
        `About to save ${fc.features.length} features but the layer had ${loadedCount}. ` +
        `Existing features may be deleted. Continue?`,
      )) {
        setStatus("save cancelled", "muted");
        return;
      }

      const r = await fetchJSON(`/api/worlds/${currentWorld}/layers/${currentLayer}`, {
        method: "PUT",
        body: JSON.stringify(fc),
      });
      setDirty(false);
      // Reload so new features pick up their permanent ids (a second save
      // with the local "new_N" ids would insert them again).
      await loadLayer(currentLayer);
      const parts = [`${r.inserted} added`, `${r.updated} changed`, `${r.deleted} deleted`];
      const unstored = r.unstored_attributes || [];
      if (unstored.length) {
        setStatus(`saved ${r.layer}: ${parts.join(", ")} — not stored (the layer has no column for): ${unstored.join(", ")}`, "bad");
      } else {
        setStatus(`saved ${r.layer}: ${parts.join(", ")}`, "ok");
      }
      $info.textContent = JSON.stringify(r, null, 2);
    } catch (e) {
      setStatus(e.body?.detail || e.message, "bad");
    }
  } else {
    await saveManifest();
  }
});

$reload.addEventListener("click", async () => {
  if (dirty && !confirm("Discard unsaved changes?")) return;
  if (currentMode === "map") await loadLayer(currentLayer);
  else await loadManifest(currentWorld, MANIFEST_KINDS[currentMode]);
});

$newLayer.addEventListener("click", async () => {
  const name = prompt("New layer name (letters/digits/underscore only):");
  if (!name || !/^[a-zA-Z0-9_\-]+$/.test(name)) return;
  const geomTypes = ["Point", "LineString", "Polygon"];
  const geometryType = prompt(
    `Geometry type? one of: ${geomTypes.join(" | ")}`,
    "Polygon",
  );
  if (!geometryType || !geomTypes.includes(geometryType)) {
    alert(`type must be one of: ${geomTypes.join(", ")}`);
    return;
  }
  try {
    // 1. seed empty layer
    await fetchJSON(`/api/worlds/${currentWorld}/layers/${name}`, {
      method: "PUT",
      body: JSON.stringify({ type: "FeatureCollection", features: [] }),
    });
    // 2. seed layer schema with geometryType so the editor knows
    await fetchJSON(`/api/worlds/${currentWorld}/layers/${name}/schema`, {
      method: "PUT",
      body: JSON.stringify({
        type: "object",
        properties: {},
        geometryType,
      }),
    });
  } catch (e) {
    setStatus(`could not create layer: ${e.body?.detail || e.message}`, "bad");
    return;
  }
  const opt = document.createElement("option");
  opt.value = name;
  opt.textContent = `${name} (0, ${geometryType})`;
  $layer.appendChild(opt);
  $layer.value = name;
  loadLayer(name);
});

// Entry point: gate on creds; if missing, show login and let its handler
// call init() after a successful sign-in.
if (getCreds()) {
  init().catch((e) => {
    if (e.status !== 401) setStatus(e.message, "bad");
  });
} else {
  showLogin();
}
