/* HomePower panel: part of homepower-panel.js (loaded by it; methods are added to the panel class). */
export default (U) => {
  const { MODES, SERIES, DAYS, esc, fmtW, fmtKwh, slug, CSS } = U;
  return {
  // ---- insights tab: savings, energy, long-term history -----------------------------
  async _loadHistory() {
    const ids = this.data.entity_ids || {}, list = [];
    if (ids.battery_power) list.push(ids.battery_power);
    if (ids.grid_power) list.push(ids.grid_power);
    for (const d of this.data.devices) { const e = (ids.devices || {})[d.device_id]; if (e && e.soc) list.push(e.soc); }
    const hours = { "6h": 6, "12h": 12, "24h": 24, "3d": 72, "7d": 168, "30d": 720 }[this._range || "24h"];
    if (!list.length) { this._hist2 = {}; return; }
    const end = new Date(), start = new Date(end - hours * 3600e3);
    this._hist2 = await this._hass.callWS({ type: "recorder/statistics_during_period", start_time: start.toISOString(), end_time: end.toISOString(),
      statistic_ids: list, period: hours > 48 ? "hour" : "5minute", types: ["mean"] });
  },

  _lineChart(series, unit) {
    const all = series.flatMap((s) => s.pts);
    if (all.length < 2) return `<div class="sub" style="color:var(--secondary-text-color)">No history yet. It fills in as Home Assistant records the sensors.</div>`;
    const tmin = Math.min(...all.map((p) => p.t)), tmax = Math.max(...all.map((p) => p.t));
    let nt;
    if (unit === "%") nt = { lo: 0, hi: 100, ticks: [0, 25, 50, 75, 100] };
    else { const m = Math.max(500, ...all.map((p) => Math.abs(p.v))); nt = this._niceTicks(Math.min(0, ...all.map((p) => p.v)) < 0 ? -m : 0, m, 4); }
    const ser = series.map((s) => ({ color: s.color, pts: this._downsample(s.pts) }));
    return `${this._plot(ser, { lo: nt.lo, hi: nt.hi, ticks: nt.ticks, tmin, tmax, fmtY: (v) => (unit === "%" ? `${v}%` : v === 0 ? "0" : fmtW(v)), height: 220, ariaLabel: unit === "%" ? "Charge level history" : "Power history" })}
      <div class="legend">${series.map((s) => `<span><i class="sw" style="background:${s.color}"></i>${esc(s.name)}</span>`).join("")}</div>`;
  },

  _cyclingText(c) {
    if (!c) return "–";
    const cur = esc(this.live.currency || ""), n = Math.abs(c.net_per_kwh).toFixed(2);
    return `${c.worth_it ? `<span class="ok">Yes: about ${cur}${n} per kWh</span>` : `<span class="bad">No: loses about ${cur}${n} per kWh</span>`}<span class="sub" style="color:var(--secondary-text-color)"> (cheapest to dearest price at ${c.efficiency_pct}%${c.measured ? "" : ", estimated"})</span>`;
  },

  _planCard() {
    const pa = this.live.plan_actual || {};
    const blocks = this.data.devices.map((d, i) => {
      const x = pa[d.device_id];
      if (!x || !(x.rows || []).some((r) => r[1] != null || r[2] != null)) return "";
      const t = (r) => Date.parse(r[0]) + 900000, mk = (k) => x.rows.filter((r) => r[k] != null).map((r) => ({ t: t(r), v: r[k] }));
      const series = [{ name: "Planned", color: "#9ca3af", dash: true, pts: mk(1) }, { name: "Actual", color: this._color(i), pts: mk(2) }];
      const vs = series.flatMap((s) => s.pts.map((p) => p.v)).concat([0]), nt = this._niceTicks(Math.min(...vs), Math.max(...vs), 4);
      const tmin = Date.parse(x.rows[0][0]), tmax = Date.parse(x.rows[x.rows.length - 1][0]) + 1800000;
      const verdict = x.match_pct == null ? "Not enough time under HomePower's control yet to compare." : `Followed the plan ${x.match_pct}% over the slots it was given one.`;
      return `<div style="margin:10px 0"><b>${esc(d.name)}</b> <span class="sub" style="font-size:12px;color:var(--secondary-text-color)">${verdict}</span>
        ${this._plot(series, { lo: nt.lo, hi: nt.hi, ticks: nt.ticks, tmin, tmax, fmtY: (v) => (v === 0 ? "0" : fmtW(v)), height: 130, ariaLabel: `${d.name}: planned and actual power, last 24 hours` })}
        <div class="legend">${series.map((s) => `<span><i class="sw" style="background:${s.color}"></i>${s.name}</span>`).join("")}</div></div>`;
    }).join("");
    if (!blocks) return "";
    return `<div class="card" style="margin-top:16px"><h2><ha-icon icon="mdi:chart-timeline-variant"></ha-icon>Plan versus actual (last 24 hours)</h2>${blocks}
      <p class="sub" style="font-size:12px;color:var(--secondary-text-color)">Charging is above the line, discharging below. The dashed line is what HomePower told the battery to do (it stops where the battery was left to its own control); the solid line is what it really did. A big gap means the battery is not following commands, or something else (the app, a schedule on the battery) is overriding them.</p></div>`;
  },

  _ecoflowCard() {
    const rows = this.data.devices.map((d) => {
      const r = ((this.live.devices || {})[d.device_id] || {}).report;
      if (!r) return "";
      const cls = r.verdict === "unreliable" ? "bad" : r.sent >= 3 && r.worked / r.sent >= 0.8 ? "ok" : "";
      return `<tr><td>${esc(d.name)}</td><td>${r.worked} of ${r.sent}</td><td>${r.avg_reaction_s == null ? "–" : r.avg_reaction_s + " s"}</td><td>${r.charge_rate_w ? fmtW(r.charge_rate_w) : "–"}</td><td>${r.follows_plan_pct == null ? "–" : r.follows_plan_pct + "%"}</td><td class="${cls}">${esc(r.advice)}</td></tr>`;
    }).join("");
    if (!rows) return "";
    return `<div class="card" style="margin-top:16px"><h2><ha-icon icon="mdi:clipboard-pulse"></ha-icon>EcoFlow control report</h2>
      <table class="tbl"><tr><th>Battery</th><th>Commands that worked</th><th>Average reaction</th><th>Real charge rate</th><th>Followed the plan</th><th>Verdict</th></tr>${rows}</table>
      <p class="sub" style="font-size:12px;color:var(--secondary-text-color)">Built from HomePower's log of every Hold and Charge sent to batteries that pick their own charge rate. Use it to decide whether to leave control on.</p></div>`;
  },

  _weatherText() {
    const w = this.live.weather || {};
    if (!w.status) return "Switch the first option on, pick a weather entity, and HomePower works out each hour whether tomorrow is colder than lately.";
    const bits = [w.tomorrow_c != null ? `Tomorrow ${w.tomorrow_c} °C, from ${w.source}` : "", w.lately_c != null ? `lately about ${w.lately_c} °C` : "",
      w.k_wh_per_degree != null ? `your house uses about ${(w.k_wh_per_degree / 1000).toFixed(2)} kWh more per degree of cold (from ${w.days_learned} days)` : "", w.extra_wh ? `expected extra use ${(w.extra_wh / 1000).toFixed(1)} kWh` : ""].filter(Boolean);
    return `${bits.join(" · ")}${bits.length ? ". " : ""}Status: ${w.status}.`;
  },

  _healthCard() {
    const h = (this.live.health || {}), rows = this.data.devices.map((d) => {
      const x = h[d.device_id] || { efficiency: [], warning: null }, e = x.efficiency || [];
      return `<tr><td>${esc(d.name)}</td><td>${e.length ? e.map(([, v]) => v + "%").join(" → ") : "not enough history yet"}</td><td>${x.warning ? `<span style="color:#dc2626">${esc(x.warning)}</span>` : e.length >= 3 ? "Steady" : "–"}</td><td>${this._cyclingText(x.cycling)}</td></tr>`;
    }).join("");
    if (!rows) return "";
    return `<div class="card" style="margin-top:16px"><h2><ha-icon icon="mdi:heart-pulse"></ha-icon>Battery health (weekly round-trip efficiency)</h2>
      <table class="tbl"><tr><th>Battery</th><th>Last weeks (oldest → newest)</th><th>Trend</th><th>Worth cycling in the next 24 h?</th></tr>${rows}</table>
      <p class="sub" style="font-size:12px;color:var(--secondary-text-color)">Energy out divided by energy in, per full week. HomePower alerts you if a battery drops 10 points below its usual.</p></div>`;
  },

  _csvLink(file, rows) {
    const csv = rows.map((r) => r.map((c) => `"${String(c ?? "").replace(/"/g, '""')}"`).join(",")).join("\n");
    const urls = (this._csvUrls ||= new Map());          // one blob per distinct content: not a new one (never freed) on every refresh
    if (!urls.has(csv)) {
      if (urls.size >= 12) { const [k, u] = urls.entries().next().value; URL.revokeObjectURL(u); urls.delete(k); }
      urls.set(csv, URL.createObjectURL(new Blob([csv], { type: "text/csv" })));
    }
    return `<a class="btn sm" download="${esc(file)}" href="${urls.get(csv)}">Download CSV</a>`;
  },

  _tariffCsv(t) {
    const rows = [["Tariff", `Import (last ${t.days} days)`, "Export earned", "Standing", "Total"], ...t.tariffs.map((x) => [x.name, x.import_cost, x.export_earned, x.standing, x.total])];
    const m = t.monthly;
    if (m && m.months && m.months.length) {
      const names = Object.keys(m.totals);
      rows.push([], ["Month", ...names], ...m.months.map((mo, i) => [mo, ...names.map((n) => m.totals[n][i])]));
    }
    return `<div style="margin:6px 0">${this._csvLink("tariff-comparison.csv", rows)}</div>`;
  },

  _whatifCard() {
    const w = this._whatif || {}, cur = esc(this.live.currency || "£"), v = (k, d) => (w.in ? w.in[k] : d);
    const box = (id, label, val) => `<div class="f"><label>${label}</label><input type="number" id="wi-${id}" min="0" max="100" value="${val}"></div>`;
    let res = "";
    if (w.error) res = `<div class="err">${esc(w.error)}</div>`;
    else if (w.result) {
      const r = w.result, d = r.alt.cost - r.base.cost, row = (l, x) => `<tr><td>${l}</td><td>${cur}${x.cost.toFixed(2)}</td><td>${x.import_kwh} kWh</td><td>${x.export_kwh} kWh</td></tr>`;
      res = `<table class="tbl"><tr><th></th><th>Cost over ${r.days} days</th><th>Imported</th><th>Exported</th></tr>
        ${row(`Now: reserve ${w.in.br}%, cheap top-up ${w.in.bc}%`, r.base)}${row(`What if: reserve ${w.in.ar}%, cheap top-up ${w.in.ac}%`, r.alt)}</table>
        <p style="margin:8px 0"><b>${d < -0.004 ? `The what-if would have saved ${cur}${(-d).toFixed(2)}` : d > 0.004 ? `The what-if would have cost ${cur}${d.toFixed(2)} more` : "No difference"}</b> over those ${r.days} days, on ${esc(r.tariff)}, for ${r.capacity_kwh} kWh of battery at up to ${r.max_kw} kW.</p>`;
    }
    return `<div class="card" style="margin-top:16px"><h2><ha-icon icon="mdi:flask-outline"></ha-icon>What if I changed the battery settings?</h2>
      <p class="sub" style="font-size:12px;color:var(--secondary-text-color);margin-top:0">Replays your recorded house load and solar through a simple battery model on the first tariff in Settings > Compare tariffs. The reserve is the lowest the battery goes; the cheap top-up is how full it is charged from the grid in cheap-rate hours (0 = not at all).</p>
      <div class="two">${box("br", "Now: reserve (%)", v("br", 10))}${box("bc", "Now: cheap top-up to (%)", v("bc", 0))}</div>
      <div class="two">${box("ar", "What if: reserve (%)", v("ar", 10))}${box("ac", "What if: cheap top-up to (%)", v("ac", 80))}</div>
      <button class="btn pri" data-act="whatif-run">Compare</button>${res ? `<div style="margin-top:10px">${res}</div>` : ""}
      <p class="sub" style="font-size:12px;color:var(--secondary-text-color)">An estimate: 95% one-way efficiency, all your batteries as one, and none of HomePower's other rules (limits, forecast, goals). It needs a few days of recorded load and solar, which HomePower keeps from this version on.</p></div>`;
  },

  _trendCard() {
    const tr = this.live.trend || {};
    const rows = this.data.devices.filter((d) => ((tr[d.device_id] || {}).days || []).some((r) => r[1] != null || r[2] != null)).map((d) => {
      const x = tr[d.device_id], f = (i, dp) => x.days.slice(-14).map((r) => (r[i] == null ? "–" : r[i].toFixed(dp))).join(" ");
      return `<tr><td>${esc(d.name)}</td><td>${f(1, 0)}</td><td>${f(2, 1)}</td><td>${x.warning ? `<span style="color:#dc2626">${esc(x.warning)}</span>` : "Steady"}</td></tr>`;
    }).join("");
    if (!rows) return "";
    const series = this.data.devices.map((d, i) => ({ name: d.name, color: this._color(i), pts: (((tr[d.device_id] || {}).days) || []).filter((r) => r[1] != null).map((r) => ({ t: Date.parse(r[0]), v: r[1] })) })).filter((s) => s.pts.length >= 2);
    let chart = "";
    if (series.length) {
      const all = series.flatMap((s) => s.pts), vs = all.map((p) => p.v), nt = this._niceTicks(Math.floor(Math.min(...vs) - 2), Math.ceil(Math.max(...vs) + 2), 4);
      chart = this._plot(series, { lo: nt.lo, hi: nt.hi, ticks: nt.ticks, tmin: Math.min(...all.map((p) => p.t)), tmax: Math.max(...all.map((p) => p.t)), fmtY: (v) => `${Math.round(v)}°`, height: 150, ariaLabel: "Hottest temperature each day" })
        + `<div class="legend">${series.map((s) => `<span><i class="sw" style="background:${s.color}"></i>${esc(s.name)}</span>`).join("")}</div>`;
    }
    return `<div class="card" style="margin-top:16px"><h2><ha-icon icon="mdi:thermometer-lines"></ha-icon>Battery temperature and pack balance</h2>${chart}
      <table class="tbl"><tr><th>Battery</th><th>Hottest each day (°C)</th><th>Pack voltage spread (%)</th><th>Status</th></tr>${rows}</table>
      <p class="sub" style="font-size:12px;color:var(--secondary-text-color)">Last 14 days, oldest to newest. The spread is how far apart the highest and lowest battery pack voltages are, as a percentage of their average: HomePower alerts you if it doubles against its usual, or the X1's internal temperature passes 65 °C. Read from Anker X1 batteries.</p></div>`;
  },

  _gridEventsCard() {
    const ev = this.live.grid_events || [];
    if (!ev.length) return "";
    const dur = (e) => { const m = Math.max(0, Math.round(((e.end ? Date.parse(e.end) : Date.now()) - Date.parse(e.start)) / 60000)); return m < 60 ? `${m} min` : `${Math.floor(m / 60)} h ${m % 60} min`; };
    const pc = (v) => (v == null ? "–" : `${Math.round(v)}%`);
    const rows = ev.map((e) => `<tr><td>${esc(new Date(e.start).toLocaleString([], this._tzOpt()))}</td><td>${esc(e.name)}</td><td>${e.end ? dur(e) : `<b>still down: ${dur(e)}</b>`}</td><td>${pc(e.soc_start)} → ${e.end ? pc(e.soc_end) : "…"}</td></tr>`).join("");
    return `<div class="card" style="margin-top:16px"><h2><ha-icon icon="mdi:transmission-tower-off"></ha-icon>Grid outages</h2>
      <table class="tbl"><tr><th>Started</th><th>Battery</th><th>Lasted</th><th>Charge</th></tr>${rows}</table>
      <div style="margin:6px 0">${this._csvLink("grid-outages.csv", [["Started", "Ended", "Battery", "Lasted", "Charge at start (%)", "Charge at end (%)"], ...ev.map((e) => [e.start, e.end || "", e.name, dur(e), e.soc_start, e.soc_end])])}</div>
      <p class="sub" style="font-size:12px;color:var(--secondary-text-color)">Each time a battery reported the grid down and ran the house itself (Anker X1). The charge column shows how much the battery had at the start and the end.</p></div>`;
  },

  _monthChart(m, cur) {
    const names = Object.keys((m || {}).totals || {}), months = (m || {}).months || [];
    if (months.length < 2 || !names.length) return "";
    const vals = names.flatMap((n) => m.totals[n]), hi = Math.max(1, ...vals), lo = Math.min(0, ...vals);
    const W = 640, H = 210, L = 52, B = 26, T = 8, pw = W - L - 8, ph = H - B - T, gw = pw / months.length, bw = Math.min(26, (gw - 8) / names.length);
    const nt = this._niceTicks(lo, hi, 4);                       // the scale runs to the rounded ticks, so the top line and label stay inside the chart
    const y = (v) => T + ph * (1 - (v - nt.lo) / Math.max(1e-9, nt.hi - nt.lo));
    let g = (nt.ticks || []).map((tv) => `<line x1="${L}" x2="${W - 8}" y1="${y(tv).toFixed(1)}" y2="${y(tv).toFixed(1)}" stroke="currentColor" stroke-opacity=".15"/><text x="${L - 6}" y="${(y(tv) + 4).toFixed(1)}" text-anchor="end" font-size="11" fill="currentColor">${cur}${Number.isInteger(tv) ? tv : tv.toFixed(2)}</text>`).join("");
    months.forEach((mo, i) => {
      names.forEach((n, j) => {
        const v = m.totals[n][i], x = L + i * gw + (gw - bw * names.length) / 2 + j * bw;
        g += `<rect x="${x.toFixed(1)}" y="${Math.min(y(v), y(0)).toFixed(1)}" width="${(bw - 2).toFixed(1)}" height="${Math.max(1, Math.abs(y(v) - y(0))).toFixed(1)}" fill="${SERIES[j % SERIES.length]}"><title>${esc(n)}, ${mo}: ${cur}${v.toFixed(2)}</title></rect>`;
      });
      g += `<text x="${(L + i * gw + gw / 2).toFixed(1)}" y="${H - 8}" text-anchor="middle" font-size="11" fill="currentColor">${mo.slice(2)}${i === months.length - 1 ? "*" : ""}</text>`;
    });
    const f = m.cheaper_than_current;
    return `<h3 style="margin:14px 0 4px;font-size:14px">Month by month</h3>
      ${f ? `<div class="banner warn">${esc(f.tariff)} has been cheaper than ${esc(f.current)} for the last ${f.months} months running (about ${cur}${f.saved.toFixed(2)} less in total).</div>` : ""}
      <svg viewBox="0 0 ${W} ${H}" style="width:100%;height:auto;color:var(--primary-text-color)" role="img" aria-label="Cost of each tariff by month">${g}</svg>
      <div class="legend">${names.map((n, j) => `<span><i class="sw" style="background:${SERIES[j % SERIES.length]}"></i>${esc(n)}</span>`).join("")}</div>
      <p class="sub" style="font-size:12px;color:var(--secondary-text-color)">* the month is still in progress. The first tariff you list is taken to be the one you are on.</p>`;
  },

  _tariffCard(cur) {
    const t = this._tariffs;
    if (!t || (!t.error && !Array.isArray(t.tariffs))) return "";      // nothing (or an unexpected answer): no card, not a broken tab
    if (t.error) return `<div class="card" style="margin-top:16px"><h2><ha-icon icon="mdi:scale-balance"></ha-icon>Tariff comparison</h2><div class="err">${esc(t.error)}</div></div>`;
    if (!t.tariffs.length) return `<div class="card" style="margin-top:16px"><h2><ha-icon icon="mdi:scale-balance"></ha-icon>Tariff comparison</h2><p class="sub" style="color:var(--secondary-text-color)">List other tariffs in Settings > Compare tariffs and this shows what your recorded usage would have cost on each.</p></div>`;
    const best = t.tariffs[0].total;
    const rows = t.tariffs.map((x, i) => `<tr><td>${esc(x.name)}${i === 0 ? ' <span class="chip">cheapest</span>' : ""}</td><td>${cur}${x.import_cost.toFixed(2)}</td><td>${x.export_earned ? "-" + cur + x.export_earned.toFixed(2) : "–"}</td><td>${x.standing ? cur + x.standing.toFixed(2) : "–"}</td><td><b>${cur}${x.total.toFixed(2)}</b></td><td>${i === 0 ? "" : "+" + cur + (x.total - best).toFixed(2)}</td></tr>`).join("");
    return `<div class="card" style="margin-top:16px"><h2><ha-icon icon="mdi:scale-balance"></ha-icon>Tariff comparison (last ${t.days} day${t.days === 1 ? "" : "s"})</h2>
      <table class="tbl"><tr><th>Tariff</th><th>Import</th><th>Export</th><th>Standing</th><th>Total</th><th>vs cheapest</th></tr>${rows}</table>
      ${this._monthChart(t.monthly, cur)}${this._tariffCsv(t)}
      <p class="sub" style="font-size:12px;color:var(--secondary-text-color)">Your recorded usage (${t.import_kwh} kWh imported, ${t.export_kwh} kWh exported) priced on each tariff. It is what your battery and house did, so a tariff that suits a different battery schedule could do better than shown.</p></div>`;
  },

  // Bars (what was measured) with the forecast as a dashed line and dots: two charts drawn the same way, by day and by hour.
  // Colours: the two series' validated pair (blue forecast, orange measured, passes light and dark backgrounds), and the
  // forecast also differs by shape (dashed line + dots) so colour is never the only cue.
  _fcChart(rows, o) {
    const FC = "#4c8bf5", ACT = "#e26c08";
    const vals = rows.flatMap((r) => [r.forecast, r.actual]).filter((v) => v != null);
    if (!vals.length) return `<div class="sub" style="color:var(--secondary-text-color)">Nothing recorded yet.</div>`;
    // drawn at about the width it is shown at, so the labels stay readable on a phone instead of shrinking with the chart
    const W = Math.round(Math.max(340, Math.min(640, (this.offsetWidth || 680) - 72))), H = W < 480 ? 180 : 200;
    const L = 40, B = 24, T = 8, pw = W - L - 8, ph = H - B - T, n = rows.length, gw = pw / n, bw = Math.max(2, Math.min(18, gw * 0.62));
    const nt = this._niceTicks(0, Math.max(0.1, ...vals), 4);
    const y = (v) => T + ph * (1 - (v - nt.lo) / Math.max(1e-9, nt.hi - nt.lo));
    const cx = (i) => L + i * gw + gw / 2;
    const fmt = (v) => (v == null ? "–" : `${v < 10 ? v.toFixed(2) : v.toFixed(1)} kWh`);
    let g = (nt.ticks || []).map((tv) => `<line x1="${L}" x2="${W - 8}" y1="${y(tv).toFixed(1)}" y2="${y(tv).toFixed(1)}" stroke="currentColor" stroke-opacity=".12"/><text x="${L - 6}" y="${(y(tv) + 4).toFixed(1)}" text-anchor="end" font-size="11" fill="currentColor" fill-opacity=".7">${tv}</text>`).join("");
    const every = Math.ceil(n / Math.max(4, Math.floor(pw / 60)));          // about one label per 60 units of width
    rows.forEach((r, i) => {
      if (r.actual != null && r.actual > 0) g += `<rect x="${(cx(i) - bw / 2).toFixed(1)}" y="${y(r.actual).toFixed(1)}" width="${bw.toFixed(1)}" height="${Math.max(1, y(0) - y(r.actual)).toFixed(1)}" rx="2" fill="${ACT}"/>`;
      if ((i % every === 0 && n - 1 - i >= every / 2) || i === n - 1) g += `<text x="${cx(i).toFixed(1)}" y="${H - 7}" text-anchor="middle" font-size="11" fill="currentColor" fill-opacity=".7">${esc(o.xlab(r, i))}</text>`;
    });
    let d = "", pen = false;                                     // the forecast line breaks where the forecast is not known
    rows.forEach((r, i) => { if (r.forecast == null) { pen = false; return; } d += `${pen ? "L" : "M"}${cx(i).toFixed(1)},${y(r.forecast).toFixed(1)}`; pen = true; });
    if (d) g += `<path d="${d}" fill="none" stroke="${FC}" stroke-width="2" stroke-dasharray="6 4" stroke-linejoin="round"/>`;
    rows.forEach((r, i) => { if (r.forecast) g += `<circle cx="${cx(i).toFixed(1)}" cy="${y(r.forecast).toFixed(1)}" r="4" fill="${FC}" stroke="var(--card-background-color, #fff)" stroke-width="2"/>`; });
    // hover: the whole column is the target (bigger than the marks), and says both values
    rows.forEach((r, i) => { g += `<rect x="${(L + i * gw).toFixed(1)}" y="${T}" width="${gw.toFixed(1)}" height="${ph}" fill="transparent"><title>${esc(o.title(r))}: forecast ${fmt(r.forecast)}, measured ${fmt(r.actual)}</title></rect>`; });
    return `<svg viewBox="0 0 ${W} ${H}" style="width:100%;height:auto;color:var(--primary-text-color)" role="img" aria-label="${esc(o.aria)}">${g}</svg>
      <div class="legend"><span><i class="sw" style="background:${ACT}"></i>Measured solar</span><span><svg width="22" height="10" aria-hidden="true" style="vertical-align:middle;margin-right:4px"><line x1="0" x2="22" y1="5" y2="5" stroke="${FC}" stroke-width="2" stroke-dasharray="5 3"/><circle cx="11" cy="5" r="3.5" fill="${FC}"/></svg>Forecast</span><span style="color:var(--secondary-text-color)">kWh</span></div>`;
  },

  _solarForecastCard() {
    const c = this._solarCmp, sf = this.live.solar_forecast;
    if (!c || !Array.isArray(c.days) || !Array.isArray(c.hours) || (!sf && !c.days.some((r) => r.forecast != null))) return "";
    const src = { forecast_solar: "Forecast.Solar", solcast_solar: "Solcast" }[c.source] || "the solar forecast";
    const today = c.days.length ? c.days[c.days.length - 1].day : c.day;
    // whole days only: today's measured solar is only what has come so far, against a forecast for the whole day
    const both = c.days.filter((r) => r.day !== today && r.forecast != null && r.actual != null && r.forecast > 0);
    const sumF = both.reduce((a, r) => a + r.forecast, 0), sumA = both.reduce((a, r) => a + r.actual, 0);
    const verdict = both.length >= 3 && sumF > 0
      ? `<p style="margin:4px 0">Over the last ${both.length} days with both, the solar measured was <b>${Math.round((100 * sumA) / sumF)}%</b> of what ${esc(src)} forecast.</p>` : "";
    const yday = c.days.length > 1 ? c.days[c.days.length - 2].day : null;
    const dayBtn = (d, label) => (d ? `<button class="btn sm ${c.day === d ? "pri" : ""}" data-act="solar-day" data-day="${esc(d)}">${label}</button>` : "");
    const md = (s) => { const [, m, dd] = String(s).split("-"); return `${+dd}/${+m}`; };
    const csv = this._csvLink("solar-forecast-vs-measured.csv", [["day", "forecast_kwh", "measured_kwh"], ...c.days.map((r) => [r.day, r.forecast, r.actual])]);
    return `<div class="card" style="margin-top:16px"><h2><ha-icon icon="mdi:weather-sunny"></ha-icon>Solar forecast against what came</h2>
      ${c.measured ? "" : `<div class="banner">None of your batteries reports solar, so HomePower cannot measure it: only the forecast is shown.</div>`}
      ${verdict}
      <h3 style="margin:10px 0 4px;font-size:14px">By day</h3>
      ${this._fcChart(c.days, { xlab: (r) => md(r.day), title: (r) => (r.day === today ? `${r.day} (today, so far)` : r.day), aria: "Solar forecast and measured solar for each day" })}
      <h3 style="margin:14px 0 4px;font-size:14px">By hour, ${md(c.day)} <span class="durs">${dayBtn(yday, "Yesterday")} ${dayBtn(today, "Today")}</span></h3>
      ${this._fcChart(c.hours, { xlab: (r) => String(r.hour).padStart(2, "0"), title: (r) => `${c.day} ${String(r.hour).padStart(2, "0")}:00`, aria: "Solar forecast and measured solar for each hour" })}
      <p class="sub" style="font-size:12px;color:var(--secondary-text-color)">Forecast from ${esc(src)}: by day, the total first seen that day (what the day was planned with); by hour, the last forecast made before each hour. Today's bar is the solar so far, so it is left out of the percentage. Measured solar is what your batteries report, so panels on a separate inverter are not included. ${csv}</p></div>`;
  },

  _solarStringsCard() {
    const devs = this.data.devices.filter((d) => ((this.live.devices || {})[d.device_id] || {}).pv);
    const blocks = devs.map((d) => { const h = this._pvHtml(this.live.devices[d.device_id].pv); return h ? `<h3>${esc(d.name)}</h3>${h}` : ""; }).join("");
    if (!blocks) return "";
    return `<div class="card" style="margin-top:16px"><h2><ha-icon icon="mdi:solar-panel"></ha-icon>Solar strings</h2>${blocks}
      <p class="sub" style="font-size:12px;color:var(--secondary-text-color)">Each string is compared with the others on the same inverter, so the weather cancels out. A string that gives almost nothing for half an hour while the others produce, or a clearly smaller share of the day's solar than usual two days running, raises an alert.</p></div>`;
  },

  _insightsView() {
    const L = this.live, E = L.energy || {}, cur = esc(L.currency || "£"), ids = this.data.entity_ids || {}, H = this._hist2 || {};
    const pts = (eid) => (H[eid] || []).filter((r) => r.mean != null).map((r) => ({ t: typeof r.start === "number" ? r.start : Date.parse(r.start), v: r.mean }));
    const powerSeries = [];
    if (ids.battery_power) powerSeries.push({ name: "Battery power (+ charging)", color: SERIES[0], pts: pts(ids.battery_power) });
    if (ids.grid_power) powerSeries.push({ name: "Grid (+ importing)", color: SERIES[1], pts: pts(ids.grid_power) });
    const socSeries = this.data.devices.map((d, i) => ({ name: d.name, color: this._color(i), pts: pts(((ids.devices || {})[d.device_id] || {}).soc) }));
    const kwh = (k) => (E[k] || 0).toFixed(1);
    const rows = this.data.devices.map((d) => {
      const cin = E[`bat:${d.device_id}_in`] || 0, out = E[`bat:${d.device_id}_out`] || 0;
      return `<tr><td>${esc(d.name)}</td><td>${kwh(`bat:${d.device_id}_in`)} kWh</td><td>${kwh(`bat:${d.device_id}_out`)} kWh</td><td>${cin >= 1 ? Math.round((out / cin) * 100) + "%" : "–"}</td><td>${(out / Math.max(d.capacity_wh / 1000, 0.1)).toFixed(1)}</td></tr>`;
    }).join("");
    const rng = ["6h", "12h", "24h", "3d", "7d", "30d"].map((r) => `<button class="btn sm ${((this._range || "24h") === r) ? "pri" : ""}" data-act="range" data-r="${r}">${r}</button>`).join(" ");
    const tile = (l, v, sub) => `<div class="card tile"><div class="lbl">${l}</div><div class="val">${v}</div><div class="sub">${sub}</div></div>`;
    return `<div class="bar-actions"><h2>Insights</h2></div>
      <div class="grid g-tiles">
        ${tile("Saved today", `${cur}${(+(L.savings && L.savings.today) || 0).toFixed(2)}`, "Estimate: avoided import minus grid-charging cost")}
        ${tile("Saved this month", `${cur}${(+(L.savings && L.savings.month) || 0).toFixed(2)}`, "Needs prices set in Settings")}
        ${tile("Grid imported", `${kwh("grid_in")} kWh`, `Exported ${kwh("grid_out")} kWh`)}
        ${tile("Solar", `${kwh("pv_in")} kWh`, "As reported by your batteries")}
        ${L.daily && L.daily.self_consumption != null ? tile("Solar used at home today", `${L.daily.self_consumption}%`, "Not exported to the grid") : ""}
        ${L.daily && L.daily.cheap_share != null ? tile("Cheap-rate share today", `${L.daily.cheap_share}%`, "Of the grid energy you imported") : ""}
        ${L.export && L.export.price_now != null && L.export.buyback != null ? tile("Exporting now pays", `${cur}${L.export.price_now.toFixed(3)}`, `Buying back costs ${cur}${L.export.buyback.toFixed(3)}${L.export.pays ? ": worth exporting" : ""}${L.export.on ? "" : " (paid export is off)"}`) : ""}
      </div>${this._solarForecastCard()}${this._tariffCard(cur)}${this._solarStringsCard()}${this._healthCard()}${this._ecoflowCard()}${this._planCard()}${this._trendCard()}${this._gridEventsCard()}${this._whatifCard()}
      <div class="card" style="margin-top:16px"><h2><ha-icon icon="mdi:battery-sync"></ha-icon>Batteries (since HomePower started recording)</h2>
        <table class="tbl"><tr><th>Battery</th><th>Charged</th><th>Discharged</th><th>Round-trip</th><th>Cycles</th></tr>${rows || ""}</table>
        <p class="sub" style="font-size:12px;color:var(--secondary-text-color)">These are also Home Assistant sensors, so you can add them to the Energy dashboard.</p></div>
      <div class="card" style="margin-top:16px"><h2><ha-icon icon="mdi:chart-areaspline"></ha-icon>History ${rng}</h2>${this._tabError ? `<div class="err">${esc(this._tabError)}</div>` : ""}
        ${this._lineChart(powerSeries, "W")}<h2 style="margin-top:16px">Charge level</h2>${this._lineChart(socSeries, "%")}</div>`;
  },

  };
};
