/* HomePower panel: part of homepower-panel.js (loaded by it; methods are added to the panel class).
   Click a figure on the Overview (the Home battery card, the row of tiles) to see how it has changed: a popup with a chart
   drawn from Home Assistant's recorder, using HomePower's own sensors. */
export default (U) => {
  const { SERIES, esc, fmtW } = U;
  const RANGES = [[6, "6h"], [24, "24h"], [72, "3 days"], [168, "7 days"]];
  return {
  // What each figure's popup shows. Returns null when there is nothing recorded to draw (the sensor is missing).
  _histSpec(key) {
    const ids = this.data.entity_ids || {}, devs = ids.devices || {}, ds = this.data.devices || [];
    const cur = esc(this.live.currency || "£");
    const perDevice = (k) => ds.map((d, i) => ({ eid: (devs[d.device_id] || {})[k], name: d.name, color: this._color(i), width: 1.6 }))
      .filter((s) => s.eid);
    const one = (eid, name) => (eid ? [{ eid, name, color: "var(--primary-text-color)", width: 2.6 }] : []);
    const many = ds.length > 1;
    const specs = {
      flow: { title: "Battery flow", icon: "mdi:lightning-bolt", unit: "power", signed: true, fmt: (v) => (v === 0 ? "0" : fmtW(v)),
        lead: "Power into (+) or out of (−) the batteries.", series: [...one(ids.battery_power, "All batteries"), ...(many ? perDevice("power") : [])] },
      soc: { title: "Charge level", icon: "mdi:home-battery", fixed: [0, 100], fmt: (v) => `${Math.round(v)}%`,
        lead: "How full the batteries are.", series: [...one(ids.home_battery, "All batteries"), ...perDevice("soc")] },
      stored: { title: "Stored energy", icon: "mdi:battery-high", unit: "energy", fmt: (v) => `${(+v).toFixed(1)} kWh`, zero: true,
        lead: "Energy held in the batteries.", series: one(ids.stored_energy, "Stored energy") },
      grid: { title: "Grid", icon: "mdi:transmission-tower", unit: "power", signed: true, fmt: (v) => (v === 0 ? "0" : fmtW(v)),
        lead: "Power from the grid (+ importing, − exporting).", series: one(ids.grid_power, "Grid") },
      savings: { title: "Saved today", icon: "mdi:piggy-bank-outline", raw: true, signed: true, fmt: (v) => `${cur}${(+v).toFixed(2)}`,
        lead: "The day's estimated saving as it built up (it starts again from 0 each midnight).", series: one(ids.savings_today, "Saved today") },
      solar: { title: "Solar power", icon: "mdi:solar-power", unit: "power", zero: true, fmt: (v) => (v === 0 ? "0" : fmtW(v)),
        lead: "The solar power the batteries really measured (the forecast itself is not recorded over time).",
        series: perDevice("solar") },
      price: { title: "Price", icon: "mdi:tag-outline", raw: true, step: true, zero: true, fmt: (v) => `${cur}${(+v).toFixed(3)}`,
        lead: "Your import price per kWh, from your rates.", series: one(ids.price_now, "Price") },
    };
    const s = specs[key];
    return s && s.series.length ? s : null;
  },

  // The attributes that make a figure open its history (none when there is no sensor to read it from).
  _histAttr(key) {
    const s = this._histSpec(key);
    return s ? ` data-act="hist" data-k="${key}" role="button" tabindex="0" title="Show ${esc(s.title.toLowerCase())} over time"` : "";
  },

  _histHours() {
    let v = this._histH;
    if (!v) { try { v = parseInt(localStorage.getItem("homepower.histH"), 10); } catch (e) { /* storage unavailable */ } }
    return (this._histH = RANGES.some(([h]) => h === v) ? v : 24);
  },

  _histModal(key) {
    const s = this._histSpec(key);
    if (!s) return;
    this._histKey = key;
    this._openModal(`<div class="overlay" data-close-bg><div class="modal hist" role="dialog" aria-label="${esc(s.title)} history">
      <h2><ha-icon icon="${s.icon}"></ha-icon> ${esc(s.title)}</h2><p class="lead">${esc(s.lead)}</p>
      <div class="durs" id="hist-ranges" style="margin:0 0 12px"></div>
      <div id="hist-body"><div class="sub" style="color:var(--secondary-text-color)">Loading history…</div></div>
      <div class="foot"><span></span><div class="r"><button class="btn" data-act="close">Close</button></div></div></div></div>`);
    this._histLoad();
  },

  async _histLoad() {
    const key = this._histKey, s = this._histSpec(key), hours = this._histHours();
    const root = this.shadowRoot, rng = root.getElementById("hist-ranges");
    if (!s || !rng) return;
    rng.innerHTML = RANGES.map(([h, l]) => `<button class="btn sm ${h === hours ? "pri" : ""}" data-act="hist-range" data-h="${h}" aria-pressed="${h === hours}">${l}</button>`).join("");
    const token = (this._histToken = (this._histToken || 0) + 1);
    const end = new Date(), start = new Date(end - hours * 3600e3), list = s.series.map((x) => x.eid);
    let data = {};
    try {
      if (s.raw) {        // no long-term statistics for these (no state class): the recorded states themselves
        data = await this._hass.callWS({ type: "history/history_during_period", start_time: start.toISOString(), end_time: end.toISOString(),
          entity_ids: list, minimal_response: true, no_attributes: true, significant_changes_only: false });
      } else {
        data = await this._hass.callWS({ type: "recorder/statistics_during_period", start_time: start.toISOString(), end_time: end.toISOString(),
          statistic_ids: list, period: hours <= 24 ? "5minute" : "hour", types: ["mean"], units: { power: "W", energy: "kWh" } });
      }
    } catch (e) {
      if (token === this._histToken && this._histKey === key) this._histBody(`<div class="sub" style="color:var(--secondary-text-color)">History is not available: ${esc(e.message || String(e))}</div>`);
      return;
    }
    if (token !== this._histToken || this._histKey !== key || !this._modal) return;     // closed, or another range asked for since
    this._histData = { key, hours, tmin: +start, data: data || {} };
    this._histDraw();
  },

  _histBody(html) { const b = this.shadowRoot.getElementById("hist-body"); if (b) b.innerHTML = html; },

  // One series' points [{t, v}] from the recorder's answer, ending with the sensor's value now.
  _histPts(s, eid, data, tmin) {
    const ts = (x) => (typeof x === "number" ? (x < 1e11 ? x * 1000 : x) : Date.parse(x));
    let pts = s.raw
      ? (data[eid] || []).map((r) => ({ t: ts(r.lu ?? r.last_updated ?? r.last_changed), v: parseFloat(r.s ?? r.state) }))
      : (data[eid] || []).filter((r) => r.mean != null).map((r) => ({ t: ts(r.start), v: r.mean }));
    pts = pts.filter((p) => Number.isFinite(p.v) && Number.isFinite(p.t)).map((p) => ({ t: Math.max(p.t, tmin), v: p.v }));
    const st = this._hass.states && this._hass.states[eid], now = Date.now();
    let v = st ? parseFloat(st.state) : NaN;
    const unit = st ? String((st.attributes || {}).unit_of_measurement || "").trim().toLowerCase() : "";
    if (s.unit === "power" && unit === "kw") v *= 1000;               // asked for in W above: the live value must match
    if (s.unit === "energy" && unit === "wh") v /= 1000;
    if (Number.isFinite(v)) pts.push({ t: now, v });
    else if (pts.length && s.step) pts.push({ t: now, v: pts[pts.length - 1].v });
    if (s.step) pts = pts.flatMap((p, i) => (i ? [{ t: p.t, v: pts[i - 1].v }, p] : [p]));   // a price holds until it changes
    return s.step ? pts : this._downsample(pts, 400);
  },

  _histDraw() {
    const h = this._histData, s = h && this._histSpec(h.key);
    if (!s || h.key !== this._histKey) return;
    const chart = "hist:" + h.key;
    const series = s.series.map((x) => ({ ...x, key: x.eid, pts: this._histPts(s, x.eid, h.data, h.tmin) })).filter((x) => x.pts.length > 1);
    if (!series.length) {
      return this._histBody(`<div class="sub" style="color:var(--secondary-text-color)">Nothing recorded for this period yet. Home Assistant's recorder keeps these figures as they come in${s.raw ? "" : " (as 5-minute averages, so the newest few minutes show only once complete)"}.</div>`);
    }
    const shown = series.filter((x) => !this._isHidden(chart, x.key));
    const vals = (shown.length ? shown : series).flatMap((x) => x.pts.map((p) => p.v));
    let lo = Math.min(...vals), hi = Math.max(...vals);
    if (s.fixed) [lo, hi] = s.fixed;
    else {
      if (s.zero || s.signed) { lo = Math.min(lo, 0); hi = Math.max(hi, 0); }
      if (s.unit === "power") { const m = s.signed ? Math.max(500, Math.abs(lo), Math.abs(hi)) : Math.max(500, hi); hi = m; lo = s.signed ? -m : lo; }
      if (hi - lo < 1e-6) { hi += 1; lo = Math.min(lo, 0); }
    }
    const nt = s.fixed ? { lo, hi, ticks: [0, 25, 50, 75, 100] } : this._niceTicks(lo, hi, 4);
    const main = series[0], mv = main.pts.map((p) => p.v);
    const facts = [`Now <b>${s.fmt(mv[mv.length - 1])}</b>`, `lowest <b>${s.fmt(Math.min(...mv))}</b>`, `highest <b>${s.fmt(Math.max(...mv))}</b>`];
    if (!s.raw) facts.push(`average <b>${s.fmt(mv.reduce((a, v) => a + v, 0) / mv.length)}</b>`);
    const legend = series.length > 1 ? series.map((x) => {
      const off = this._isHidden(chart, x.key);
      return `<button type="button" class="lg${off ? " off" : ""}" data-act="hist-legend" data-key="${esc(x.key)}" aria-pressed="${!off}" title="${off ? "Show" : "Hide"}"><i class="sw" style="background:${x.color}"></i>${esc(x.name)}</button>`;
    }).join("") : "";
    this._histBody(`${this._plot(shown, { lo: nt.lo, hi: nt.hi, ticks: nt.ticks, tmin: h.tmin, tmax: Date.now(), fmtY: s.fmt, height: 230, ariaLabel: `${s.title} over time` })}
      <div class="legend">${legend}<span>${esc(main.name)}: ${facts.join(" · ")}</span></div>`);
  },

  _histToggle(key) {
    const set = this._hiddenSet(), k = JSON.stringify(["hist:" + this._histKey, key]);
    if (!set.delete(k)) set.add(k);
    try { localStorage.setItem("homepower.hidden", JSON.stringify([...set])); } catch (e) { /* optional */ }
    this._histDraw();
    const again = [...this.shadowRoot.querySelectorAll('[data-act="hist-legend"]')].find((b) => b.dataset.key === key);
    if (again) again.focus();
  },

  _histRange(h) {
    this._histH = h;
    try { localStorage.setItem("homepower.histH", String(h)); } catch (e) { /* optional */ }
    this._histBody(`<div class="sub" style="color:var(--secondary-text-color)">Loading history…</div>`);
    this._histLoad();
  },

  };
};
