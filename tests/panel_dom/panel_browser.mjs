// The panel in a real browser (headless Chromium): served over HTTP with a proper module MIME type, loaded with a ?v= tag the way
// Home Assistant loads it, fed the real backend payloads. Checks every part loads, there are no console errors, every tab renders,
// and takes a screenshot of each tab.
// Needs: npm install playwright && npx playwright install chromium; payload.json from tests/ha_harness/dump_panel_payload.py.
import fs from "fs";
import http from "http";
import path from "path";
import { fileURLToPath } from "url";
import { chromium } from "playwright";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const FRONT = path.join(HERE, "..", "..", "custom_components", "homepower", "frontend");
const PAYLOAD = process.argv[2] || path.join(HERE, "payload.json");
const SHOTS = process.argv[3] || path.join(HERE, "shots");
fs.mkdirSync(SHOTS, { recursive: true });
const payload = JSON.parse(fs.readFileSync(PAYLOAD, "utf8"));

// make the one battery look like a managed EcoFlow one so the newer cards have something to show
const lv = payload.get.live;
const a = (lv.devices.a ||= {});
Object.assign(a, { why: null, measured_charge_w: 2700, charge_verdict: "works", awaiting_confirm: false,
  traces: [{ at: "2026-03-01T10:00:00", mode: "charge", ok: true, reaction_s: 40, peak_w: 2800 }],
  setting_changes: [{ at: "2026-03-01 10:00:00", key: "workMode", from: "TIMER_TASK", to: "SELF_CONSUMPTION" }],
  report: { sent: 6, worked: 5, avg_reaction_s: 40, charge_rate_w: 2700, follows_plan_pct: 88, verdict: "works", advice: "Control looks reliable: it reacts to commands and follows the plan." } });
const now = Date.now();
lv.plan_actual = { a: { match_pct: 88, rows: Array.from({ length: 48 }, (_, i) => [new Date(now - (48 - i) * 1800e3).toISOString(), i > 20 && i < 36 ? 2000 : null, i > 22 && i < 38 ? 1900 + (i % 5) * 40 : -150]) } };
lv.weather = { status: "raising the overnight target", tomorrow_c: -2, source: "weather entity (daily)", lately_c: 6, k_wh_per_degree: 800, days_learned: 14, extra_wh: 2100 };
lv.startup = "Started. Restored 2 learned charge rates, 3 command results.";

const types = { ".js": "text/javascript; charset=utf-8", ".json": "application/json", ".html": "text/html; charset=utf-8" };
const requests = [];
const server = http.createServer((req, res) => {
  const url = new URL(req.url, "http://x");
  requests.push({ path: url.pathname, query: url.search });
  if (url.pathname === "/") {
    res.writeHead(200, { "content-type": types[".html"] });
    return res.end(`<!doctype html><html><body style="margin:0;background:#fff"><script type="module">
      window.__calls = [];
      const get = ${JSON.stringify(payload.get)}, diag = ${JSON.stringify(payload.diag)}, preview = ${JSON.stringify(payload.preview)}, tariffs = ${JSON.stringify(payload.tariffs)};
      const reply = (m) => ({ "homepower/get": get, "homepower/diagnostics": diag, "homepower/preview": preview, "homepower/tariffs/compare": tariffs, "homepower/backups": { backups: [] } }[m.type] ?? {});
      await import("/homepower-panel.js?v=1.2.3-browser");
      const el = document.createElement("homepower-panel"); document.body.appendChild(el);
      el.hass = { states: {}, language: "en", callWS: async (m) => { window.__calls.push(m.type); window.__scanHosts = window.__scanHosts || [];
        if (m.type === "homepower/scan_ports" && m.wide) {
          return { host: m.host, address: m.host, wide: true, total: 10000, complete: true, counts: { open: 2, closed: 9998, "no answer": 0, skipped: 0 },
                   results: [{ port: 502, service: "Modbus TCP", state: "open", banner: "" }, { port: 4321, service: "", state: "open", banner: "EF" }] };
        }
        if (m.type === "homepower/scan_ports") {                      // first scan: all closed; the second: Modbus and a web page are open
          window.__scanHosts.push(m.host); const n = window.__scanHosts.length, st = (p, on) => (n > 1 && on ? "open" : "closed");
          return { host: m.host, address: m.host, results: [[22, "SSH", 0], [80, "HTTP", 1], [502, "Modbus TCP", 1], [1883, "MQTT", 0], [8883, "MQTT over TLS", 0]].map(([port, service, on]) => ({ port, service, state: st(port, on), banner: on && n > 1 && port === 80 ? "nginx" : "" })) };
        }
        return JSON.parse(JSON.stringify(reply(m))); },
                  connection: { subscribeMessage: (cb) => { setTimeout(() => cb(get.live), 20); return Promise.resolve(() => {}); } } };
      window.__panel = el; window.__ready = true;
    </script></body></html>`);
  }
  const file = path.join(FRONT, path.basename(url.pathname));
  if (fs.existsSync(file) && fs.statSync(file).isFile()) {
    res.writeHead(200, { "content-type": types[path.extname(file)] || "application/octet-stream" });
    return res.end(fs.readFileSync(file));
  }
  res.writeHead(404); res.end("not found");
});
await new Promise((r) => server.listen(0, "127.0.0.1", r));
const base = `http://127.0.0.1:${server.address().port}/`;

let failures = 0;
const must = (name, ok, extra = "") => { console.log(ok ? "ok  " : "FAIL", name, ok ? "" : extra); if (!ok) failures++; };
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
const errors = [], dialogs = [];
page.on("dialog", (d) => { dialogs.push(d.message()); d.accept(); });      // the panel asks before Safe mode, Pause and similar
page.on("console", (m) => { if (["error", "warning"].includes(m.type())) errors.push(`${m.type()}: ${m.text()}`); });
page.on("pageerror", (e) => errors.push("pageerror: " + e.message));
page.on("requestfailed", (r) => errors.push("requestfailed: " + r.url()));
page.on("response", (r) => { if (r.status() >= 400) errors.push(`http ${r.status()}: ${r.url()}`); });

await page.goto(base);
await page.waitForFunction(() => window.__ready === true, null, { timeout: 15000 });
await page.waitForFunction(() => window.__panel._loaded === true, null, { timeout: 15000 });

const parts = ["homepower-panel.js", "panel-util.js", ...["overview", "charts", "schedules", "batteries", "plan", "insights", "diag", "settings"].map((f) => `panel-${f}.js`)];
const fetched = new Set(requests.map((r) => r.path.slice(1)));
must("every part of the panel was fetched by the browser", parts.every((p) => fetched.has(p)), parts.filter((p) => !fetched.has(p)).join(", "));
must("every part was fetched with the version tag", requests.filter((r) => /^\/(panel-.*|homepower-panel)\.js$/.test(r.path)).every((r) => r.query === "?v=1.2.3-browser"), JSON.stringify(requests.filter((r) => r.query !== "?v=1.2.3-browser")));

const tabs = ["overview", "plan", "schedules", "batteries", "insights", "diagnostics", "settings"];
for (const tab of tabs) {
  await page.evaluate(async (t) => { const p = window.__panel; p.tab = t; await p._tabLoad(); p._render(); }, tab);
  await page.waitForTimeout(150);
  const info = await page.evaluate(() => {
    const root = window.__panel.shadowRoot, app = root.getElementById("app");
    return { text: app.innerText, h: app.scrollHeight, cards: root.querySelectorAll(".card").length, svgs: root.querySelectorAll("svg").length,
             overflowX: document.documentElement.scrollWidth > document.documentElement.clientWidth + 2 };
  });
  must(`tab ${tab}: renders (${info.cards} cards, ${info.svgs} charts)`, info.cards > 0 && !/undefined|NaN|\[object Object\]/.test(info.text), (info.text.match(/.{0,40}(undefined|NaN|\[object Object\]).{0,40}/) || [""])[0]);
  await page.screenshot({ path: path.join(SHOTS, `${tab}.png`), fullPage: true });
  if (tab === "settings") {                                   // a close-up of the newer settings groups
    for (const [title, file] of [["Cold weather", "settings-weather.png"]]) {
      const card = page.locator(".card", { has: page.locator("h2", { hasText: title }) }).first();
      if (await card.count()) await card.screenshot({ path: path.join(SHOTS, file) });
    }
  }
}

// real clicks through the browser's own event system
await page.evaluate(() => { window.__panel.tab = "overview"; window.__panel._render(); });
await page.locator('[data-act="safe"]').first().click();
await page.waitForTimeout(100);
await page.locator('[data-tab="insights"]').first().click();
await page.waitForTimeout(300);
const onInsights = await page.evaluate(() => window.__panel.tab);
must("a real click on a tab switches to it", onInsights === "insights", onInsights);
const calls = await page.evaluate(() => window.__calls);
must("a real click on Safe mode asked for confirmation and reached the backend call", calls.includes("homepower/safe") && dialogs.some((d) => d.includes("Safe mode")), calls.join(",") + " | " + dialogs.join(" / "));

// the port-scan card, with real typing and real clicks
await page.evaluate(async () => { const p = window.__panel; p.tab = "diagnostics"; await p._tabLoad(); p._render(); });
await page.locator("#ps-host").fill("192.168.1.50");
await page.locator('[data-act="scan-ports"]').click();
await page.waitForFunction(() => window.__panel.shadowRoot.textContent.includes("Nothing is listening"), null, { timeout: 5000 });
await page.locator("#ps-host").fill("192.168.1.50");
await page.locator('[data-act="scan-ports"]').click();
await page.waitForFunction(() => window.__panel.shadowRoot.textContent.includes("Changed since"), null, { timeout: 5000 });
const portText = await page.evaluate(() => window.__panel.shadowRoot.textContent);
must("port scan: the second scan shows 502 and 80 open and what changed", portText.includes("2 open: 80 (HTTP), 502 (Modbus TCP)") && portText.includes("502 is now open") && portText.includes("nginx"));
await page.locator(".card", { has: page.locator("h2", { hasText: "Find local control ports" }) }).first().screenshot({ path: path.join(SHOTS, "diagnostics-ports.png") });
await page.locator("#ps-host").fill("192.168.1.50");
await page.locator('[data-act="scan-ports-wide"]').click();
await page.waitForFunction(() => window.__panel.shadowRoot.textContent.includes("Checked ports 1 to 10,000"), null, { timeout: 5000 });
const wideText = await page.evaluate(() => window.__panel.shadowRoot.textContent);
must("wide scan: real click, shows the open ports and the counts", wideText.includes("2 open: 502 (Modbus TCP), 4321") && wideText.includes("9,998 closed") && wideText.includes("wide scan"));
await page.locator(".card", { has: page.locator("h2", { hasText: "Find local control ports" }) }).first().screenshot({ path: path.join(SHOTS, "diagnostics-ports-wide.png") });

must("no console errors, warnings or failed requests", errors.length === 0, errors.slice(0, 5).join(" | "));
await browser.close(); server.close();
console.log("screenshots in", SHOTS);
process.exit(failures ? 1 : 0);
