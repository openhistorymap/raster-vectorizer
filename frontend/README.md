# OFM editor — frontend

Pure-static SPA. MapLibre GL JS + mapbox-gl-draw. No build step required;
Netlify (or any static host) serves the files as-is.

## Local dev

```bash
# from /srv/ofm/raster-vectorizer/frontend
python3 -m http.server 5173
# then open http://localhost:5173 — make sure the backend is on :8765
```

The backend defaults to `http://localhost:8765`; override by editing
`config.js` (`window.OFM_API_URL`).

## Deploy on Netlify

1. Connect this directory as a Netlify site (`netlify deploy`, or via the
   web UI). `netlify.toml` declares `publish = "."` and a build step that
   substitutes `OFM_API_URL` from your Netlify env vars into `config.js`.
2. Set `OFM_API_URL` in Netlify → site settings → environment variables
   to the public URL of your backend (e.g. `https://editor-api.example.com`).
3. Set CORS on the backend: `OFM_CORS_ORIGINS=https://your-site.netlify.app`.

## Files

| file              | role |
|-------------------|------|
| `index.html`      | shell — header, map div, side panel |
| `editor.js`       | MapLibre setup, draw lifecycle, REST round-trips |
| `style.css`       | dark editor theme |
| `config.js`       | runtime config (overwritten at Netlify build time) |
| `config.js.tmpl`  | template substituted via envsubst at deploy |
| `netlify.toml`    | build/redirect config |
| `_redirects`      | SPA fallback for client routes |

## Notes on MapLibre + map.json

Each OFM world's `map.json` is a Mapbox-GL style spec — MapLibre loads
those directly. On world-load the editor rewrites URLs pointing at
`statictiles.fantasymaps.org/<slug>/…` to the backend's tile/layer
endpoints, so the editor works even before you publish to the CDN.

`mapbox-gl-draw` (MIT-licensed) is used unchanged on top of MapLibre; the
ABI is compatible between the two.
