"""Web admin UI — edit config.json from a browser.

Serves a single settings page (default port 8080) where users, ports, API
secrets, Tidepool sources, and display thresholds can be edited. Saving
validates the new config, writes it atomically, and exits the process so
systemd restarts the app with the new settings (Restart=always).

Protected with HTTP Basic auth when config.admin.password is set.
"""

import base64
import html
import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

import secrets as secrets_mod

from . import config as config_mod
from . import network, predict, synclog
from .config import SCREEN_PNG, Config, merged_thresholds
from .store import Store

log = logging.getLogger("trio_monitor.webadmin")

PAGE_STYLE = """
:root, [data-theme=dark] { color-scheme: dark;
  --bg:#0d1117; --card:#161b22; --line:#2d333b; --fg:#ebeef1; --dim:#9aa4af;
  --faint:#6e7681; --accent:#58a6ff; --btn:#238636; --danger:#f85149; }
[data-theme=light] { color-scheme: light;
  --bg:#f4f6f8; --card:#ffffff; --line:#c6ccd3; --fg:#1a2027; --dim:#5c6670;
  --faint:#8a939c; --accent:#0969da; --btn:#1a7f37; --danger:#ce2626; }
body { font-family: -apple-system, system-ui, sans-serif; background: var(--bg);
       color: var(--fg); max-width: 760px; margin: 1.5rem auto; padding: 0 1rem; }
h1 { font-size: 1.3rem; } h2 { font-size: 1.05rem; margin-top: 1.8rem; color: var(--dim); }
nav { display:flex; gap:.7rem; align-items:center; margin-bottom:1rem; flex-wrap:wrap; }
nav a, nav button.link { color: var(--dim); background:none; border:1px solid var(--line);
  border-radius:8px; padding:.3rem .7rem; font-size:.85rem; text-decoration:none;
  cursor:pointer; margin:0; }
fieldset { border: 1px solid var(--line); border-radius: 8px; margin: 1rem 0; padding: 1rem; }
legend { padding: 0 .5rem; color: var(--accent); }
label { display: inline-block; width: 11rem; color: var(--dim); }
input, select { background: var(--card); color: var(--fg); border: 1px solid var(--line);
        border-radius: 6px; padding: .35rem .5rem; margin: .2rem 0; width: 16rem; }
input.short { width: 6rem; }
.row { margin: .15rem 0; }
button { background: var(--btn); color: white; border: 0; border-radius: 6px;
         padding: .6rem 1.4rem; font-size: 1rem; cursor: pointer; margin-top: 1rem; }
button.minor { background: none; border: 1px solid var(--line); color: var(--dim);
         padding: .3rem .8rem; font-size: .85rem; margin-top: .5rem; }
button.danger { border-color: var(--danger); color: var(--danger); }
img.screen { width: 100%; border: 1px solid var(--line); border-radius: 8px; margin-top: .5rem; }
.status { color: var(--dim); font-size: .9rem; margin: .2rem 0; }
.note { color: var(--faint); font-size: .85rem; }
table { width:100%; border-collapse:collapse; font-size:.85rem; }
td, th { padding:.35rem .5rem; border-bottom:1px solid var(--line); text-align:left; }
th { color: var(--dim); font-weight:600; }
td.err { color: var(--danger); }
td.time { white-space:nowrap; color: var(--dim); }
"""

THEME_SCRIPT = """<script>
(function(){
  const t = localStorage.theme ||
    (matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark');
  document.documentElement.dataset.theme = t;
  window.toggleTheme = function(){
    const n = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
    localStorage.theme = n;
    document.documentElement.dataset.theme = n;
  };
})();
</script>"""

NAV_HTML = """<nav><a href="/">Dashboard</a><a href="/settings">Settings</a>
<a href="/log">Sync log</a>
<button class="link" type="button" onclick="toggleTheme()">Theme</button></nav>"""

SETTINGS_SCRIPT = """<script>
setInterval(() => {
  const img = document.getElementById('screen');
  if (img) img.src = '/screen.png?t=' + Date.now();
}, 5000);
function updateSrc(sel) {
  document.querySelectorAll('.srcgrp[data-i="' + sel.dataset.i + '"]').forEach(g => {
    g.style.display = g.dataset.kind.split(' ').includes(sel.value) ? '' : 'none';
  });
}
function initSrc(sel) { sel.addEventListener('change', () => updateSrc(sel)); updateSrc(sel); }
document.querySelectorAll('.srcsel').forEach(initSrc);
function removePerson(i) {
  document.querySelector('[name=u' + i + '_remove]').value = '1';
  document.getElementById('fs' + i).style.display = 'none';
}
function addPerson() {
  let maxI = -1, maxPort = 1336;
  document.querySelectorAll('fieldset.person').forEach(fs => {
    maxI = Math.max(maxI, +fs.dataset.i || 0);
    const p = fs.querySelector('[name$=_port]');
    if (p && +p.value) maxPort = Math.max(maxPort, +p.value);
  });
  const i = maxI + 1;
  const markup = document.getElementById('person-template').innerHTML
    .replaceAll('__I__', i).replaceAll('__PORT__', maxPort + 1);
  document.getElementById('people').insertAdjacentHTML('beforeend', markup);
  initSrc(document.querySelector('#fs' + i + ' .srcsel'));
}
</script>"""

LOG_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Trio Monitor sync log</title>__THEME__<style>__STYLE__</style></head><body>
__NAV__
<h1>Sync log</h1>
<p class="note">Most recent first. Cleared when the app restarts.</p>
<table><thead><tr><th>Time</th><th>Person</th><th>Source</th><th>Event</th></tr></thead>
<tbody id="rows"><tr><td colspan="4">loading&hellip;</td></tr></tbody></table>
<script>
async function refreshLog(){
  try {
    const r = await fetch('/api/log.json', {cache:'no-store'});
    const d = await r.json();
    document.getElementById('rows').innerHTML = d.entries.length
      ? d.entries.map(e =>
          `<tr><td class="time">${new Date(e.ts).toLocaleTimeString()}</td>` +
          `<td>${e.user}</td><td>${e.source}</td>` +
          `<td${e.ok ? '' : ' class="err"'}>${e.message}</td></tr>`).join('')
      : '<tr><td colspan="4">no sync activity yet</td></tr>';
  } catch (err) {}
}
refreshLog();
setInterval(refreshLog, 15000);
</script></body></html>"""


DASHBOARD_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Trio Monitor</title>
<style>
:root, [data-theme=dark] {
  --bg:#0d1117; --card:#141a21; --band:#182018; --line:#2d333b;
  --fg:#ebeef1; --dim:#6e7681; --inrange:#3fb950; --high:#d29922;
  --low:#f85149; --urgent:#ff2828;
}
[data-theme=light] {
  --bg:#f4f6f8; --card:#ffffff; --band:#e8edf0; --line:#c6ccd3;
  --fg:#1a2027; --dim:#5c6670; --inrange:#168a3a; --high:#b26c06;
  --low:#ce2626; --urgent:#e20000;
}
* { box-sizing:border-box; margin:0; }
html, body { height:100%; }
body { font-family:-apple-system,system-ui,sans-serif; background:var(--bg);
       color:var(--fg); padding:.8rem; transition:background .2s;
       display:flex; flex-direction:column; overflow:hidden; }
header { display:flex; align-items:center; justify-content:space-between;
         flex:0 0 auto; margin-bottom:.6rem; }
header h1 { font-size:1.05rem; color:var(--dim); font-weight:600; }
header .right { display:flex; gap:.8rem; align-items:center; }
header a, header button { color:var(--dim); background:none; border:1px solid var(--line);
  border-radius:8px; padding:.35rem .7rem; font-size:.85rem; cursor:pointer;
  text-decoration:none; }
#updated { font-size:.75rem; color:var(--dim); }
#updated.err { color:var(--low); }
.grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(320px,1fr));
        gap:.8rem; flex:1 1 auto; min-height:0; }
.card { background:var(--card); border:1px solid var(--line); border-radius:14px;
        padding:.9rem 1rem 1rem; display:flex; flex-direction:column;
        min-height:0; }
.card.urgent { border:3px solid var(--urgent); }
.who { display:flex; justify-content:center; align-items:center; gap:.5rem;
       color:var(--dim); font-weight:600; font-size:clamp(.9rem,2.4vh,1.3rem); }
.dot { width:.6rem; height:.6rem; border-radius:50%; }
.big { display:flex; justify-content:center; align-items:baseline; gap:.6rem;
       font-size:clamp(2.8rem,15vh,8rem); font-weight:800; line-height:1.12; }
.big .arrow { font-size:.55em; font-weight:700; }
.sub { text-align:center; color:var(--dim); margin-bottom:.5rem;
       font-size:clamp(.8rem,2.2vh,1.2rem); }
.chartbox { flex:1 1 auto; min-height:80px; }
.chartbox svg { width:100%; height:100%; display:block; background:var(--band);
      border-radius:10px; }
.strip { display:grid; grid-template-columns:repeat(4,1fr); text-align:center;
         margin-top:.6rem; flex:0 0 auto; }
.strip .lbl, .stats .lbl { font-size:clamp(.62rem,1.6vh,.85rem); color:var(--dim);
         letter-spacing:.05em; }
.strip .val { font-size:clamp(1.1rem,3.6vh,2rem); font-weight:700; }
.stats { display:grid; grid-template-columns:repeat(4,1fr); text-align:center;
         margin-top:.7rem; flex:0 0 auto; }
.stats .val { font-size:clamp(1rem,3vh,1.7rem); font-weight:700; }
.stats .sub2 { font-size:clamp(.6rem,1.5vh,.8rem); color:var(--dim); }
/* Stacked (narrow) layout: viewport can't fit both cards — allow scrolling. */
@media (max-width:719px) {
  body { overflow-y:auto; }
  .grid { grid-auto-rows:minmax(85vh,auto); }
  .big { font-size:clamp(2.8rem,10vh,6rem); }
}
</style></head><body>
<header>
  <h1>Trio Monitor</h1>
  <div class="right">
    <span id="updated">loading&hellip;</span>
    <button id="theme">&#9788;</button>
    <a href="/log">Log</a>
    <a href="/settings">Settings</a>
  </div>
</header>
<div class="grid" id="grid"></div>
<script>
const ARROWS = {DoubleUp:"\\u2191\\u2191", SingleUp:"\\u2191", FortyFiveUp:"\\u2197",
  Flat:"\\u2192", FortyFiveDown:"\\u2198", SingleDown:"\\u2193", DoubleDown:"\\u2193\\u2193"};
const html = document.documentElement;
function applyTheme(t){ html.dataset.theme = t;
  document.getElementById('theme').innerHTML = t === 'dark' ? '&#9788;' : '&#9789;'; }
applyTheme(localStorage.theme ||
  (matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark'));
document.getElementById('theme').onclick = () => {
  const t = html.dataset.theme === 'dark' ? 'light' : 'dark';
  localStorage.theme = t; applyTheme(t);
};

function colorFor(v, th, stale){
  if (v == null || stale) return 'var(--dim)';
  if (v <= th.urgent_low || v >= th.urgent_high) return 'var(--urgent)';
  if (v < th.low) return 'var(--low)';
  if (v > th.high) return 'var(--high)';
  return 'var(--inrange)';
}
function age(now, then){
  if (!then) return '--';
  const m = Math.floor((now - then) / 60000);
  if (m < 1) return 'now';
  if (m < 60) return m + 'm ago';
  if (m < 1440) return Math.floor(m/60) + 'h' + String(m%60).padStart(2,'0') + 'm ago';
  return Math.floor(m/1440) + 'd ago';
}
function chart(u, th, now, W, H){
  const hasF = u.forecast && u.forecast.series.length;
  const t0 = now - 180*60000, t1 = now + (hasF ? 120*60000 : 0);
  const pts = u.history || [];
  const fpts = hasF ? u.forecast.series : [];
  const vals = pts.concat(fpts).map(p => p[1]);
  if (!vals.length) return `<svg viewBox="0 0 ${W} ${H}"></svg>`;
  const lo = Math.min(Math.min(...vals), th.low) - 10;
  const hi = Math.max(Math.max(...vals), th.high) + 10;
  const X = t => Math.max(0, Math.min(W, (t - t0) / (t1 - t0) * W));
  const Y = v => H - (v - lo) / (hi - lo) * H;
  const r = Math.max(2, Math.min(3.5, H / 60));
  let s = `<svg viewBox="0 0 ${W} ${H}">`;
  for (const b of [th.low, th.high]) {
    s += `<line x1="4" x2="${W-4}" y1="${Y(b)}" y2="${Y(b)}" stroke="var(--line)"/>`;
    s += `<text x="${W-6}" y="${Y(b)-3}" font-size="11" fill="var(--dim)" text-anchor="end">${b}</text>`;
  }
  if (hasF) s += `<line x1="${X(now)}" x2="${X(now)}" y1="3" y2="${H-3}" stroke="var(--line)"/>`;
  if (pts.length > 1)
    s += `<polyline fill="none" stroke="var(--dim)" stroke-width="1" points="${
      pts.map(p => X(p[0]).toFixed(1) + ',' + Y(p[1]).toFixed(1)).join(' ')}"/>`;
  for (const p of pts)
    s += `<circle cx="${X(p[0]).toFixed(1)}" cy="${Y(p[1]).toFixed(1)}" r="${r}" fill="${colorFor(p[1], th, false)}"/>`;
  for (const p of fpts)
    s += `<circle cx="${X(p[0]).toFixed(1)}" cy="${Y(p[1]).toFixed(1)}" r="${(r*0.8).toFixed(1)}" opacity="0.65" fill="${colorFor(p[1], th, false)}"/>`;
  return s + '</svg>';
}
let lastData = null;
function drawCharts(){
  if (!lastData) return;
  document.querySelectorAll('.chartbox').forEach(box => {
    const u = lastData.users[+box.dataset.i];
    if (u) box.innerHTML = chart(u, u.thresholds || lastData.thresholds, lastData.now,
                                 box.clientWidth || 400, box.clientHeight || 130);
  });
}
let resizeTimer;
window.addEventListener('resize', () => {
  clearTimeout(resizeTimer); resizeTimer = setTimeout(drawCharts, 150);
});
function card(u, th, now, idx){
  const staleMs = th.stale_minutes * 60000;
  const stale = !u.sgv_date || now - u.sgv_date > staleMs;
  const ageMin = u.sgv_date ? (now - u.sgv_date) / 60000 : 1e9;
  const dotCol = ageMin <= 7 ? 'var(--inrange)' : ageMin <= th.stale_minutes ? 'var(--high)' : 'var(--low)';
  const col = colorFor(u.sgv, th, stale);
  const urgent = !stale && u.sgv != null && (u.sgv <= th.urgent_low || u.sgv >= th.urgent_high);
  const tilde = u.forecast && u.forecast.source === 'est' ? '~' : '';
  const labels = {30:'+30m', 60:'+1h', 90:'+1.5h', 120:'+2h'};
  let strip = '';
  if (u.forecast && !stale)
    strip = '<div class="strip">' + [30,60,90,120].map(hz => {
      const v = u.forecast.horizons[hz];
      return v == null ? '' : `<div><div class="lbl">${labels[hz]}</div>` +
        `<div class="val" style="color:${colorFor(v, th, false)};opacity:.85">${tilde}${Math.round(v)}</div></div>`;
    }).join('') + '</div>';
  const stat = (lbl, val, sub) =>
    `<div><div class="lbl">${lbl}</div><div class="val">${val}</div>` +
    (sub ? `<div class="sub2">${sub}</div>` : '') + '</div>';
  return `<div class="card${urgent ? ' urgent' : ''}">
    <div class="who">${u.name} <span class="dot" style="background:${dotCol}"></span></div>
    <div class="big" style="color:${col}">${u.sgv != null ? Math.round(u.sgv) : '---'}
      <span class="arrow">${!stale && ARROWS[u.direction] || ''}</span></div>
    <div class="sub">${u.delta != null && !stale ? (u.delta >= 0 ? '+' : '') + Math.round(u.delta) + ' &nbsp; ' : ''}${age(now, u.sgv_date)}</div>
    <div class="chartbox" data-i="${idx}"></div>${strip}
    <div class="stats">
      ${stat('IOB', u.iob != null ? u.iob.toFixed(1) + 'U' : '--')}
      ${stat('COB', u.cob != null ? Math.round(u.cob) + 'g' : '--')}
      ${stat('CARBS', u.last_carbs != null ? Math.round(u.last_carbs) + 'g' : '--',
             u.last_carbs_date ? age(now, u.last_carbs_date) : '')}
      ${stat('BOLUS', u.last_bolus != null ? u.last_bolus.toFixed(2) + 'U' : '--',
             u.last_bolus_date ? age(now, u.last_bolus_date) : '')}
    </div></div>`;
}
async function refresh(){
  const updated = document.getElementById('updated');
  try {
    const r = await fetch('/api/dashboard.json', {cache: 'no-store'});
    if (!r.ok) throw new Error(r.status);
    const d = await r.json();
    lastData = d;
    document.getElementById('grid').innerHTML =
      d.users.map((u, i) => card(u, u.thresholds || d.thresholds, d.now, i)).join('');
    drawCharts();
    updated.textContent = 'updated ' + new Date().toLocaleTimeString();
    updated.classList.remove('err');
  } catch (e) {
    updated.textContent = 'connection lost — retrying';
    updated.classList.add('err');
  }
}
refresh();
setInterval(refresh, 30000);
document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
</script></body></html>"""


class AdminServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, config: Config, config_path: str, store: Store):
        super().__init__(("0.0.0.0", config.admin_port), AdminHandler)
        self.config = config
        self.config_path = str(config_path)
        self.password = config.admin_password
        self.store = store


class AdminHandler(BaseHTTPRequestHandler):
    server: AdminServer
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        log.debug(fmt % args)

    def _send(self, body: bytes, ctype: str, code: int = 200, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        if not self.server.password:
            return True
        header = self.headers.get("Authorization", "")
        if header.startswith("Basic "):
            try:
                decoded = base64.b64decode(header[6:]).decode()
                return decoded.split(":", 1)[1] == self.server.password
            except Exception:
                return False
        return False

    def _deny(self):
        self._send(
            b"Authentication required", "text/plain", 401,
            {"WWW-Authenticate": 'Basic realm="Trio Monitor admin"'},
        )

    # ---- GET ----

    def do_GET(self):
        if not self._authorized():
            self._deny()
            return
        path = self.path.split("?")[0]
        if path == "/":
            self._send(DASHBOARD_HTML.encode(), "text/html; charset=utf-8")
        elif path == "/settings":
            self._send(self._render_page().encode(), "text/html; charset=utf-8")
        elif path == "/log":
            page = (LOG_HTML.replace("__THEME__", THEME_SCRIPT)
                    .replace("__STYLE__", PAGE_STYLE).replace("__NAV__", NAV_HTML))
            self._send(page.encode(), "text/html; charset=utf-8")
        elif path == "/api/log.json":
            self._send(
                json.dumps({"entries": synclog.recent()}).encode(),
                "application/json",
            )
        elif path == "/api/dashboard.json":
            self._send(
                json.dumps(self._dashboard_data()).encode(),
                "application/json",
            )
        elif path == "/screen.png":
            try:
                with open(SCREEN_PNG, "rb") as f:
                    self._send(f.read(), "image/png")
            except OSError:
                self._send(b"no screenshot yet", "text/plain", 404)
        else:
            self._send(b"not found", "text/plain", 404)

    def _dashboard_data(self) -> dict:
        import time
        now_ms = int(time.time() * 1000)
        dc = self.server.config.display
        users = []
        for user in self.server.config.users:
            snap = self.server.store.snapshot(user.name)
            horizons, series, source = predict.predict(snap, now_ms)
            users.append({
                "name": user.name,
                "thresholds": {
                    **merged_thresholds(dc, user),
                    "stale_minutes": dc.stale_minutes,
                },
                "sgv": snap.sgv,
                "sgv_date": snap.sgv_date,
                "direction": snap.direction,
                "delta": snap.delta,
                "iob": snap.iob,
                "cob": snap.cob,
                "last_carbs": snap.last_carbs,
                "last_carbs_date": snap.last_carbs_date,
                "last_bolus": snap.last_bolus,
                "last_bolus_date": snap.last_bolus_date,
                "history": snap.history,
                "forecast": {
                    "horizons": horizons,
                    "series": series,
                    "source": source,
                } if horizons else None,
            })
        return {
            "now": now_ms,
            "units": dc.units,
            "thresholds": {
                "low": dc.low, "high": dc.high,
                "urgent_low": dc.urgent_low, "urgent_high": dc.urgent_high,
                "stale_minutes": dc.stale_minutes,
            },
            "users": users,
        }

    def _user_fieldset(self, i, user: dict, status: str, defaults: dict) -> str:
        e = html.escape
        source = user.get("source") or {}
        stype = source.get("type") or "push"
        selected = lambda kind: "selected" if stype == kind else ""
        ns_key = source.get("api_secret") or source.get("token") or ""
        th = user.get("thresholds") or {}
        th_val = lambda k: e(str(th[k])) if th.get(k) else ""
        legend = e(user.get("name") or "New person")
        return f"""
<fieldset class="person" data-i="{i}" id="fs{i}"><legend>{legend}</legend>
  <input type="hidden" name="u{i}_remove" value="">
  <div class="status">{status}</div>
  <div class="row"><label>Name</label><input name="u{i}_name" value="{e(user.get('name', ''))}"></div>
  <div class="row"><label>Port (Nightscout API)</label><input class="short" name="u{i}_port" value="{user.get('port', '')}"></div>
  <div class="row"><label>API secret</label><input name="u{i}_secret" value="{e(user.get('api_secret', ''))}" placeholder="(blank = generate)"></div>
  <div class="row"><label>Low / High</label>
    <input class="short" name="u{i}_th_low" value="{th_val('low')}" placeholder="{defaults['low']:g}">
    <input class="short" name="u{i}_th_high" value="{th_val('high')}" placeholder="{defaults['high']:g}"></div>
  <div class="row"><label>Urgent low / high</label>
    <input class="short" name="u{i}_th_urgent_low" value="{th_val('urgent_low')}" placeholder="{defaults['urgent_low']:g}">
    <input class="short" name="u{i}_th_urgent_high" value="{th_val('urgent_high')}" placeholder="{defaults['urgent_high']:g}"></div>
  <div class="row"><label>Data source</label>
    <select name="u{i}_source" class="srcsel" data-i="{i}">
      <option value="push" {selected('push')}>Push (Trio / Nightscout upload)</option>
      <option value="tidepool" {selected('tidepool')}>Pull from Tidepool (twiist)</option>
      <option value="nightscout" {selected('nightscout')}>Pull from a Nightscout site</option>
    </select></div>
  <div class="srcgrp" data-i="{i}" data-kind="tidepool">
    <div class="row"><label>Tidepool email</label><input name="u{i}_tp_email" value="{e(source.get('email', ''))}"></div>
    <div class="row"><label>Tidepool password</label><input type="password" name="u{i}_tp_password" value="{e(source.get('password', ''))}"></div>
  </div>
  <div class="srcgrp" data-i="{i}" data-kind="nightscout">
    <div class="row"><label>Nightscout URL</label><input name="u{i}_ns_url" value="{e(source.get('url', ''))}" placeholder="https://mysite.example.com"></div>
    <div class="row"><label>API secret or token</label><input type="password" name="u{i}_ns_key" value="{e(ns_key if stype == 'nightscout' else '')}"></div>
  </div>
  <div class="srcgrp" data-i="{i}" data-kind="tidepool nightscout">
    <div class="row"><label>Poll every (seconds)</label><input class="short" name="u{i}_poll" value="{source.get('poll_seconds', 60)}"></div>
  </div>
  <button type="button" class="minor danger" onclick="removePerson('{i}')">Remove</button>
</fieldset>"""

    def _wifi_section(self) -> str:
        if not network.available():
            return ""
        e = html.escape
        state = network.connectivity()
        status = ("setup hotspot active — pick your home network below"
                  if network.hotspot_active() else f"connectivity: {state}")
        options = "".join(
            f'<option value="{e(n["ssid"])}">'
            f'{e(n["ssid"])} ({n["signal"]}%{"" if n["secured"] else ", open"})'
            "</option>"
            for n in network.wifi_scan()
        ) or "<option value=''>(no networks found)</option>"
        return f"""<h2>Wi-Fi</h2>
<form method="POST" action="/wifi"><fieldset><legend>Network</legend>
  <div class="status">{status}</div>
  <div class="row"><label>Network</label><select name="wifi_ssid">{options}</select></div>
  <div class="row"><label>Password</label><input type="password" name="wifi_password"></div>
  <button type="submit" class="minor">Join network</button>
  <p class="note">Joining a different network changes the Pi's address —
  the display will show the new URL.</p>
</fieldset></form>"""

    def _render_page(self) -> str:
        raw = json.loads(open(self.server.config_path).read())
        display = raw.get("display", {})
        d = lambda key, default: display.get(key, default)
        defaults = {
            "low": d("low", 70), "high": d("high", 180),
            "urgent_low": d("urgent_low", 55), "urgent_high": d("urgent_high", 250),
        }
        import time
        now_ms = int(time.time() * 1000)
        fieldsets = []
        for i, user in enumerate(raw.get("users", [])):
            snap = self.server.store.snapshot(user["name"])
            if snap.sgv_date:
                mins = int((now_ms - snap.sgv_date) / 60000)
                status = f"last reading {snap.sgv:.0f} mg/dL, {mins}m ago"
            else:
                status = "no data yet"
            fieldsets.append(self._user_fieldset(i, user, status, defaults))
        template = self._user_fieldset(
            "__I__", {"port": "__PORT__"}, "not saved yet", defaults
        )
        return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Trio Monitor settings</title>{THEME_SCRIPT}<style>{PAGE_STYLE}</style></head><body>
{NAV_HTML}
<h1>Settings</h1>
<h2>Live display</h2>
<img class="screen" id="screen" src="/screen.png" alt="live display">
{self._wifi_section()}
<form method="POST" action="/save">
<h2>People</h2>
<div id="people">
{''.join(fieldsets)}
</div>
<button type="button" class="minor" onclick="addPerson()">+ Add person</button>
<template id="person-template">{template}</template>
<h2>Display defaults</h2>
<fieldset><legend>Thresholds (mg/dL) — used unless a person overrides them</legend>
  <div class="row"><label>Low</label><input class="short" name="low" value="{d('low', 70)}"></div>
  <div class="row"><label>High</label><input class="short" name="high" value="{d('high', 180)}"></div>
  <div class="row"><label>Urgent low</label><input class="short" name="urgent_low" value="{d('urgent_low', 55)}"></div>
  <div class="row"><label>Urgent high</label><input class="short" name="urgent_high" value="{d('urgent_high', 250)}"></div>
  <div class="row"><label>Stale after (minutes)</label><input class="short" name="stale_minutes" value="{d('stale_minutes', 12)}"></div>
</fieldset>
<h2>Admin</h2>
<fieldset><legend>Web access</legend>
  <div class="row"><label>New admin password</label>
    <input type="password" name="admin_password" value="" placeholder="(leave blank to keep current)"></div>
  <p class="note">Protects this web interface and the API (username: admin).
  After saving with a new password, your browser will ask you to log in again.</p>
</fieldset>
<button type="submit">Save &amp; Apply</button>
<p class="note">Saving restarts the display (takes ~5 seconds). Blank API secrets
are generated automatically; blank per-person thresholds inherit the defaults.</p>
</form>
{SETTINGS_SCRIPT}</body></html>"""

    # ---- POST ----

    def do_POST(self):
        if not self._authorized():
            self._deny()
            return
        post_path = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length") or 0)
        form = {
            k: v[0]
            for k, v in parse_qs(self.rfile.read(length).decode()).items()
        }
        if post_path == "/wifi":
            ssid = form.get("wifi_ssid", "").strip()
            password = form.get("wifi_password", "")
            if not ssid:
                self._send(b"missing ssid", "text/plain", 400)
                return
            synclog.add("network", "system", f"joining Wi-Fi '{ssid}'")
            threading.Thread(
                target=network.connect_wifi, args=(ssid, password), daemon=True
            ).start()
            body = (
                "<!DOCTYPE html><html><head><meta charset='utf-8'>"
                f"<style>{PAGE_STYLE}</style></head><body>"
                f"<h1>Joining {html.escape(ssid)}&hellip;</h1>"
                "<p>If the password is right, the Pi switches networks in a few"
                " seconds and its screen shows the new address. Reconnect your"
                " phone to the same network and open that address.</p>"
                "</body></html>"
            ).encode()
            self._send(body, "text/html; charset=utf-8")
            return
        if post_path != "/save":
            self._send(b"not found", "text/plain", 404)
            return
        try:
            self._save(form)
        except Exception as exc:
            body = (
                f"<h1>Invalid configuration</h1><p>{html.escape(str(exc))}</p>"
                '<p><a href="/">Back</a></p>'
            ).encode()
            self._send(body, "text/html; charset=utf-8", 400)
            return
        body = (
            "<!DOCTYPE html><html><head><meta charset='utf-8'>"
            "<meta http-equiv='refresh' content='8;url=/settings'>"
            f"<style>{PAGE_STYLE}</style></head><body>"
            "<h1>Saved</h1><p>Restarting the display&hellip; "
            "this page reloads in a few seconds.</p></body></html>"
        ).encode()
        self._send(body, "text/html; charset=utf-8")
        # Exit shortly after the response flushes; systemd restarts us with
        # the new config (Restart=always).
        log.info("Config saved from web admin; restarting")
        threading.Timer(0.8, lambda: os._exit(0)).start()

    def _save(self, form: dict) -> None:
        raw = json.loads(open(self.server.config_path).read())
        users = []
        i = 0
        while f"u{i}_name" in form:
            idx = i
            i += 1
            if form.get(f"u{idx}_remove"):
                continue
            name = form[f"u{idx}_name"].strip()
            if not name:
                raise ValueError(f"person {idx + 1} needs a name")
            user = {
                "name": name,
                "port": int(form[f"u{idx}_port"]),
                "api_secret": form.get(f"u{idx}_secret", "").strip()
                              or secrets_mod.token_hex(12),
            }
            thresholds = {}
            for key in ("low", "high", "urgent_low", "urgent_high"):
                value = form.get(f"u{idx}_th_{key}", "").strip()
                if value:
                    thresholds[key] = float(value)
            if thresholds:
                user["thresholds"] = thresholds
            stype = form.get(f"u{idx}_source")
            poll = int(form.get(f"u{idx}_poll", 60) or 60)
            if stype == "tidepool":
                user["source"] = {
                    "type": "tidepool",
                    "email": form.get(f"u{idx}_tp_email", "").strip(),
                    "password": form.get(f"u{idx}_tp_password", ""),
                    "poll_seconds": poll,
                }
            elif stype == "nightscout":
                url = form.get(f"u{idx}_ns_url", "").strip()
                if url and not url.startswith(("http://", "https://")):
                    url = "https://" + url
                # The poller auto-detects whether the key is a classic API
                # secret or an access token, so one field covers both.
                user["source"] = {
                    "type": "nightscout",
                    "url": url,
                    "api_secret": form.get(f"u{idx}_ns_key", "").strip(),
                    "poll_seconds": poll,
                }
            users.append(user)
        if not users:
            raise ValueError("at least one person is required")
        raw["users"] = users
        display = raw.setdefault("display", {})
        for key in ("low", "high", "urgent_low", "urgent_high", "stale_minutes"):
            if form.get(key):
                display[key] = float(form[key])

        new_admin_password = form.get("admin_password", "").strip()
        if new_admin_password:
            if len(new_admin_password) < 6:
                raise ValueError("admin password must be at least 6 characters")
            raw.setdefault("admin", {})["password"] = new_admin_password

        tmp = self.server.config_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(raw, f, indent=2)
            f.write("\n")
        config_mod.load(tmp)  # validate before replacing the live file
        os.replace(tmp, self.server.config_path)


def start_admin(config: Config, config_path, store: Store) -> AdminServer | None:
    if not config.admin_port:
        return None
    server = AdminServer(config, config_path, store)
    thread = threading.Thread(target=server.serve_forever, name="webadmin", daemon=True)
    thread.start()
    log.info("Web admin listening on port %d", config.admin_port)
    return server
