/* HomePower panel: part of homepower-panel.js (loaded by it; methods are added to the panel class). */
export default (U) => {
  const { MODES, SERIES, DAYS, esc, fmtW, fmtKwh, slug, CSS } = U;
  return {
  _path(pts, W, H, color, area) {
    const max = Math.max(500, ...pts.map((x) => Math.abs(x.p)));
    const t0 = pts[0].t, t1 = pts[pts.length - 1].t || t0 + 1;
    const X = (t) => ((t - t0) / Math.max(1, t1 - t0)) * W;
    const Y = (p) => H / 2 - (p / max) * (H / 2 - 2);
    const line = pts.map((x, i) => `${i ? "L" : "M"}${X(x.t).toFixed(1)},${Y(x.p).toFixed(1)}`).join("");
    return `<line x1="0" x2="${W}" y1="${H / 2}" y2="${H / 2}" stroke="var(--divider-color)" stroke-dasharray="3 3"/>
      ${area ? `<path d="${line}L${X(t1)},${H / 2}L0,${H / 2}Z" fill="${color}" opacity=".15"/>` : ""}
      <path d="${line}" fill="none" stroke="${color}" stroke-width="2" vector-effect="non-scaling-stroke"/>`;
  },

  // ---- chart helpers: nice axis ticks, a plot with labelled Y and X axes, adjustable live duration ----
  _niceTicks(lo, hi, n = 4) {
    const span = Math.max(hi - lo, 1e-9), raw = span / n, mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const step = ([1, 2, 2.5, 5, 10].map((m) => m * mag).find((x) => x >= raw)) || 10 * mag;
    // rounded to the step's own decimals: 3 x 0.2 is 0.6000000000000001 in floating point, which would be printed as such
    const dec = Math.max(0, Math.ceil(-Math.log10(step)) + 1), fix = (v) => +v.toFixed(dec);
    const a = fix(Math.floor(lo / step + 1e-9) * step), b = fix(Math.ceil(hi / step - 1e-9) * step), ticks = [];
    for (let v = a; v <= b + step / 2; v += step) ticks.push(fix(Math.round(v / step) * step));
    return { lo: a, hi: b, ticks };
  },

  _downsample(pts, maxN = 300) {
    if (pts.length <= maxN) return pts;
    const k = Math.ceil(pts.length / maxN), out = [];
    for (let i = 0; i < pts.length; i += k) {
      const c = pts.slice(i, i + k);
      out.push({ t: c.reduce((a, x) => a + x.t, 0) / c.length, v: c.reduce((a, x) => a + x.v, 0) / c.length });
    }
    return out;
  },

  // Date formatting options in Home Assistant's time zone (the home's clock), not the browser's; the browser's if the zone is unknown.
  _tzOpt(o = {}) {
    const tz = this._hass && this._hass.config && this._hass.config.time_zone;
    if (!tz) return o;
    if (this._tzOk === undefined || this._tzOk[0] !== tz) {
      let ok = true; try { new Intl.DateTimeFormat("en-GB", { timeZone: tz }); } catch (e) { ok = false; }
      this._tzOk = [tz, ok];
    }
    return this._tzOk[1] ? { ...o, timeZone: tz } : o;
  },

  _fmtT(t, span) {
    const d = new Date(t);
    if (span <= 36 * 3600e3) return d.toLocaleTimeString([], this._tzOpt({ hour: "2-digit", minute: "2-digit" }));
    if (span <= 3 * 86400e3) return d.toLocaleString([], this._tzOpt({ weekday: "short", hour: "2-digit", minute: "2-digit" }));
    return d.toLocaleDateString([], this._tzOpt({ month: "short", day: "numeric" }));
  },

  // series: [{pts:[{t,v}], color, width}]; o: {lo, hi, ticks, tmin, tmax, fmtY, height, ariaLabel}
  _plot(series, o) {
    const W = 800, H = 200, { lo, hi, ticks, tmin, tmax } = o, height = o.height || 210;
    const X = (t) => ((t - tmin) / Math.max(1, tmax - tmin)) * W;
    const Y = (v) => H - ((v - lo) / Math.max(1e-9, hi - lo)) * H;
    const xt = [0, 0.25, 0.5, 0.75, 1].map((f) => tmin + f * (tmax - tmin));
    const grid = ticks.map((v) => `<line x1="0" x2="${W}" y1="${Y(v).toFixed(1)}" y2="${Y(v).toFixed(1)}" stroke="var(--divider-color)" stroke-opacity="${v === 0 && lo < 0 ? 1 : 0.5}" ${v === 0 && lo < 0 ? "" : 'stroke-dasharray="3 4"'} vector-effect="non-scaling-stroke"/>`).join("")
      + xt.slice(1, -1).map((t) => `<line x1="${X(t).toFixed(1)}" x2="${X(t).toFixed(1)}" y1="0" y2="${H}" stroke="var(--divider-color)" stroke-opacity=".3" vector-effect="non-scaling-stroke"/>`).join("");
    const lines = series.map((s) => s.pts.length < 2 ? "" : `<path d="${s.pts.map((p, i) => `${i ? "L" : "M"}${X(p.t).toFixed(1)},${Math.min(H, Math.max(0, Y(p.v))).toFixed(1)}`).join("")}" fill="none" stroke="${s.color}" stroke-width="${s.width || 2.2}"${s.dash ? ' stroke-dasharray="7 5"' : ""} stroke-linejoin="round" vector-effect="non-scaling-stroke"/>`).join("");
    const ylab = ticks.map((v) => `<span style="top:${((1 - (v - lo) / Math.max(1e-9, hi - lo)) * 100).toFixed(2)}%">${o.fmtY(v)}</span>`).join("");
    const xlab = xt.map((t, i) => `<span style="left:${i * 25}%">${this._fmtT(t, tmax - tmin)}</span>`).join("");
    return `<div class="plot"><div class="plot-y" style="height:${height}px">${ylab}</div>
      <div class="plot-main"><svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" style="height:${height}px" role="img" aria-label="${esc(o.ariaLabel || "Chart")}">${grid}${lines}</svg>
      <div class="plot-x">${xlab}</div></div></div>`;
  },

  // ---- legends: click an item to hide that series, again to show it (remembered in this browser) ----------------
  _hiddenSet() {
    if (!this._hiddenV) {
      let saved = [];
      try { saved = JSON.parse(localStorage.getItem("homepower.hidden") || "[]"); } catch (e) { /* storage unavailable or damaged */ }
      this._hiddenV = new Set(Array.isArray(saved) ? saved : []);
    }
    return this._hiddenV;
  },

  _isHidden(chart, key) { return this._hiddenSet().has(JSON.stringify([chart, key])); },

  _toggleSeries(chart, key) {
    const set = this._hiddenSet(), k = JSON.stringify([chart, key]);
    if (!set.delete(k)) set.add(k);
    try { localStorage.setItem("homepower.hidden", JSON.stringify([...set])); } catch (e) { /* optional */ }
    this._render();
    const again = [...this.shadowRoot.querySelectorAll('[data-act="legend"]')].find((b) => b.dataset.chart === chart && b.dataset.key === key);
    if (again) again.focus();       // the redraw replaced the button: a keyboard user stays on it
  },

  // items: [{key, name, color, mark?}] (mark: the swatch's own HTML, when a plain square will not do)
  _legend(chart, items) {
    return items.map((s) => {
      const off = this._isHidden(chart, s.key);
      return `<button type="button" class="lg${off ? " off" : ""}" data-act="legend" data-chart="${esc(chart)}" data-key="${esc(s.key)}" aria-pressed="${!off}" title="${off ? "Show" : "Hide"}">${s.mark || `<i class="sw" style="background:${s.color}"></i>`}${esc(s.name)}</button>`;
    }).join("");
  },

  _flowMin() {
    let v = this._flowMinV;
    if (!v) { try { v = parseInt(localStorage.getItem("homepower.flowMin"), 10); } catch (e) { /* storage unavailable */ } }
    return (this._flowMinV = [15, 30, 60, 180, 360, 720, 1440].includes(v) ? v : 30);
  },

  _durLabel(m) { return m < 60 ? `${m} minutes` : m === 60 ? "hour" : `${m / 60} hours`; },

  _durButtons() {
    const cur = this._flowMin();
    return [[15, "15m"], [30, "30m"], [60, "1h"], [180, "3h"], [360, "6h"], [720, "12h"], [1440, "24h"]]
      .map(([m, l]) => `<button class="btn sm ${m === cur ? "pri" : ""}" data-act="flowdur" data-m="${m}">${l}</button>`).join("");
  },

  // History older than this page has been open comes from Home Assistant's own statistics (5-minute averages).
  async _backfillFlow() {
    const mins = this._flowMin(), ids = (this.data.entity_ids || {}).devices || {};
    const list = Object.values(ids).map((e) => e && e.power).filter(Boolean);
    const key = `${mins}:${list.join(",")}`;
    if (!list.length || mins <= 30 || (this._flowBack && this._flowBack.key === key && Date.now() - this._flowBack.at < 120000)) return;
    this._flowBack = { key, at: Date.now(), data: (this._flowBack && this._flowBack.key === key) ? this._flowBack.data : {} };
    try {
      const end = new Date(), start = new Date(end - mins * 60e3);
      const res = await this._hass.callWS({ type: "recorder/statistics_during_period", start_time: start.toISOString(), end_time: end.toISOString(),
        statistic_ids: list, period: "5minute", types: ["mean"] });
      this._flowBack.data = res || {};
      if (this.tab === "overview" && !this._modal) this._render();
    } catch (e) { /* recorder statistics are optional: the live samples still draw */ }
  },

  _flowChart() {
    const ds = this.data.devices, mins = this._flowMin(), now = Date.now(), tmin = now - mins * 60e3;
    this._backfillFlow();
    const back = (this._flowBack && this._flowBack.data) || {}, ids = (this.data.entity_ids || {}).devices || {};
    const series = ds.map((d, i) => {
      const live = (this._hist[d.device_id] || []).filter((x) => x.p != null && x.t >= tmin).map((x) => ({ t: x.t, v: x.p }));
      const first = live.length ? live[0].t : now;
      const eid = (ids[d.device_id] || {}).power;
      const old = mins > 30 && eid ? (back[eid] || []).filter((r) => r.mean != null).map((r) => ({ t: typeof r.start === "number" ? r.start : Date.parse(r.start), v: r.mean })).filter((x) => x.t >= tmin && x.t < first) : [];
      return { key: d.device_id, name: d.name, pts: this._downsample([...old, ...live]), color: this._color(i), width: 2.4 };
    }).filter((s) => s.pts.length > 1);
    if (!series.length) return `<div class="sub" style="color:var(--secondary-text-color)">Collecting live data… the chart fills in as readings arrive.</div>`;
    const all = series.flatMap((s) => s.pts), shown = series.filter((s) => !this._isHidden("flow", s.key));
    const m = Math.max(500, ...shown.flatMap((s) => s.pts).map((x) => Math.abs(x.v)));      // the scale fits what is shown
    const nt = this._niceTicks(-m, m, 4);
    const t0 = Math.min(tmin, ...all.map((x) => x.t));
    const covered = Math.round((now - Math.min(...all.map((x) => x.t))) / 60e3);
    return `${this._plot(shown, { lo: nt.lo, hi: nt.hi, ticks: nt.ticks, tmin: t0, tmax: now, fmtY: (v) => (v === 0 ? "0" : fmtW(v)), ariaLabel: "Battery power over time" })}
      <div class="legend">${this._legend("flow", series)}
      <span>Above 0 = charging, below = discharging${covered < mins - 2 ? ` · showing the last ${covered} min so far (the page records while open${mins > 30 ? "; older data comes from Home Assistant history" : ""})` : ""}</span></div>`;
  },

  // "Now" on Home Assistant's clock (schedules run in its time zone), as a Date whose local fields show that time: a browser in another
  // time zone would otherwise draw today's bars on the wrong day and the now-line at the wrong hour.
  _haNow() {
    const tz = this._hass && this._hass.config && this._hass.config.time_zone, d = new Date();
    if (!tz) return d;
    try {
      const p = Object.fromEntries(new Intl.DateTimeFormat("en-GB", { timeZone: tz, year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" })
        .formatToParts(d).map((x) => [x.type, x.value]));
      return new Date(+p.year, +p.month - 1, +p.day, +p.hour, +p.minute, +p.second);
    } catch (e) { return d; }          // an unknown zone name: fall back to the browser's clock
  },

  _segmentsToday(s) {
    const now = this._haNow();
    const dayOk = (dt) => {
      if (s.on_date) return s.on_date === `${dt.getFullYear()}-${String(dt.getMonth() + 1).padStart(2, "0")}-${String(dt.getDate()).padStart(2, "0")}`;
      const wd = (dt.getDay() + 6) % 7;
      if (!s.days.includes(wd)) return false;
      if (s.season_start && s.season_end) {
        const [sm, sd] = s.season_start.split("-").map(Number), [em, ed] = s.season_end.split("-").map(Number);
        const cur = (dt.getMonth() + 1) * 100 + dt.getDate(), a = sm * 100 + sd, b = em * 100 + ed;
        return a <= b ? cur >= a && cur <= b : cur >= a || cur <= b;
      }
      return true;
    };
    const hm = (t) => { const [h, m] = t.split(":").map(Number); return h + m / 60; };
    if (s.all_day) return dayOk(now) ? [[0, 24]] : [];
    const a = hm(s.start), b = hm(s.end);
    const yest = new Date(now.getTime() - 864e5);
    const out = [];
    if (b > a) { if (dayOk(now)) out.push([a, b]); }
    else { if (dayOk(now)) out.push([a, 24]); if (dayOk(yest)) out.push([0, b]); }
    return out;
  },

  _timeline() {
    const ds = this.data.devices;
    if (!ds.length) return "";
    const now = this._haNow(), nowH = now.getHours() + now.getMinutes() / 60;
    const rows = ds.map((d) => {
      const zi = this.data.settings.zero_import_device === d.device_id;
      const mine = this.data.schedules.filter((s) => s.enabled && (!s.device_ids.length || s.device_ids.includes(d.device_id))).sort((x, y) => x.priority - y.priority);
      const segs = mine.flatMap((s) => this._segmentsToday(s).map(([a, b]) => `<div class="seg" title="${esc(s.id)}: ${s.start}–${s.end}" style="left:${(a / 24) * 100}%;width:${((b - a) / 24) * 100}%;background:${MODES[s.mode].color};color:${U.onFill(MODES[s.mode].color)}">${b - a > 2 ? esc(s.id) : ""}</div>`));
      return `<div class="row"><div class="nm" title="${esc(d.name)}">${esc(d.name)}</div><div class="bar ${zi ? "zi" : ""}">${segs.join("")}<div class="now" style="left:${(nowH / 24) * 100}%"></div></div></div>`;
    }).join("");
    const ticks = `<div class="axis"><div></div><div><span>00:00</span><span>06:00</span><span>12:00</span><span>18:00</span><span>24:00</span></div></div>`;
    const legend = `<div class="legend">${Object.entries(MODES).map(([k, m]) => `<span><i class="sw" style="background:${m.color}"></i>${m.label}</span>`).join("")}${this.data.settings.zero_import_device ? `<span><i class="sw" style="background:repeating-linear-gradient(45deg,#cbd5e1,#cbd5e1 3px,#93b4f5 3px,#93b4f5 6px)"></i>Zero-import when no schedule applies</span>` : ""}<span><i class="sw" style="background:var(--primary-text-color);width:3px"></i>Now</span></div>`;
    return `<div class="tl">${rows}${ticks}</div>${legend}`;
  },

  };
};
