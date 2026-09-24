// Runtime configuration — override per deployment.
//
// Local dev:    OFM_API_URL = "http://localhost:8765"
// Remote prod:  OFM_API_URL = "http://51.15.160.236:8765"
// Netlify:      OFM_API_URL is substituted at deploy time from a Netlify env var.
//
// Credentials are NOT embedded here. The SPA shows a login overlay on first
// load and stores the entered user/password in sessionStorage so they survive
// page reloads but not browser-tab closes.
window.OFM_API_URL = window.OFM_API_URL || "http://51.15.160.236:8765";
