/* HomePower panel: part of homepower-panel.js (loaded by it; methods are added to the panel class). */
export default (U) => {
  const { MODES, SERIES, DAYS, esc, fmtW, fmtKwh, slug, CSS } = U;
  return {
  // All batteries as one: charge level, stored energy, flow, and how long it will last / take to fill.
  _readinessCard() {
    const r = this.live.readiness;
    if (!r) return "";
    const txt = r.hours == null ? "not worked out yet (no usual-use pattern learned and no load reading)" : `${r.capped ? "more than " : "about "}${r.hours} h`;
    return `<div class="card" style="margin-top:16px"><h2><ha-icon icon="mdi:home-lightning-bolt-outline"></ha-icon>If the grid went down now</h2>
      <p style="margin:4px 0">The batteries could run the house for <b>${txt}</b> (${r.usable_kwh} kWh usable, at ${esc(r.basis)}, ignoring solar).</p></div>`;
  },

  _homeBattery() {
    const c = this.live.combined;
    if (!c || c.soc == null || (this.data.devices || []).length < 2) return "";
    const flow = c.flow_w || 0, kind = Math.abs(flow) < 20 ? "Idle" : flow > 0 ? "Charging" : "Discharging";
    const n = (v, l) => `<div><b>${v}</b><span>${l}</span></div>`;
    return `<div class="card" style="margin-bottom:16px"><h2><ha-icon icon="mdi:home-battery"></ha-icon>Home battery <span class="sub" style="font-weight:400;font-size:13px">all ${c.total} batteries together${c.online < c.total ? ` (${c.online} online)` : ""}</span></h2>
      <div class="hb">${this._ring(c.soc, 110)}<div class="nums">
        ${n(fmtKwh(c.stored_kwh * 1000), `stored of ${fmtKwh(c.capacity_kwh * 1000)}`)}
        ${n(fmtKwh(c.usable_kwh * 1000), "usable above the minimum")}
        ${n(`${kind} ${fmtW(Math.abs(flow))}`, "right now")}
        ${c.runtime_h != null ? n(this._hours(c.runtime_h), `lasts at ${fmtW(Math.abs(flow) > 20 ? Math.abs(flow) : c.load_w)}`) : ""}
        ${c.full_in_h != null ? n(this._hours(c.full_in_h), "until full at this rate") : ""}
        ${c.load_w != null ? n(fmtW(c.load_w), "house load") : ""}</div></div></div>`;
  },

  // Cheap-rate hours are the single most important setting for planning: nudge until they are entered.
  _tariffBanner() {
    const s = this.data.settings || {};
    if ((s.offpeak_start && s.offpeak_end) || s.offpeak_extra || (this.live.setup || []).length) return "";   // the setup check covers it
    const inf = this.live.tariff_inferred;
    if (!inf && this.live.price_now != null) return "";   // per-slot prices (Agile, Nord Pool...): no fixed cheap hours to enter
    const msg = inf ? `Your cheap-rate hours are being <b>estimated</b> as ${inf.start}-${inf.end} from price history, which can run up to an hour late.`
      : "HomePower does not know your cheap-rate hours yet, so goals cannot pick the cheapest time to charge.";
    return `<div class="banner"><ha-icon icon="mdi:cash-clock"></ha-icon><div class="t">${msg} Enter them once for exact planning.</div>
      <button class="btn pri sm" data-act="goto-tariff">Set my tariff</button></div>`;
  },

  _scenesCard() {
    const sc = this.data.scenes || [], L = this.live, paused = !!this.data.paused;
    const on = (x) => !!x.backup === !!L.backup && !!x.away === !!L.away && !!x.paused === paused;
    return `<div class="card" style="margin-top:16px"><h2><ha-icon icon="mdi:palette-swatch-variant"></ha-icon>Scenes</h2>
      <div class="scenes">${sc.map((x) => `<span style="display:inline-flex"><button class="btn sm ${on(x) ? "pri" : ""}" data-act="apply-scene" data-id="${esc(x.name)}" title="Switch to this scene">${esc(x.name)}</button><button class="btn sm" data-act="edit-scene" data-id="${esc(x.name)}" title="Edit" aria-label="Edit ${esc(x.name)}">⋯</button></span>`).join("")}
        <button class="btn sm" data-act="add-scene"><ha-icon icon="mdi:plus"></ha-icon>New scene</button></div>
      <p class="sub" style="font-size:12px;color:var(--secondary-text-color);margin:8px 0 0">A scene is a saved combination of backup / away / pause and optional limits, switched in one tap (or with the homepower.apply_scene service).</p></div>`;
  },

  _sceneModal(x, isNew) {
    const st = x.settings || {}, num = (k, l) => `<div class="f"><label>${l}</label><input type="number" id="sc-${k}" min="0" step="100" value="${st[k] ?? ""}" placeholder="unchanged"></div>`;
    this._openModal(`<div class="overlay" data-close-bg><div class="modal" role="dialog" aria-label="Scene">
      <h2>${isNew ? "New scene" : "Edit scene"}</h2>
      <div class="f"><label>Name</label><input type="text" id="sc-name" maxlength="40" value="${esc(x.name || "")}" placeholder="e.g. EV charging night"></div>
      <label style="display:flex;gap:8px;align-items:center;margin:8px 0"><input type="checkbox" id="sc-backup" ${x.backup ? "checked" : ""}>Backup mode on</label>
      <label style="display:flex;gap:8px;align-items:center;margin:8px 0"><input type="checkbox" id="sc-away" ${x.away ? "checked" : ""}>Away mode on</label>
      <label style="display:flex;gap:8px;align-items:center;margin:8px 0"><input type="checkbox" id="sc-paused" ${x.paused ? "checked" : ""}>Pause control (only read, send no commands)</label>
      <h3>Limits this scene sets (leave empty to keep the current value)</h3>
      <div class="two">${num("import_limit_w", "House import limit (W)")}${num("export_limit_w", "Export limit (W)")}</div>
      <div class="two">${num("total_charge_limit_w", "Max total charge (W)")}${num("total_discharge_limit_w", "Max total discharge (W)")}</div>
      <div class="err" id="m-err"></div>
      <div class="foot">${isNew ? "<span></span>" : `<button class="btn danger" data-act="del-scene" data-id="${esc(x.name)}">Delete</button>`}
        <div class="r"><button class="btn" data-act="close">Cancel</button><button class="btn pri" data-act="save-scene" data-orig="${isNew ? "" : esc(x.name)}">Save</button></div></div>
    </div></div>`);
  },

  _collectScene() {
    const r = this.shadowRoot, v = (id) => r.getElementById(id).value, settings = {};
    for (const k of ["import_limit_w", "export_limit_w", "total_charge_limit_w", "total_discharge_limit_w"]) if (v("sc-" + k) !== "") settings[k] = +v("sc-" + k);
    return { name: v("sc-name").trim(), backup: r.getElementById("sc-backup").checked, away: r.getElementById("sc-away").checked, paused: r.getElementById("sc-paused").checked, settings };
  },

  _overview() {
    const ds = this.data.devices;
    if (!ds.length) {
      return `<div class="card empty"><ha-icon icon="mdi:home-battery-outline"></ha-icon><h3>No batteries yet</h3>
        Add your first battery to start seeing live power and SOC.<br><br>
        <button class="btn pri" data-act="add-battery"><ha-icon icon="mdi:plus"></ha-icon>Add battery</button></div>`;
    }
    let net = 0, stored = 0, cap = 0, online = 0;
    const rd = ds.map((d) => {
      const r = this._reading(d);
      if (r.power != null) net += r.power;
      if (r.soc != null) stored += (r.soc / 100) * d.capacity_wh;
      cap += d.capacity_wh;
      if (r.online) online++;
      return r;
    });
    const grid = this._gridW();
    const s = this.data.settings || {};
    const netTxt = Math.abs(net) < 20 ? "Idle" : net > 0 ? "Charging" : "Discharging";
    const tile = (l, v, sub, c) => `<div class="card tile"><div class="lbl">${l}</div><div class="val" ${c ? `style="color:${c}"` : ""}>${v}</div><div class="sub">${sub}</div></div>`;
    const tiles = `<div class="grid g-tiles">
      ${tile("Battery flow", fmtW(Math.abs(net)), netTxt + (s.total_charge_limit_w && net > 0 ? ` · cap ${fmtW(s.total_charge_limit_w)}` : "") + (s.total_discharge_limit_w && net < 0 ? ` · cap ${fmtW(s.total_discharge_limit_w)}` : ""), net > 20 ? U.INK.charge : net < -20 ? U.INK.discharge : "")}
      ${tile("Stored energy", fmtKwh(stored), `${cap ? Math.round((stored / cap) * 100) : 0}% of ${fmtKwh(cap)}`)}
      ${tile("Grid", grid == null ? "–" : fmtW(Math.abs(grid)), grid == null ? "No grid reading (Settings > Grid meter)" : Math.abs(grid) < 20 ? "Balanced" : grid > 0 ? "Importing" : "Exporting", grid == null ? "" : grid > 20 ? U.INK.err : grid < -20 ? U.INK.charge : "")}
      ${tile("Batteries online", `${online} / ${ds.length}`, "Reading directly from the batteries")}
      ${this.live.savings ? tile("Saved today", `${esc(this.live.currency || "£")}${(+this.live.savings.today || 0).toFixed(2)}`, `This month ${esc(this.live.currency || "£")}${(+this.live.savings.month || 0).toFixed(2)} (estimate)`, this.live.savings.today >= 0 ? U.INK.charge : U.INK.err) : ""}
      ${this._solarTile(tile)}
      ${this.live.price_now != null ? tile("Price now", `${esc(this.live.currency || "£")}${this.live.price_now.toFixed(3)}/kWh`, "From your rates") : ""}
    </div>`;
    const cards = ds.map((d, i) => this._batCard(d, rd[i], i)).join("");
    return `${this._alertsBanner()}${this._setupCard(false)}${this._tariffBanner()}${this._homeBattery()}${this._readinessCard()}${tiles}
      <h2 style="margin:22px 0 10px;font-size:18px">Batteries</h2>
      <div class="grid g-cards">${cards}</div>
      ${this._scenesCard()}
      <div class="card" style="margin-top:16px"><h2><ha-icon icon="mdi:chart-areaspline"></ha-icon>Power, last ${this._durLabel(this._flowMin())}<span class="durs">${this._durButtons()}</span></h2>${this._flowChart()}</div>
      <div class="card" style="margin-top:16px"><h2><ha-icon icon="mdi:timeline-clock"></ha-icon>Today's plan</h2>${this._timeline()}</div>
      <div class="card" style="margin-top:16px"><h2><ha-icon icon="mdi:text-box-outline"></ha-icon>Activity</h2>${this._log()}</div>`;
  },

  // Today's solar forecast (Forecast.Solar or Solcast, found by themselves): shown only when one is set up.
  _solarTile(tile) {
    const sf = this.live.solar_forecast;
    if (!sf) return "";
    const k = (v) => `${(+v).toFixed(1)} kWh`;
    const sub = [sf.so_far_kwh != null ? `${k(sf.so_far_kwh)} so far` : null, sf.remaining_kwh != null ? `${k(sf.remaining_kwh)} still to come` : null,
      sf.tomorrow_kwh != null ? `tomorrow ${k(sf.tomorrow_kwh)}` : null].filter(Boolean).join(" · ");
    const src = { forecast_solar: "Forecast.Solar", solcast_solar: "Solcast" }[sf.source] || "your solar forecast";
    const pct = sf.use_pct != null && sf.use_pct !== 100 ? ` (${+sf.use_pct}% of its figures, as set in Settings)` : "";
    return tile("Solar forecast today", sf.today_kwh != null ? k(sf.today_kwh) : "–", esc(sub || src))
      .replace('<div class="card tile"', `<div class="card tile" title="From ${esc(src)}${pct}. Forecast against what really came: Insights."`);
  },

  _ring(soc, size = 96) {
    const r = 40, c = 2 * Math.PI * r, v = soc == null ? 0 : Math.max(0, Math.min(100, soc));
    const col = soc == null ? "var(--divider-color)" : soc < 20 ? U.INK.err : soc < 50 ? U.INK.warn : U.INK.ok;
    return `<svg class="ring" width="${size}" height="${size}" viewBox="0 0 100 100" role="img" aria-label="State of charge ${soc == null ? "unknown" : Math.round(soc) + " percent"}">
      <circle cx="50" cy="50" r="${r}" fill="none" stroke="var(--divider-color)" stroke-width="10"/>
      <circle cx="50" cy="50" r="${r}" fill="none" stroke="${col}" stroke-width="10" stroke-linecap="round" stroke-dasharray="${(v / 100) * c} ${c}" transform="rotate(-90 50 50)"/>
      <text x="50" y="${soc == null ? 56 : 54}" text-anchor="middle" font-size="${soc == null ? 20 : 24}" font-weight="700">${soc == null ? "–" : Math.round(soc) + "%"}</text></svg>`;
  },

  _batCard(d, r, i) {
    const lv = r.lv || {};
    const p = r.power;
    const state = r.waiting ? "Waiting for readings" : !r.online ? "Offline" : p == null ? "–" : Math.abs(p) < 20 ? "Idle" : p > 0 ? "Charging" : "Discharging";
    const col = !r.online ? "var(--secondary-text-color)" : p > 20 ? U.INK.charge : p < -20 ? U.INK.discharge : "var(--secondary-text-color)";
    const mode = lv.mode && MODES[lv.mode];
    const want = lv.desired_mode && MODES[lv.desired_mode];
    const chips = [];
    const sent = d.control_enabled !== false && !this.data.paused;   // monitor only or paused: nothing is sent, so no "Target" (their own chips say why)
    if (want && sent) chips.push(`<span class="chip m" style="background:${want.color};color:${U.onFill(want.color)}" title="${esc(want.help)}">Target: ${want.label}${lv.desired_power_w ? " " + fmtW(lv.desired_power_w) : ""}</span>`);
    else if (r.online && !this.data.paused) chips.push(`<span class="chip">Device's own control</span>`);
    if (r.stale || r.waiting) chips.push(this._linkChip(r));
    if (mode) chips.push(`<span class="chip"><span class="dot" style="background:${mode.color}"></span>Mode: ${mode.label}</span>`);
    const room = ((this.live.solar_room || {}).batteries || {})[d.device_id];
    if (room && room.ceiling < 100) chips.push(`<span class="chip" title="Overnight charging stops here so the solar forecast (${room.solar_kwh} kWh for this battery) has room">Room for solar: up to ${room.ceiling}%</span>`);
    if (lv.has_solar || (lv.pv_w || 0) > 0) chips.push(`<span class="chip" title="Solar panels attached to this battery"><ha-icon icon="mdi:solar-power" style="--mdc-icon-size:14px"></ha-icon>Solar${lv.pv_w != null ? " " + fmtW(lv.pv_w) : ""}</span>`);
    if ((this.live.forecast_reserve || {}).active) chips.push(`<span class="chip" title="A dull day is forecast, so HomePower will not drain the batteries below this reserve (Settings > Backup & away)">Reserve ${this.live.forecast_reserve.pct}% (dull forecast)</span>`);
    if (lv.off_grid) chips.push(`<span class="chip warn" title="The battery reports the grid is down and is running the house itself. HomePower sends it no commands until the grid is back."><ha-icon icon="mdi:transmission-tower-off" style="--mdc-icon-size:14px"></ha-icon>Grid down</span>`);
    else if (lv.protected) chips.push(`<span class="chip warn"><ha-icon icon="mdi:shield-alert" style="--mdc-icon-size:14px"></ha-icon>Protection active</span>`);
    if (lv.limits) chips.push(`<span class="chip" title="The battery's own limits, set in the Anker app: it charges to ${lv.limits[0]}% at most and stops discharging at ${lv.limits[1]}%${lv.limits[3] ? `; backup reserve ${lv.limits[2]}%` : ""}. HomePower cannot charge it past its own limit.">Battery limits ${lv.limits[1]}–${lv.limits[0]}%</span>`);
    if (lv.temp_c != null) chips.push(`<span class="chip" title="Inverter internal temperature">${lv.temp_c.toFixed(0)}°C</span>`);
    for (const al of lv.alarms || []) chips.push(`<span class="chip warn" title="The device itself reports this">${esc(al)}</span>`);
    if (lv.reason) chips.push(`<span class="chip" title="Why HomePower is doing this">${esc(lv.reason)}</span>`);
    if (lv.override) chips.push(`<span class="chip m" style="background:#7c3aed;color:${U.onFill("#7c3aed")}">Manual: ${(MODES[lv.override.mode] || {}).label} until ${new Date(lv.override.until).toLocaleTimeString([], this._tzOpt({ hour: "2-digit", minute: "2-digit" }))}</span>`);
    if (d.control_enabled === false) chips.push(`<span class="chip warn" title="HomePower reads this battery but never sends it commands">Monitor only</span>`);
    if (lv.guard_held) chips.push(`<span class="chip" title="Held by the SOC limit">SOC limit reached</span>`);
    if (lv.last_error) chips.push(`<span class="chip bad" title="${esc(lv.last_error)}">Last write failed</span>`);
    if (lv.awaiting_confirm) chips.push(`<button class="btn sm pri" data-act="confirm-first" data-id="${esc(d.device_id)}" title="This battery's control has not been proven yet. Allow HomePower to send its first real command (or pass Test control).">Allow first command</button>`);
    if (lv.charge_verdict === "unreliable") chips.push(`<span class="chip bad" title="Several Charge commands had no effect on this battery, so HomePower no longer sends it Charge: schedules that ask for it leave the battery on its own control. Pass Test control to switch it back on.">Charge not working</span>`);
    if ((lv.traces || []).length) chips.push(`<span class="chip" title="${esc(lv.traces.map((t) => `${(t.at || "").slice(5, 16).replace("T", " ")}  ${t.mode}: ${t.ok ? "reacted after " + t.reaction_s + " s" : "no effect"}, peak ${t.peak_w} W`).join("\n"))}">Command results (${lv.traces.length})</span>`);
    if (lv.why && !lv.last_error && d.control_enabled !== false && lv.online) chips.push(`<span class="chip warn" title="${esc(lv.why)}">Why idle: ${esc(lv.why.length > 48 ? lv.why.slice(0, 46) + "…" : lv.why)}</span>`);
    if (lv.measured_charge_w) chips.push(`<span class="chip" title="The charge power this battery has really shown while charging; it picks its own rate. Plans use it.">Charges at ~${fmtW(lv.measured_charge_w)} (measured)</span>`);
    if ((lv.setting_changes || []).length) chips.push(`<span class="chip" title="${esc(lv.setting_changes.map((c) => `${c.at}  ${c.key}: ${c.from} → ${c.to}`).join("\n"))}">Settings changed (${lv.setting_changes.length})</span>`);
    if (this.data.settings.zero_import_device === d.device_id) chips.push(`<span class="chip"><ha-icon icon="mdi:scale-balance" style="--mdc-icon-size:14px"></ha-icon>Zero-import</span>`);
    for (const sid of lv.active_schedules || []) chips.push(`<span class="chip"><ha-icon icon="mdi:calendar-clock" style="--mdc-icon-size:14px"></ha-icon>${esc(sid)}</span>`);
    return `<div class="card"><div class="bat">${this._ring(r.soc)}
      <div class="info"><div class="nm"><span class="dot" style="background:${this._color(i)}"></span>${esc(d.name)}<button class="btn sm" style="margin-left:auto;padding:2px 6px" title="All settings and readings" aria-label="Battery details" data-act="battery-details" data-id="${esc(d.device_id)}"><ha-icon icon="mdi:cog" style="--mdc-icon-size:16px"></ha-icon></button></div>
      <div class="pw" style="color:${col}">${state}${r.online && p != null && Math.abs(p) >= 20 ? " · " + fmtW(Math.abs(p)) : ""}</div>
      ${!r.online && (lv.last_read_ok || lv.offline_hint || lv.offline_reason) ? `<div class="sub" style="font-size:12px;color:var(--secondary-text-color);margin-top:4px">${lv.last_read_ok ? "Last read " + esc(new Date(lv.last_read_ok).toLocaleTimeString([], this._tzOpt({ hour: "2-digit", minute: "2-digit" }))) + ". " : ""}${lv.offline_reason ? esc(lv.offline_reason.charAt(0).toUpperCase() + lv.offline_reason.slice(1)) + ". " : ""}${esc(lv.offline_hint || "")}</div>` : ""}
      <div class="sub" style="font-size:12px;color:var(--secondary-text-color)">${r.soc != null ? fmtKwh((r.soc / 100) * d.capacity_wh) + " of " : ""}${fmtKwh(d.capacity_wh)} · max ${fmtW(d.max_charge_w)} in / ${fmtW(d.max_discharge_w)} out</div>
      <div class="chips">${chips.join("")}</div></div></div>${this._spark(this._hist[d.device_id] || [], this._color(i))}
      <div style="display:flex;gap:8px;margin-top:10px"><button class="btn sm" data-act="override" data-id="${esc(d.device_id)}"><ha-icon icon="mdi:hand-back-right" style="--mdc-icon-size:16px"></ha-icon>Override</button>${lv.override ? `<button class="btn sm" data-act="clear-override" data-id="${esc(d.device_id)}">Clear override</button>` : ""}</div></div>`;
  },

  _spark(h, color) {
    const pts = h.filter((x) => x.p != null);
    if (pts.length < 2) return `<div class="spark" style="font-size:12px;color:var(--secondary-text-color);display:flex;align-items:center">Collecting live data…</div>`;
    return `<svg class="spark" viewBox="0 0 300 46" preserveAspectRatio="none" aria-hidden="true">${this._path(pts, 300, 46, color, true)}</svg>`;
  },

  };
};
