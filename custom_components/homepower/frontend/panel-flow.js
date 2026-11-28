/* HomePower panel: part of homepower-panel.js (loaded by it; methods are added to the panel class).
   The Flow tab: the house in the middle, the grid, solar and each battery around it, joined by lines whose dots move the way
   the power is flowing (faster for more power), with the live figures on them.
   The page itself is drawn once (its layout depends only on which batteries there are); each update then only changes the
   figures, colours and speeds in place, so the moving dots never jump back to the start. */
export default (U) => {
  const { esc, fmtW } = U;
  const IDLE = 20, DASH = 20;
  // Wide: grid, house and solar in a row with the batteries below. Narrow (a phone): grid and solar above the house, so the
  // circles stay big enough to read.
  const WIDE = { W: 800, H: 410, R: 54, HOUSE: [400, 100], GRID: [105, 100], SOLAR: [695, 100], BAT_Y: 340, SPAN: 620, STEP: 220 };
  const TALL = { W: 400, H: 560, R: 52, HOUSE: [200, 250], GRID: [78, 80], SOLAR: [322, 80], BAT_Y: 470, SPAN: 300, STEP: 120 };
  // a straight line between two circles, from edge to edge, with its label just beside the middle
  const edge = (a, b, ra, rb) => {
    const dx = b[0] - a[0], dy = b[1] - a[1], len = Math.hypot(dx, dy) || 1, ux = dx / len, uy = dy / len;
    const p = [a[0] + ux * ra, a[1] + uy * ra], q = [b[0] - ux * rb, b[1] - uy * rb];
    return { d: `M${p[0].toFixed(1)},${p[1].toFixed(1)} L${q[0].toFixed(1)},${q[1].toFixed(1)}`, mid: [(p[0] + q[0]) / 2, (p[1] + q[1]) / 2 - (Math.abs(uy) > 0.3 ? 0 : 22)] };
  };
  return {
  // Where everything sits (viewBox units): the same numbers place the SVG lines and the HTML circles over them.
  _flowLayout() {
    const ds = this.data.devices || [], n = ds.length, solar = this._flowHasSolar();
    const w = this.clientWidth || 0, G = w && w < 640 ? TALL : WIDE, { R, HOUSE, BAT_Y } = G, HR = R + 8;
    const span = Math.min(G.SPAN, G.STEP * (n - 1)), r = n > 4 ? Math.max(30, Math.min(R, (G.SPAN / n) * 0.45)) : R;
    const bats = ds.map((d, i) => ({ d, i, x: n === 1 ? HOUSE[0] : HOUSE[0] - span / 2 + (span * i) / (n - 1), y: BAT_Y, r }));
    const lines = [{ key: "grid", ...edge(G.GRID, HOUSE, R, HR) }];
    if (solar) lines.push({ key: "solar", ...edge(G.SOLAR, HOUSE, R, HR) });
    for (const b of bats) {
      const ex = HOUSE[0] + (n > 1 ? ((b.i - (n - 1) / 2) * Math.min(26, 70 / (n - 1))) : 0), ey = HOUSE[1] + HR - 4;
      const sy = b.y - b.r, bend = (sy - ey) * 0.45, c1 = [b.x, sy - bend], c2 = [ex, ey + bend];
      // the label sits on the curve's middle (t = 0.5 of the cubic)
      const mx = (b.x + 3 * c1[0] + 3 * c2[0] + ex) / 8, my = (sy + 3 * c1[1] + 3 * c2[1] + ey) / 8;
      lines.push({ key: "bat:" + b.d.device_id, d: `M${b.x},${sy} C${c1[0]},${c1[1]} ${c2[0]},${c2[1]} ${ex},${ey}`, mid: [mx, my] });
    }
    return { bats, lines, solar, G };
  },

  // Solar is shown once any battery has panels; it then stays, so the page is not redrawn when the sun goes down.
  _flowHasSolar() {
    if (!this._flowSolar) {
      const lvs = Object.values(this.live.devices || {});
      this._flowSolar = lvs.some((lv) => lv && (lv.has_solar || (lv.pv_w || 0) > 0));
    }
    return this._flowSolar;
  },

  _flowView() {
    const ds = this.data.devices || [];
    if (!ds.length) {
      return `<div class="card empty"><ha-icon icon="mdi:home-lightning-bolt-outline"></ha-icon><h3>No batteries yet</h3>
        Add a battery to see how power flows around your home.<br><br><button class="btn pri" data-act="add-battery"><ha-icon icon="mdi:plus"></ha-icon>Add battery</button></div>`;
    }
    const L = this._flowLayout(), { W, H, R, HOUSE, GRID, SOLAR } = L.G;
    const pos = (x, y) => `left:${(x / W) * 100}%;top:${(y / H) * 100}%`;
    const size = (r) => `width:${((2 * r) / W) * 100}%`;
    const node = (id, x, y, r, icon, label, extra = "", attrs = "", css = "") => `<div class="fnode" id="fn-${id}" style="${pos(x, y)};${size(r)}${css}"${attrs}>
      <div class="fcirc"><ha-icon icon="${icon}"></ha-icon><b class="fv">–</b><span class="fs">${label}</span>${extra}</div></div>`;
    const paths = L.lines.map((l) => `<path class="ftrack" d="${l.d}"/><path class="fglow" data-k="${esc(l.key)}" d="${l.d}"/><path class="fdots" data-k="${esc(l.key)}" d="${l.d}" stroke-dasharray="0 ${DASH}"/>`).join("");
    const labels = L.lines.map((l) => `<div class="flbl" data-k="${esc(l.key)}" style="${pos(l.mid[0], l.mid[1])}"></div>`).join("");
    const bats = L.bats.map((b) => node(`bat-${slugId(b.d.device_id)}`, b.x, b.y, b.r, "mdi:home-battery", esc(b.d.name),
      `<span class="fp">–</span><div class="fsoc"><i style="background:${this._color(b.i)}"></i></div>`, this._histAttr("flow"), `;--bc:${this._color(b.i)}`)).join("");
    return `<div class="card flowcard"><h2><ha-icon icon="mdi:home-lightning-bolt-outline"></ha-icon>Power flow <span class="sub" style="font-weight:400;font-size:13px">live</span></h2>
      <div class="flow" style="aspect-ratio:${W}/${H}" role="img" aria-label="Live power flow between the grid, ${L.solar ? "solar, " : ""}the batteries and the house">
        <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet" aria-hidden="true">${paths}</svg>
        ${node("grid", GRID[0], GRID[1], R, "mdi:transmission-tower", "Grid", "", this._histAttr("grid"))}
        ${node("house", HOUSE[0], HOUSE[1], R + 8, "mdi:home", "House")}
        ${L.solar ? node("solar", SOLAR[0], SOLAR[1], R, "mdi:solar-power", "Solar", "", this._histAttr("solar")) : ""}
        ${bats}${labels}
      </div>
      <div class="fsum" id="fsum" aria-live="polite"></div></div>`;
  },

  // This moment's figures (W): + means power moving along the line towards the house.
  _flowNow() {
    const ds = this.data.devices || [];
    const grid = this._gridW();
    const bats = ds.map((d) => { const r = this._reading(d); return { d, r, w: r.online && !r.waiting && r.power != null ? r.power : null }; });
    let pv = null;
    for (const lv of Object.values(this.live.devices || {})) if (lv && lv.online && lv.pv_w != null) pv = (pv || 0) + lv.pv_w;
    let house = this.live.house_w;
    if (house == null && grid != null) house = Math.max(0, grid - bats.reduce((a, b) => a + (b.w || 0), 0) + (pv || 0));
    return { grid, pv, house, bats };
  },

  _flowPatch() {
    const root = this.shadowRoot, box = root.querySelector(".flow");
    if (!box) return;
    const f = this._flowNow(), lines = {};
    const set = (id, v, s) => { const n = root.getElementById("fn-" + id); if (!n) return n; n.querySelector(".fv").textContent = v; if (s != null) n.querySelector(".fs").textContent = s; return n; };
    // grid: + importing flows grid -> house
    const g = set("grid", f.grid == null ? "–" : fmtW(Math.abs(f.grid)), f.grid == null ? "No reading" : Math.abs(f.grid) < IDLE ? "Balanced" : f.grid > 0 ? "Importing" : "Exporting");
    lines.grid = { w: f.grid, col: f.grid > 0 ? U.INK.err : U.INK.charge };
    if (g) g.classList.toggle("off", f.grid == null);
    set("house", f.house == null ? "–" : fmtW(f.house), "House");
    if (root.getElementById("fn-solar")) { set("solar", f.pv == null ? "–" : fmtW(f.pv), "Solar"); lines.solar = { w: f.pv, col: "var(--hp-solar)" }; }
    for (const b of f.bats) {
      const lv = b.r.lv || {}, soc = b.r.soc;
      const n = set(`bat-${slugId(b.d.device_id)}`, soc != null && b.r.online ? `${Math.round(soc)}%` : b.r.online ? "–" : "Offline", b.d.name);
      if (n) {
        n.querySelector(".fp").textContent = b.r.waiting ? "waiting" : !b.r.online ? "" : b.w == null ? "no reading" : Math.abs(b.w) < IDLE ? "idle" : b.w > 0 ? "charging" : "discharging";
        n.classList.toggle("off", !b.r.online);
        const bar = n.querySelector(".fsoc i"); if (bar) bar.style.width = `${Math.max(0, Math.min(100, soc || 0))}%`;
        n.title = `${b.d.name}: ${soc != null ? Math.round(soc) + "% · " : ""}${b.w == null ? (b.r.online ? "no power reading" : "offline") : Math.abs(b.w) < IDLE ? "idle" : (b.w > 0 ? "charging " : "discharging ") + fmtW(Math.abs(b.w))}${lv.temp_c != null ? ` · ${lv.temp_c.toFixed(0)}°C` : ""}`;
      }
      // a battery discharging (-) sends power towards the house
      lines["bat:" + b.d.device_id] = { w: b.w == null ? null : -b.w, col: b.w > 0 ? U.INK.charge : U.INK.discharge };
    }
    const reduce = this._flowReduced();
    this._flowSpeed = {};
    for (const p of root.querySelectorAll(".fdots, .fglow")) {
      const l = lines[p.dataset.k] || { w: null }, on = l.w != null && Math.abs(l.w) >= IDLE;
      const a = on ? Math.min(1, Math.sqrt(Math.abs(l.w) / 6000)) : 0;      // 0..1: how strong this flow is
      p.style.stroke = on ? l.col : "";
      p.classList.toggle("on", on);
      if (p.classList.contains("fdots")) {
        p.style.strokeWidth = on ? (4 + 6 * a).toFixed(1) : "";
        this._flowSpeed[p.dataset.k] = on && !reduce ? Math.sign(l.w) * (28 + 150 * a) : 0;   // viewBox units per second
      } else p.style.strokeWidth = on ? (10 + 14 * a).toFixed(1) : "";
    }
    for (const el of root.querySelectorAll(".flbl")) {
      const l = lines[el.dataset.k] || { w: null }, on = l.w != null && Math.abs(l.w) >= IDLE;
      el.textContent = on ? fmtW(Math.abs(l.w)) : "";
      el.style.borderColor = on ? l.col : "";
      el.classList.toggle("on", on);
    }
    this._flowSummary(f);
    this._flowStart();
  },

  // Where the house's power is coming from right now, and where any spare is going.
  _flowSummary(f) {
    const el = this.shadowRoot.getElementById("fsum");
    if (!el) return;
    const dis = f.bats.reduce((a, b) => a + (b.w != null && b.w < 0 ? -b.w : 0), 0), chg = f.bats.reduce((a, b) => a + (b.w > 0 ? b.w : 0), 0);
    const imp = f.grid > 0 ? f.grid : 0, exp = f.grid < 0 ? -f.grid : 0, pv = f.pv > 0 ? f.pv : 0;
    const parts = [[imp, "Grid", U.INK.err], [dis, "Batteries", U.INK.discharge], [pv, "Solar", "var(--hp-solar)"]].filter((x) => x[0] >= IDLE);
    const tot = parts.reduce((a, x) => a + x[0], 0);
    const bar = tot ? `<div class="fbar">${parts.map(([v, n, c]) => `<i style="width:${((v / tot) * 100).toFixed(1)}%;background:${c}" title="${n} ${fmtW(v)}"></i>`).join("")}</div>` : "";
    const sw = (c) => `<i class="sw" style="background:${c}"></i>`;
    const from = parts.length ? `Power in: ${parts.map(([v, n, c]) => `<span class="fsi">${sw(c)}${n} <b>${fmtW(v)}</b> (${Math.round((v / tot) * 100)}%)</span>`).join(" ")}` : "Nothing flowing in right now";
    const to = [chg >= IDLE ? `<span class="fsi">${sw(U.INK.charge)}charging batteries <b>${fmtW(chg)}</b></span>` : "", exp >= IDLE ? `<span class="fsi">${sw(U.INK.charge)}exporting <b>${fmtW(exp)}</b></span>` : ""].filter(Boolean);
    el.innerHTML = `${bar}<div class="legend"><span>${from}</span>${f.house != null ? `<span class="fsi">House using <b>${fmtW(f.house)}</b></span>` : ""}${to.length ? `<span>Going to: ${to.join(" ")}</span>` : ""}</div>`;
  },

  _flowReduced() {
    try { return !!(globalThis.matchMedia && globalThis.matchMedia("(prefers-reduced-motion: reduce)").matches); } catch (e) { return false; }
  },

  // Moves the dots: each line's dash offset advances by its speed, so a change of speed carries on smoothly from where it is.
  _flowStart() {
    if (this._flowRaf || typeof requestAnimationFrame !== "function") return;
    this._flowOff = this._flowOff || {};
    let last = performance.now();
    const step = (t) => {
      const dots = this.isConnected && this.tab === "flow" ? this.shadowRoot.querySelectorAll(".fdots") : [];
      if (!dots.length) { this._flowRaf = null; return; }       // left the tab or the page: stop until shown again
      const dt = Math.min(0.1, (t - last) / 1000); last = t;
      for (const p of dots) {
        const k = p.dataset.k, v = (this._flowSpeed || {})[k] || 0;
        if (!v) continue;
        this._flowOff[k] = (((this._flowOff[k] || 0) - v * dt) % DASH + DASH) % DASH;
        p.style.strokeDashoffset = this._flowOff[k].toFixed(2);
      }
      this._flowRaf = requestAnimationFrame(step);
    };
    this._flowRaf = requestAnimationFrame(step);
  },

  };
};

// A device id as an element id part (ids may hold characters an id selector would choke on).
function slugId(s) { return String(s).replace(/[^A-Za-z0-9_-]/g, (c) => "_" + c.charCodeAt(0).toString(16)); }
