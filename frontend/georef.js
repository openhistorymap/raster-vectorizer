// Georeferencing workbench: control points between a scan and the map, live accuracy,
// warping to a basemap, and assisted tracing / label reading that produce cited features.
// Shares the editor's backend and sign-in session (sessionStorage "ofm.editor.creds").

const API = window.OFM_API_URL;
const CREDS_KEY = "ofm.editor.creds";
const MIN_POINTS = { polynomial1: 3, polynomial2: 6, polynomial3: 10, tps: 3 };

// --------------------------------------------------------------------------- auth + fetch

function authHeader() {
  try {
    const c = JSON.parse(sessionStorage.getItem(CREDS_KEY) || "null");
    return c ? "Basic " + btoa(`${c.user}:${c.pass}`) : null;
  } catch { return null; }
}

function showLogin(msg) {
  const err = document.getElementById("login-error");
  document.getElementById("login-overlay").hidden = false;
  err.hidden = !msg;
  err.textContent = msg || "";
  document.getElementById("login-user").focus();
}

document.getElementById("login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const user = document.getElementById("login-user").value.trim();
  const pass = document.getElementById("login-pass").value;
  sessionStorage.setItem(CREDS_KEY, JSON.stringify({ user, pass }));
  const r = await fetch(`${API}/api/worlds`, { headers: { Authorization: authHeader() } }).catch(() => null);
  if (!r || !r.ok) {
    sessionStorage.removeItem(CREDS_KEY);
    showLogin(r?.status === 401 ? "Wrong credentials." : "Cannot reach the editor backend.");
    return;
  }
  document.getElementById("login-overlay").hidden = true;
  init();
});

async function api(path, opts = {}) {
  const headers = { ...(opts.headers || {}) };
  if (!(opts.body instanceof FormData) && opts.body !== undefined) headers["Content-Type"] = "application/json";
  const auth = authHeader();
  if (auth) headers.Authorization = auth;
  const r = await fetch(`${API}${path}`, { ...opts, headers });
  if (r.status === 401) {
    sessionStorage.removeItem(CREDS_KEY);
    showLogin("Session expired — sign in again.");
    throw new Error("unauthenticated");
  }
  if (!r.ok) {
    let detail = `${r.status} ${r.statusText}`;
    try {
      const b = await r.json();
      const d = b.detail;
      detail = typeof d === "string" ? d : d?.errors ? d.errors.join("; ") : JSON.stringify(d);
    } catch {}
    const err = new Error(detail);
    err.status = r.status;
    throw err;
  }
  return r;
}
const getJSON = async (path, opts) => (await api(path, opts)).json();
const sendJSON = (path, method, body) => getJSON(path, { method, body: JSON.stringify(body) });

function setStatus(msg, level = "muted") {
  const el = document.getElementById("status");
  el.textContent = msg;
  el.className = level;
}

// --------------------------------------------------------------------------- state

const state = {
  world: null, timeline: {}, style: null,
  scans: [], scan: null, previewScale: 1, previewUrl: null,
  gcps: [], pending: null, fit: null,
  mode: "gcp", traced: null, nameCitation: null, assist: null,
};
const view = { k: 1, tx: 0, ty: 0 };
let map = null;
const $ = (id) => document.getElementById(id);

// --------------------------------------------------------------------------- init

async function init() {
  const params = new URLSearchParams(location.search);
  const worlds = await getJSON("/api/worlds");
  const sel = $("world-select");
  sel.innerHTML = worlds.map((w) => `<option value="${w.slug}">${w.name || w.slug}</option>`).join("");
  const wanted = params.get("world") || worlds[0]?.slug;
  if (wanted) sel.value = wanted;
  state.assist = await getJSON("/api/assist").catch(() => null);
  await loadWorld(sel.value, params.get("scan"));
}

async function loadWorld(slug, scanName) {
  state.world = slug;
  const desc = await getJSON(`/api/worlds/${slug}`);
  state.timeline = desc.timeline || {};
  state.style = await getJSON(`/api/worlds/${slug}/style`).catch(() => null);
  $("to-editor").href = `./index.html?world=${encodeURIComponent(slug)}`;
  initMap();
  await Promise.all([loadScans(scanName), loadLayers()]);
  updateReadAvailability();
}

function worldStyle() {
  const date = String(state.timeline.date ?? "");
  const text = JSON.stringify(state.style || {}).replaceAll("{atDate}", encodeURIComponent(date));
  const style = JSON.parse(text);
  return style.version ? style : null;
}

function osmStyle() {
  return {
    version: 8,
    sources: { osm: { type: "raster", tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"],
      tileSize: 256, maxzoom: 19, attribution: "© OpenStreetMap contributors" } },
    layers: [{ id: "osm", type: "raster", source: "osm" }],
  };
}

function initMap() {
  const base = state.timeline.base || {};
  const style = ($("basemap-select").value === "osm" ? null : worldStyle()) || osmStyle();
  if (map) { map.setStyle(style); map.once("styledata", addOverlayLayers); return; }
  map = new maplibregl.Map({
    container: "gr-map", style,
    center: [base.lng ?? 0, base.lat ?? 0], zoom: base.zoom ?? 2,
  });
  map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
  map.on("load", addOverlayLayers);
  map.on("click", onMapClick);
  map.on("mousemove", (e) => { $("map-hint").textContent = `${e.lngLat.lng.toFixed(6)}, ${e.lngLat.lat.toFixed(6)}`; });
}

function addOverlayLayers() {
  const empty = { type: "FeatureCollection", features: [] };
  for (const id of ["footprint", "traced"]) {
    if (!map.getSource(id)) map.addSource(id, { type: "geojson", data: empty });
  }
  if (!map.getLayer("footprint-line")) {
    map.addLayer({ id: "footprint-line", type: "line", source: "footprint",
      paint: { "line-color": "#5fa8d3", "line-width": 2, "line-dasharray": [2, 1] } });
  }
  if (!map.getLayer("traced-fill")) {
    map.addLayer({ id: "traced-fill", type: "fill", source: "traced", paint: { "fill-color": "#e0b45f", "fill-opacity": 0.3 } });
    map.addLayer({ id: "traced-line", type: "line", source: "traced", paint: { "line-color": "#e0b45f", "line-width": 2 } });
  }
  renderMapOverlays();
}

$("basemap-select").addEventListener("change", initMap);
$("world-select").addEventListener("change", (e) => {
  history.replaceState(null, "", `?world=${encodeURIComponent(e.target.value)}`);
  loadWorld(e.target.value);
});

// --------------------------------------------------------------------------- scans

async function loadScans(select) {
  state.scans = await getJSON(`/api/worlds/${state.world}/scans`);
  const sel = $("scan-select");
  sel.innerHTML = state.scans.length
    ? state.scans.map((s) => `<option value="${s.name}">${s.name}${s.georeference ? " ✓" : ""}</option>`).join("")
    : `<option value="">(no scans)</option>`;
  const name = select && state.scans.some((s) => s.name === select) ? select : state.scans[0]?.name;
  if (name) { sel.value = name; await loadScan(name); } else { clearScan(); }
}

function clearScan() {
  state.scan = null;
  $("scan-img").removeAttribute("src");
  $("scan-empty").hidden = false;
}

async function loadScan(name) {
  state.scan = state.scans.find((s) => s.name === name);
  history.replaceState(null, "", `?world=${encodeURIComponent(state.world)}&scan=${encodeURIComponent(name)}`);
  setStatus(`loading ${name}…`);
  const r = await api(`/api/worlds/${state.world}/scans/${name}/image?max=2048`);
  state.previewScale = parseFloat(r.headers.get("X-Scale") || "1");
  if (state.previewUrl) URL.revokeObjectURL(state.previewUrl);
  state.previewUrl = URL.createObjectURL(await r.blob());
  const img = $("scan-img");
  await new Promise((resolve) => { img.onload = resolve; img.src = state.previewUrl; });
  $("scan-empty").hidden = true;
  $("scan-overlay").setAttribute("width", img.naturalWidth);
  $("scan-overlay").setAttribute("height", img.naturalHeight);
  fitScanToPane();

  state.gcps = []; state.pending = null; state.fit = null; state.traced = null;
  try {
    const ann = await getJSON(`/api/worlds/${state.world}/scans/${name}/georef`);
    state.gcps = ann.body.features.map((f) => ({ pixel: f.properties.resourceCoords, lonlat: f.geometry.coordinates }));
    const t = ann.body.transformation;
    $("method-select").value = t.type === "thinPlateSpline" ? "tps" : `polynomial${t.options?.order || 1}`;
    $("expected-input").value = ann.expectedWidthM ?? "";
  } catch (e) {
    if (e.status !== 404) throw e;
  }
  renderPendingFeature();
  await refit();
  setStatus(`${name}: ${state.scan.width} × ${state.scan.height} px`, "ok");
}

$("scan-select").addEventListener("change", (e) => e.target.value && loadScan(e.target.value));

$("upload-btn").addEventListener("click", () => $("upload-input").click());
$("upload-input").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  e.target.value = "";
  if (!file) return;
  const sources = await getJSON(`/api/worlds/${state.world}/sources`);
  const iris = Object.keys(sources);
  let source = "";
  if (iris.length) {
    const list = iris.map((iri, i) => `${i + 1}. ${sources[iri].title}`).join("\n");
    const pick = prompt(`Register this scan as a file of which source? (number, or empty for none)\n\n${list}`, "");
    if (pick && iris[parseInt(pick, 10) - 1]) source = iris[parseInt(pick, 10) - 1];
  }
  const fd = new FormData();
  fd.append("file", file);
  if (source) fd.append("source", source);
  setStatus(`uploading ${file.name}…`);
  try {
    const info = await getJSON(`/api/worlds/${state.world}/scans`, { method: "POST", body: fd });
    await loadScans(info.name);
  } catch (err) { setStatus(err.message, "bad"); }
});

// --------------------------------------------------------------------------- scan viewer (zoom / pan / clicks)

function applyView() {
  $("scan-stage").style.transform = `translate(${view.tx}px, ${view.ty}px) scale(${view.k})`;
  renderScanOverlay();
}

function fitScanToPane() {
  const pane = $("scan-view").getBoundingClientRect();
  const img = $("scan-img");
  view.k = Math.min(pane.width / img.naturalWidth, pane.height / img.naturalHeight) * 0.95;
  view.tx = (pane.width - img.naturalWidth * view.k) / 2;
  view.ty = (pane.height - img.naturalHeight * view.k) / 2;
  applyView();
}

// Screen position → full-resolution scan pixel.
function scanPixel(e) {
  const r = $("scan-view").getBoundingClientRect();
  const px = (e.clientX - r.left - view.tx) / view.k;
  const py = (e.clientY - r.top - view.ty) / view.k;
  return [px * state.previewScale, py * state.previewScale];
}

$("scan-view").addEventListener("wheel", (e) => {
  e.preventDefault();
  const r = $("scan-view").getBoundingClientRect();
  const mx = e.clientX - r.left, my = e.clientY - r.top;
  const f = Math.exp(-e.deltaY * 0.0015);
  const k = Math.min(40, Math.max(0.05, view.k * f));
  view.tx = mx - (mx - view.tx) * (k / view.k);
  view.ty = my - (my - view.ty) * (k / view.k);
  view.k = k;
  applyView();
}, { passive: false });

let drag = null;
$("scan-view").addEventListener("mousedown", (e) => {
  if (!state.scan || e.button !== 0) return;
  drag = { x: e.clientX, y: e.clientY, tx: view.tx, ty: view.ty, moved: false, start: scanPixel(e) };
});
window.addEventListener("mousemove", (e) => {
  if (!drag) return;
  const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
  if (!drag.moved && Math.hypot(dx, dy) < 4) return;
  drag.moved = true;
  if (state.mode === "read") {           // in read mode, dragging draws the region to read
    drag.end = scanPixel(e);
    renderScanOverlay();
    return;
  }
  $("scan-view").classList.add("panning");
  view.tx = drag.tx + dx; view.ty = drag.ty + dy;
  applyView();
});
window.addEventListener("mouseup", (e) => {
  if (!drag) return;
  const d = drag;
  drag = null;
  $("scan-view").classList.remove("panning");
  if (state.mode === "read" && d.moved && d.end) return readRegion(d.start, d.end);
  if (!d.moved) onScanClick(d.start);
});

window.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && state.pending) { state.pending = null; renderGcps(); }
});

// --------------------------------------------------------------------------- control points

function onScanClick(pixel) {
  if (!state.scan) return;
  const [x, y] = pixel.map((v) => Math.round(v * 10) / 10);
  if (x < 0 || y < 0 || x > state.scan.width || y > state.scan.height) return;
  if (state.mode === "trace") return traceAt([Math.floor(x), Math.floor(y)]);
  if (state.mode !== "gcp") return;
  state.pending = { ...(state.pending || {}), pixel: [x, y] };
  completePending();
}

function onMapClick(e) {
  if (state.mode !== "gcp" || !state.scan) return;
  const lonlat = [Math.round(e.lngLat.lng * 1e7) / 1e7, Math.round(e.lngLat.lat * 1e7) / 1e7];
  state.pending = { ...(state.pending || {}), lonlat };
  completePending();
}

function completePending() {
  if (state.pending?.pixel && state.pending?.lonlat) {
    state.gcps.push(state.pending);
    state.pending = null;
    refit();
  } else {
    renderGcps();
  }
}

$("method-select").addEventListener("change", refit);
$("expected-input").addEventListener("change", refit);

function georefBody() {
  const expected = parseFloat($("expected-input").value);
  return { gcps: state.gcps, transformation: $("method-select").value,
           ...(expected > 0 ? { expectedWidthM: expected } : {}) };
}

async function refit() {
  state.fit = null;
  const method = $("method-select").value;
  const need = MIN_POINTS[method];
  if (state.gcps.length >= need) {
    try {
      state.fit = await sendJSON(`/api/worlds/${state.world}/scans/${state.scan.name}/georef/fit`, "POST", georefBody());
    } catch (e) {
      state.fit = { error: e.message };
    }
  }
  renderGcps();
}

function renderGcps() {
  const pts = state.fit?.points || [];
  const worst = pts.length ? pts.reduce((a, p, i) => ((p.leave_one_out_m ?? p.residual_m) > (pts[a].leave_one_out_m ?? pts[a].residual_m) ? i : a), 0) : -1;
  const fmt = (v) => (v === undefined ? "—" : v < 10 ? v.toFixed(2) : v.toFixed(0));
  const rows = state.gcps.map((g, i) => `
    <tr class="${i === worst && pts.length > 3 ? "worst" : ""}">
      <td>${i + 1}</td><td>${g.pixel.map((v) => Math.round(v)).join(", ")}</td>
      <td>${g.lonlat.map((v) => v.toFixed(5)).join(", ")}</td>
      <td class="num">${fmt(pts[i]?.residual_m)}</td><td class="num">${fmt(pts[i]?.leave_one_out_m)}</td>
      <td><button data-del="${i}" title="remove">×</button></td></tr>`);
  if (state.pending) {
    rows.push(`<tr class="pending"><td>${state.gcps.length + 1}</td>
      <td>${state.pending.pixel ? state.pending.pixel.map(Math.round).join(", ") : "click the scan"}</td>
      <td>${state.pending.lonlat ? state.pending.lonlat.map((v) => v.toFixed(5)).join(", ") : "click the map"}</td>
      <td></td><td></td><td></td></tr>`);
  }
  $("gcp-table").querySelector("tbody").innerHTML = rows.join("");
  $("gcp-table").querySelectorAll("button[data-del]").forEach((b) => b.addEventListener("click", () => {
    state.gcps.splice(parseInt(b.dataset.del, 10), 1);
    refit();
  }));

  const need = MIN_POINTS[$("method-select").value];
  const acc = state.fit?.accuracy;
  if (state.fit?.error) {
    $("accuracy").innerHTML = `<span class="bad">${escapeHtml(state.fit.error)}</span>`;
  } else if (acc) {
    const loo = acc.leaveOneOutRmse !== undefined ? ` · leave-one-out <strong>${fmt(acc.leaveOneOutRmse)} m</strong>` : "";
    $("accuracy").innerHTML = `RMSE <strong>${fmt(acc.rmse)} m</strong>${loo} · ${acc.controlPoints} points` +
      (acc.method === "leave-one-out" ? " (spline: leave-one-out is the published figure)" : "");
  } else {
    $("accuracy").textContent = `${state.gcps.length} of at least ${need} control points`;
  }
  const checks = state.fit?.checks || [];
  $("checks").innerHTML = checks.map((c) => `<li class="${c.level}">${escapeHtml(c.message)}</li>`).join("");
  const blocking = checks.some((c) => c.level === "error");
  $("save-georef-btn").disabled = !acc || blocking;
  $("warp-btn").disabled = !state.scan?.georeference;
  renderScanOverlay();
  renderMapOverlays();
}

$("save-georef-btn").addEventListener("click", async () => {
  try {
    const r = await sendJSON(`/api/worlds/${state.world}/scans/${state.scan.name}/georef`, "PUT", georefBody());
    setStatus(`georeference saved · RMSE ${r.accuracy.rmse} m`, "ok");
    await loadScans(state.scan.name);
  } catch (e) { setStatus(e.message, "bad"); }
});

$("warp-btn").addEventListener("click", async () => {
  setStatus("warping…");
  $("warp-btn").disabled = true;
  try {
    const r = await sendJSON(`/api/worlds/${state.world}/scans/${state.scan.name}/warp`, "POST", {});
    setStatus(`basemap ${r.raster_source}: ${r.width} × ${r.height} px at ${r.resolution_m} m — pick it in the editor's Basemap menu`, "ok");
    await loadScans(state.scan.name);
  } catch (e) { setStatus(e.message, "bad"); }
  $("warp-btn").disabled = false;
});

// --------------------------------------------------------------------------- overlays

function renderScanOverlay() {
  const svg = $("scan-overlay");
  if (!state.scan) { svg.innerHTML = ""; return; }
  const s = 1 / state.previewScale;            // full-res px → preview px
  const r = 7 / view.k, w = 2 / view.k;
  const parts = state.gcps.map((g, i) => marker(g.pixel[0] * s, g.pixel[1] * s, i + 1, "#5fa8d3"));
  if (state.pending?.pixel) parts.push(marker(state.pending.pixel[0] * s, state.pending.pixel[1] * s, "?", "#e0b45f"));
  if (state.traced?.ringsPx) {
    const d = state.traced.ringsPx.map((ring) => "M" + ring.map(([x, y]) => `${x * s},${y * s}`).join(" L") + " Z").join(" ");
    parts.push(`<path d="${d}" fill="rgba(224,180,95,0.3)" stroke="#e0b45f" stroke-width="${w}" fill-rule="evenodd"/>`);
  }
  if (drag?.end && state.mode === "read") {
    const [x0, y0] = drag.start, [x1, y1] = drag.end;
    parts.push(`<rect x="${Math.min(x0, x1) * s}" y="${Math.min(y0, y1) * s}" width="${Math.abs(x1 - x0) * s}"
      height="${Math.abs(y1 - y0) * s}" fill="rgba(95,168,211,0.15)" stroke="#5fa8d3" stroke-width="${w}"/>`);
  }
  svg.innerHTML = parts.join("");

  function marker(x, y, label, colour) {
    return `<circle cx="${x}" cy="${y}" r="${r}" fill="${colour}" stroke="#fff" stroke-width="${w}"/>
      <text x="${x}" y="${y + r * 0.4}" font-size="${r * 1.1}" text-anchor="middle" fill="#0b0d11"
        font-family="system-ui" font-weight="700">${label}</text>`;
  }
}

let mapMarkers = [];
function renderMapOverlays() {
  if (!map || !map.getSource("footprint")) return;
  mapMarkers.forEach((m) => m.remove());
  mapMarkers = [];
  const add = (lonlat, label, pending) => {
    const el = document.createElement("div");
    el.className = "gcp-marker" + (pending ? " pending" : "");
    el.textContent = label;
    mapMarkers.push(new maplibregl.Marker({ element: el }).setLngLat(lonlat).addTo(map));
  };
  state.gcps.forEach((g, i) => add(g.lonlat, i + 1, false));
  if (state.pending?.lonlat) add(state.pending.lonlat, "?", true);
  map.getSource("footprint").setData(state.fit?.footprint
    ? { type: "Feature", geometry: state.fit.footprint, properties: {} } : { type: "FeatureCollection", features: [] });
  map.getSource("traced").setData(state.traced?.feature || { type: "FeatureCollection", features: [] });
}

// --------------------------------------------------------------------------- modes

document.querySelectorAll(".mode-row .mode-btn").forEach((b) => b.addEventListener("click", () => {
  state.mode = b.dataset.mode;
  document.querySelectorAll(".mode-row .mode-btn").forEach((x) => x.classList.toggle("active", x === b));
  document.querySelectorAll("[data-panel]").forEach((p) => { p.hidden = p.dataset.panel !== state.mode; });
  $("scan-hint").textContent = state.mode === "read"
    ? "scroll to zoom · drag a box around labels" : "scroll to zoom · drag to pan";
}));

$("tolerance-input").addEventListener("input", (e) => { $("tolerance-readout").textContent = e.target.value; });

function updateReadAvailability() {
  const read = state.assist?.read;
  const btn = document.querySelector('.mode-btn[data-mode="read"]');
  btn.disabled = !read?.enabled;
  btn.title = read?.enabled ? `reads with ${read.model} via ${read.provider}`
    : "set OFM_LLM_API_KEY (or OFM_LLM_BASE_URL for a private server) on the backend to enable";
  $("read-hint").textContent = read?.enabled
    ? `Drag a box around labels on the scan to read them with ${read.model} (${read.provider}).`
    : "Label reading is not configured on the backend.";
}

// --------------------------------------------------------------------------- tracing

async function traceAt(seed) {
  if (!state.scan.georeference) { setStatus("save a georeference before tracing", "bad"); return; }
  setStatus("tracing…");
  try {
    const out = await sendJSON(`/api/worlds/${state.world}/scans/${state.scan.name}/trace`, "POST",
      { seed, tolerance: parseInt($("tolerance-input").value, 10) });
    const svg = out.feature.citations[0].selector.find((s) => s.type === "SvgSelector").value;
    state.traced = { feature: out.feature, ringsPx: parseSvgRings(svg), warnings: out.warnings };
    state.nameCitation = null;
    $("feature-name").value = "";
    renderPendingFeature();
    renderScanOverlay();
    renderMapOverlays();
    setStatus(`traced ${out.pixels.toLocaleString()} px`, out.warnings.length ? "bad" : "ok");
  } catch (e) { setStatus(e.message, "bad"); }
}

function parseSvgRings(svg) {
  const d = svg.match(/ d="([^"]+)"/)?.[1] || "";
  return d.split("Z").map((r) => r.trim()).filter(Boolean)
    .map((r) => r.replace(/^M/, "").split(/\s*L/).map((p) => p.trim().split(",").map(Number)));
}

function renderPendingFeature() {
  const box = $("pending-feature");
  box.hidden = !state.traced;
  if (!state.traced) return;
  $("pending-warnings").innerHTML = state.traced.warnings.map((w) => `<p>${escapeHtml(w)}</p>`).join("");
  const cits = [state.traced.feature.citations[0], state.nameCitation].filter(Boolean);
  $("feature-citations").textContent = "Citations: " + cits.map((c) => `${c.method} from ${c.file} (${c.supports.join(", ")})`).join(" · ");
}

async function loadLayers() {
  const layers = await getJSON(`/api/worlds/${state.world}/layers`).catch(() => []);
  $("target-layer").innerHTML = layers.map((l) => `<option value="${l.name}">${l.name}</option>`).join("") +
    `<option value="__new__">new layer…</option>`;
}

$("add-feature-btn").addEventListener("click", async () => {
  let layer = $("target-layer").value;
  if (layer === "__new__") {
    layer = prompt("New layer name (letters, digits, underscore):", "traced") || "";
    if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(layer)) return;
  }
  const f = state.traced.feature;
  const name = $("feature-name").value.trim();
  const feature = { ...f, properties: { ...(name ? { name } : {}) },
                    citations: [f.citations[0], ...(state.nameCitation && name ? [state.nameCitation] : [])] };
  try {
    const r = await sendJSON(`/api/worlds/${state.world}/layers/${layer}/features`, "POST", { features: [feature] });
    setStatus(`added to ${layer} (id ${r.ids[0]})`, "ok");
    state.traced = null;
    state.nameCitation = null;
    renderPendingFeature();
    renderScanOverlay();
    renderMapOverlays();
    await loadLayers();
    $("target-layer").value = layer;
  } catch (e) { setStatus(e.message, "bad"); }
});

$("discard-feature-btn").addEventListener("click", () => {
  state.traced = null;
  renderPendingFeature();
  renderScanOverlay();
  renderMapOverlays();
});

// --------------------------------------------------------------------------- reading labels

async function readRegion(a, b) {
  const x = Math.max(0, Math.round(Math.min(a[0], b[0]))), y = Math.max(0, Math.round(Math.min(a[1], b[1])));
  const w = Math.min(state.scan.width - x, Math.round(Math.abs(b[0] - a[0])));
  const h = Math.min(state.scan.height - y, Math.round(Math.abs(b[1] - a[1])));
  if (w < 4 || h < 4) return;
  setStatus("reading labels…");
  $("labels").innerHTML = "";
  try {
    const out = await sendJSON(`/api/worlds/${state.world}/scans/${state.scan.name}/read`, "POST",
      { xywh: [x, y, w, h], hint: $("read-context").value.trim() || undefined });
    $("labels").innerHTML = out.labels.length
      ? out.labels.map((l, i) => `<li><span class="text">${escapeHtml(l.text)}</span><span class="kind">${l.kind} · ${Math.round(l.confidence * 100)}%</span>
          <button data-use="${i}" ${state.traced ? "" : "disabled title='trace a feature first'"}>use as name</button></li>`).join("")
      : `<li class="hint">no labels found</li>`;
    $("labels").querySelectorAll("button[data-use]").forEach((btn) => btn.addEventListener("click", () => {
      $("feature-name").value = out.labels[parseInt(btn.dataset.use, 10)].text;
      state.nameCitation = out.citation;
      renderPendingFeature();
    }));
    setStatus(`read ${out.labels.length} label(s) with ${out.model} — check them against the scan`, "ok");
  } catch (e) { setStatus(e.message, "bad"); }
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// --------------------------------------------------------------------------- start

if (authHeader()) init().catch((e) => setStatus(e.message, "bad"));
else showLogin();
