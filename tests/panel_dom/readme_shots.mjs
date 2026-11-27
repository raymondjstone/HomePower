// Takes the screenshots in the README (docs/images/): the real panel in headless Chromium, with a Home Assistant light theme and
// icons, fed a made-up home. Every name, number and address in them is invented.
//   python tests/ha_harness/dump_demo_payload.py tests/panel_dom/demo.json
//   npm install --no-save playwright @mdi/js && npx playwright install chromium      (in this folder)
//   node tests/panel_dom/readme_shots.mjs
import fs from "fs";
import http from "http";
import path from "path";
import { fileURLToPath } from "url";
import { chromium } from "playwright";
import * as mdi from "@mdi/js";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const FRONT = path.join(HERE, "..", "..", "custom_components", "homepower", "frontend");
const OUT = path.join(HERE, "..", "..", "docs", "images");
fs.mkdirSync(OUT, { recursive: true });
const demo = JSON.parse(fs.readFileSync(path.join(HERE, "demo.json"), "utf8"));

const NOW = Date.now(), H = 3600e3;
const ids = ["garage", "utility"];
const g = demo.get, lv = g.live;
g.devices.forEach((d, i) => { d.connection = { host: `192.168.1.${50 + i}`, port: 502, unit_id: 1 }; });
g.entity_ids = { battery_power: "sensor.bp", grid_power: "sensor.gp", devices: Object.fromEntries(ids.map((i) => [i, { soc: `sensor.${i}_soc`, power: `sensor.${i}_power` }])) };
for (const [i, id] of ids.entries()) Object.assign(lv.devices[id], { load_w: 0, last_read_ok: new Date(NOW).toISOString() });
lv.savings = { today: 1.42, month: 31.8 };
lv.alerts = []; lv.notes = []; lv.setup = [];
lv.readiness = { usable_kwh: 10.2, hours: 14, capped: false, basis: "your usual use" };
lv.combined.load_w = 1620; lv.combined.runtime_h = 6.8;
lv.startup = "";
lv.events = [["events", "Schedule Cheap night charge finished"], ["garage", "Handed back to the battery's own control"], ["utility", "Handed back to the battery's own control"]]
  .map(([a, b], i) => [new Date(NOW - (3 - i) * 0.9 * H).toISOString(), a, b]);

// what the battery was told and what it did over the last 24 hours
const shape = (hoursAgo) => (hoursAgo > 9 && hoursAgo < 15 ? 1900 : hoursAgo > 15 && hoursAgo < 18 ? -1500 : null);
lv.plan_actual = Object.fromEntries(ids.map((id, d) => [id, { match_pct: 93 - d * 4, rows: Array.from({ length: 48 }, (_, i) => {
  const ago = (48 - i) / 2, want = shape(ago);
  return [new Date(NOW - ago * H).toISOString(), want, Math.round(want == null ? -350 - 180 * Math.sin(i / 3 + d) : want * (0.96 - d * 0.04) + 60 * Math.sin(i + d))];
}) }]));

const stats = (msg) => {
  const start = Date.parse(msg.start_time), end = Date.parse(msg.end_time), step = msg.period === "hour" ? H : 300e3;
  const out = {};
  for (const id of msg.statistic_ids) {
    out[id] = [];
    for (let t = start; t <= end; t += step) {
      const ago = (NOW - t) / H, d = id.includes("utility") ? 1 : 0, p = shape(ago % 24) ?? -420 - 260 * Math.sin(t / 2.1e6 + d);
      const soc = Math.max(15, Math.min(95, 55 + 30 * Math.cos(((ago - 9 - d) / 24) * 2 * Math.PI)));
      const mean = id.endsWith("_soc") ? soc : id === "sensor.gp" ? 380 + 300 * Math.sin(t / 1.4e6) : id === "sensor.bp" ? p * 1.8 : p * (1 - d * 0.2);
      out[id].push({ start: t, mean });
    }
  }
  return out;
};

const icons = Object.fromEntries(Object.entries(mdi));
const camel = (n) => "mdi" + n.replace(/^mdi:/, "").split("-").map((w) => w[0].toUpperCase() + w.slice(1)).join("");
const used = new Set(["mdi:menu"]);
for (const f of fs.readdirSync(FRONT)) for (const m of fs.readFileSync(path.join(FRONT, f), "utf8").matchAll(/mdi:[a-z0-9-]+/g)) used.add(m[0]);
const paths = Object.fromEntries([...used].map((n) => [n, icons[camel(n)] || ""]));

const THEME = `--primary-color:#03a9f4;--primary-background-color:#fafafa;--secondary-background-color:#e5e5e5;--card-background-color:#fff;
--primary-text-color:#212121;--secondary-text-color:#727272;--disabled-text-color:#bdbdbd;--divider-color:rgba(0,0,0,.12);
--text-primary-color:#fff;--app-header-background-color:#03a9f4;--app-header-text-color:#fff;--ha-card-box-shadow:0 1px 4px rgba(0,0,0,.16);--warning-color:#ffa600`;

const page_html = `<!doctype html><html style="${THEME}"><body style="margin:0;background:#fafafa;font-family:Roboto,Segoe UI,Arial,sans-serif"><script type="module">
  const ICONS = ${JSON.stringify(paths)};
  const svg = (p) => '<svg viewBox="0 0 24 24" style="width:var(--mdc-icon-size,20px);height:var(--mdc-icon-size,20px);fill:currentColor;vertical-align:middle"><path d="' + p + '"/></svg>';
  customElements.define("ha-icon", class extends HTMLElement { connectedCallback() { this.style.display = "inline-flex"; this.innerHTML = svg(ICONS[this.getAttribute("icon")] || ""); } });
  customElements.define("ha-menu-button", class extends HTMLElement { connectedCallback() { this.style.display = "inline-flex"; this.innerHTML = svg(ICONS["mdi:menu"]); } });
  const demo = ${JSON.stringify(demo)};
  const stats = ${stats.toString()};
  const H = 3600e3, NOW = ${NOW};
  const shape = ${shape.toString()};
  const reply = (m) => m.type === "recorder/statistics_during_period" ? stats(m)
    : ({ "homepower/get": demo.get, "homepower/diagnostics": demo.diag, "homepower/preview": demo.preview, "homepower/tariffs/compare": demo.tariffs, "homepower/backups": { backups: [] } }[m.type] ?? null);
  await import("/homepower-panel.js?v=shots");
  const el = document.createElement("homepower-panel"); document.body.appendChild(el);
  el.hass = { states: {}, language: "en-GB", config: { time_zone: "Europe/London" }, callWS: async (m) => JSON.parse(JSON.stringify(reply(m))),
    connection: { subscribeMessage: (cb) => { setTimeout(() => cb(demo.get.live), 20); return Promise.resolve(() => {}); } } };
  window.__panel = el;
</script></body></html>`;

const server = http.createServer((req, res) => {
  const url = new URL(req.url, "http://x");
  if (url.pathname === "/") { res.writeHead(200, { "content-type": "text/html; charset=utf-8" }); return res.end(page_html); }
  const file = path.join(FRONT, path.basename(url.pathname));
  if (fs.existsSync(file) && fs.statSync(file).isFile()) { res.writeHead(200, { "content-type": "text/javascript; charset=utf-8" }); return res.end(fs.readFileSync(file)); }
  res.writeHead(404); res.end();
});
await new Promise((r) => server.listen(0, "127.0.0.1", r));
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1100, height: 900 }, locale: "en-GB", timezoneId: "Europe/London" });
const errors = [];
page.on("pageerror", (e) => errors.push(e.message));
await page.goto(`http://127.0.0.1:${server.address().port}/`);
await page.waitForFunction(() => window.__panel && window.__panel._loaded === true, null, { timeout: 20000 });

// live samples for the sparklines and the power chart
await page.evaluate(() => {
  const p = window.__panel, now = Date.now();
  ["garage", "utility"].forEach((id, d) => { p._hist[id] = Array.from({ length: 180 }, (_, i) => ({ t: now - (180 - i) * 10e3, p: Math.round((d ? -600 : -900) + 160 * Math.sin(i / 11 + d * 2) + 60 * Math.sin(i / 3)) })); });
});

const shot = async (tab, file, height) => {
  await page.evaluate(async (t) => { const p = window.__panel; p.tab = t; await p._tabLoad(); p._render(); }, tab);
  await page.waitForTimeout(250);
  const full = await page.evaluate(() => Math.ceil(window.__panel.shadowRoot.getElementById("app").getBoundingClientRect().height));
  await page.setViewportSize({ width: 1100, height: Math.min(height, full) });
  await page.screenshot({ path: path.join(OUT, file) });
};
await shot("overview", "overview.png", 945);
await shot("plan", "plan.png", 1000);
await shot("schedules", "schedules.png", 800);
await shot("batteries", "batteries.png", 700);
await shot("insights", "insights.png", 1100);

await browser.close(); server.close();
if (errors.length) { console.log(errors.join("\n")); process.exit(1); }
console.log("screenshots in", OUT);
