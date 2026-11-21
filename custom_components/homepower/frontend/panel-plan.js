/* HomePower panel: part of homepower-panel.js (loaded by it; methods are added to the panel class). */
export default (U) => {
  const { MODES, SERIES, DAYS, esc, fmtW, fmtKwh, slug, CSS } = U;
  return {
  // ---- alerts banner ------------------------------------------------------
  _alertsBanner() {
    const out = [];
    const nameOf = (id) => (this.data.devices.find((d) => d.device_id === id) || {}).name || id;
    const TEXT = { offline: "offline - HomePower cannot read it", error: "a command failed", unconfirmed: "is not following HomePower's commands",
      protect: "manufacturer protection is active", grid: "reports the grid is down and is running the house itself",
      stuck: "was told to charge but is not charging (check its own app for a mode or schedule overriding HomePower)",
      gridsign: "its grid reading points the opposite way to your grid meter (one of them is reversed)",
      health: "its round-trip efficiency has dropped (see Insights)", trend: "its temperature or voltage is drifting (see Insights)" };
    for (const a of this.live.alerts || []) {
      const [kind, id, ...rest] = a.split(":");                      // alerts are "kind:device_id[:detail]", or a bare kind
      if (kind === "tariffdrift") { out.push(`<div class="banner warn">Your cheap-rate hours no longer match the price history: check them in Settings.</div>`); continue; }
      const what = kind === "alarm" ? `reports: ${rest.join(":")}` : TEXT[kind] || kind;   // an alarm key carries the battery's own alarm text
      out.push(`<div class="banner warn">${id ? esc(nameOf(id)) + ": " : ""}${esc(what)}</div>`);
    }
    for (const n of this.live.notes || []) out.push(`<div class="banner info">${esc(n)}</div>`);
    if (this.live.backup) out.push(`<div class="banner info">Backup mode is on: batteries are topped up to the backup reserve and not discharged below it.</div>`);
    if (this.live.away) out.push(`<div class="banner info">Away mode is on: only schedules for "away" (or "always") apply.</div>`);
    return out.join("");
  },

  // ---- plan tab: goals, 24h preview, conflicts ----------------------------------
  // Prices for the next 24 hours as bars, with the half-hours HomePower plans to charge in picked out.
  _priceChart() {
    const pv = this._preview, pr = (pv && pv.prices) || [];
    if (pr.length < 2) return "";
    const cur = esc(this.live.currency || "£"), W = 800, H = 200;
    const t0 = Date.parse(pr[0].start), t1 = Date.parse(pr[pr.length - 1].end), span = Math.max(1, t1 - t0);
    const nt = this._niceTicks(Math.min(0, ...pr.map((x) => x.price)), Math.max(...pr.map((x) => x.price), 0.01), 4);
    const X = (t) => ((t - t0) / span) * W, Y = (v) => H - ((v - nt.lo) / Math.max(1e-9, nt.hi - nt.lo)) * H;
    const planned = (a, b) => (pv.plan || []).some((x) => Date.parse(x.start) < b && Date.parse(x.end) > a)
      || Object.values(pv.segments || {}).some((l) => l.some((g) => g.mode === "charge" && Date.parse(g.start) < b && Date.parse(g.end) > a));
    // the day plan (where stored energy is worth most) and grid events, over the same half-hours
    const dp = (pv.day_plan && pv.day_plan.slots) || [];
    const inPlan = (a, b) => dp.filter((s) => Date.parse(s.start) < b && Date.parse(s.end) > a);
    const sum = (l, k) => l.reduce((t, s) => t + (s[k] || 0), 0);
    const events = pv.events || [];
    const bars = pr.map((x) => {
      const a = Date.parse(x.start), b = Date.parse(x.end), y = Y(Math.max(x.price, 0)), h = Math.abs(Y(x.price) - Y(0)), chg = planned(a, b);
      const ps = inPlan(a, b), exp = sum(ps, "export_wh"), house = sum(ps, "house_wh"), kept = ps.length && !house && sum(ps, "load_wh") > 0;
      const fill = chg ? MODES.charge.color : exp > 0 ? MODES.discharge.color : x.cheap ? "#60a5fa" : "var(--disabled-text-color,#9ca3af)";
      const what = [chg ? "charging planned" : "", exp > 0 ? `exporting about ${fmtKwh(exp)}` : "", house > 0 ? `battery runs the house (${fmtKwh(house)})` : "", kept ? "stored energy kept for dearer hours" : ""].filter(Boolean).join(" · ");
      const strip = ps.length ? `<rect x="${(X(a) + 0.6).toFixed(1)}" y="${H - 4}" width="${Math.max(1, X(b) - X(a) - 1.2).toFixed(1)}" height="4" fill="${house > 0 ? "#16a34a" : kept ? "#b45309" : "transparent"}"/>` : "";
      return `<rect x="${(X(a) + 0.6).toFixed(1)}" y="${Math.min(y, Y(0)).toFixed(1)}" width="${Math.max(1, X(b) - X(a) - 1.2).toFixed(1)}" height="${Math.max(1, h).toFixed(1)}" fill="${fill}" opacity="${chg || exp > 0 ? 1 : 0.75}"><title>${new Date(a).toLocaleTimeString([], this._tzOpt({ hour: "2-digit", minute: "2-digit" }))}: ${cur}${x.price.toFixed(3)}/kWh${what ? " · " + what : ""}</title></rect>${strip}`;
    }).join("");
    const shade = events.map((ev) => {
      const a = Math.max(t0, Date.parse(ev.start)), b = Math.min(t1, Date.parse(ev.end));
      return b > a ? `<rect x="${X(a).toFixed(1)}" y="0" width="${(X(b) - X(a)).toFixed(1)}" height="${H}" fill="#7c3aed" opacity="0.12"><title>Grid event (saving session)</title></rect>` : "";
    }).join("");
    const grid = nt.ticks.map((v) => `<line x1="0" x2="${W}" y1="${Y(v).toFixed(1)}" y2="${Y(v).toFixed(1)}" stroke="var(--divider-color)" stroke-opacity=".5" stroke-dasharray="3 4" vector-effect="non-scaling-stroke"/>`).join("");
    const now = Date.now(), nowLine = now > t0 && now < t1 ? `<line x1="${X(now).toFixed(1)}" x2="${X(now).toFixed(1)}" y1="0" y2="${H}" stroke="var(--primary-color)" stroke-width="2" vector-effect="non-scaling-stroke"/>` : "";
    const ylab = nt.ticks.map((v) => `<span style="top:${((1 - (v - nt.lo) / Math.max(1e-9, nt.hi - nt.lo)) * 100).toFixed(2)}%">${cur}${v.toFixed(2)}</span>`).join("");
    const xlab = [0, 0.25, 0.5, 0.75, 1].map((f, i) => `<span style="left:${i * 25}%">${this._fmtT(t0 + f * span, span)}</span>`).join("");
    return `<div class="card" style="margin-top:16px"><h2><ha-icon icon="mdi:chart-bar"></ha-icon>Prices and planned charging (per kWh)</h2>
      <div class="plot"><div class="plot-y" style="height:190px">${ylab}</div><div class="plot-main"><svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" style="height:190px" role="img" aria-label="Electricity prices for the next 24 hours">${grid}${shade}${bars}${nowLine}</svg><div class="plot-x">${xlab}</div></div></div>
      <div class="legend"><span><i class="sw" style="background:${MODES.charge.color}"></i>Charging planned</span><span><i class="sw" style="background:#60a5fa"></i>Cheap rate</span><span><i class="sw" style="background:var(--disabled-text-color,#9ca3af)"></i>Standard rate</span>${dp.some((s) => s.export_wh > 0) ? `<span><i class="sw" style="background:${MODES.discharge.color}"></i>Exporting planned</span>` : ""}${dp.length ? `<span><i class="sw" style="background:#16a34a"></i>Battery runs the house</span><span><i class="sw" style="background:#b45309"></i>Kept for dearer hours</span>` : ""}${events.length ? `<span><i class="sw" style="background:#7c3aed;opacity:.35"></i>Grid event</span>` : ""}<span>Line = now</span></div>${pv.day_plan ? `<p class="sub" style="font-size:12px;color:var(--secondary-text-color);margin:6px 0 0">Stored energy: ${fmtKwh(pv.day_plan.budget_wh)} usable, ${fmtKwh(pv.day_plan.used_wh)} planned before ${new Date(pv.day_plan.until).toLocaleTimeString([], this._tzOpt({ hour: "2-digit", minute: "2-digit" }))}, when buying it back is cheapest. A half-hour must be worth more than ${cur}${pv.day_plan.threshold.toFixed(3)}/kWh (buying back, losses and wear) to use it.</p>` : ""}</div>`;
  },

  // Is everything set up? Shown on Overview until all is done, and always on Diagnostics.
  _setupCard(always) {
    const items = this.live.setup || [], missing = items.filter((x) => !x.ok);
    if (!items.length || (!always && !missing.length)) return "";
    const btn = (x) => x.ok || !x.action ? "<span></span>" : x.action === "test" ? `<button class="btn sm" data-act="setup-test" data-id="${esc(x.target)}">Test now</button>` : `<button class="btn sm" data-tab="${esc(x.target)}">Fix</button>`;
    const list = (always ? items : missing).map((x) => `<div class="it"><ha-icon class="${x.ok ? "ok" : "no"}" icon="${x.ok ? "mdi:check-circle" : "mdi:alert-circle-outline"}"></ha-icon><div>${esc(x.title)}${x.detail ? `<small>${esc(x.detail)}</small>` : ""}</div>${btn(x)}</div>`).join("");
    return `<div class="card" style="margin-bottom:16px"><h2><ha-icon icon="mdi:clipboard-check-outline"></ha-icon>Setup check <span class="sub" style="font-weight:400;font-size:13px">${items.length - missing.length} of ${items.length} done</span></h2><div class="chk">${list}</div></div>`;
  },

  _planView() {
    const goals = this.data.goals || [];
    const names = (g) => !g.device_ids.length ? "All batteries" : g.device_ids.map((id) => (this.data.devices.find((d) => d.device_id === id) || {}).name || id).join(", ");
    const gcards = goals.map((g) => `<div class="card sch ${g.enabled ? "" : "off"}">
      <label class="switch"><input type="checkbox" data-gtoggle="${esc(g.id)}" ${g.enabled ? "checked" : ""}><i></i></label>
      <div class="meta"><div class="when">${g.target_soc}% by ${g.by}</div><div style="font-weight:600">${esc(g.id)}</div>
        <div class="sub">${esc(names(g))}${g.source === "solar" ? " · solar only" : g.source === "both" ? " · solar first, then grid" : ""}${g.use_forecast ? " · less when solar is forecast" : ""}${g.max_power_w ? " · max " + fmtW(g.max_power_w) : ""}</div>
        <div class="dayrow">${DAYS.map((d, i) => `<span class="${g.days.includes(i) ? "on" : ""}">${d.slice(0, 2)}</span>`).join("")}</div></div>
      <div><button class="btn sm" data-act="edit-goal" data-id="${esc(g.id)}">Edit</button></div></div>`).join("");
    const pv = this._preview;
    let timeline = `<div class="sub" style="color:var(--secondary-text-color)">Loading…</div>`;
    if (pv) {
      const t0 = Date.now(), span = 24 * 3600e3;
      const rows = this.data.devices.map((d) => {
        const segs = (pv.segments[d.device_id] || []).map((sg) => {
          const a = Math.max(0, (Date.parse(sg.start) - t0) / span), b = Math.min(1, (Date.parse(sg.end) - t0) / span);
          if (b <= a) return "";
          const m = MODES[sg.mode] || MODES.hold;
          return `<div class="seg2" title="${esc(sg.source)}: ${esc(m.label)}${sg.power_w ? " " + fmtW(sg.power_w) : ""}" style="left:${a * 100}%;width:${(b - a) * 100}%;background:${m.color}">${b - a > 0.12 ? esc(sg.source) : ""}</div>`;
        }).join("");
        return `<div class="row"><div class="nm">${esc(d.name)}</div><div class="bar">${segs}<div class="now" style="left:0"></div></div></div>`;
      }).join("");
      const hrs = [0, 6, 12, 18, 24].map((h) => `<span>${new Date(t0 + h * 3600e3).toLocaleTimeString([], this._tzOpt({ hour: "2-digit", minute: "2-digit" }))}</span>`).join("");
      const conflicts = pv.conflicts.map((c) => `<div class="banner warn">Schedules ${c.schedules.map((x) => `"${esc(x)}"`).join(" and ")} clash on ${esc((this.data.devices.find((d) => d.device_id === c.device_id) || {}).name || c.device_id)} from ${new Date(c.start).toLocaleString([], this._tzOpt({ weekday: "short", hour: "2-digit", minute: "2-digit" }))} to ${new Date(c.end).toLocaleTimeString([], this._tzOpt({ hour: "2-digit", minute: "2-digit" }))}. Give one a higher priority.</div>`).join("");
      const plan = pv.plan.length ? `<details><summary>Planned cheap-rate charging (${pv.plan.length} slots)</summary><table class="tbl"><tr><th>Battery</th><th>From</th><th>To</th><th>Power</th><th>Price</th></tr>${pv.plan.map((x) => `<tr><td>${esc((this.data.devices.find((d) => d.device_id === x.device_id) || {}).name || x.device_id)}</td><td>${new Date(x.start).toLocaleTimeString([], this._tzOpt({ hour: "2-digit", minute: "2-digit" }))}</td><td>${new Date(x.end).toLocaleTimeString([], this._tzOpt({ hour: "2-digit", minute: "2-digit" }))}</td><td>${fmtW(x.power_w)}</td><td>${x.price}</td></tr>`).join("")}</table></details>` : "";
      timeline = `${conflicts}<div class="tl">${rows}<div class="axis"><div></div><div>${hrs}</div></div></div>${plan}
        <div class="legend">${Object.entries(MODES).map(([k, m]) => `<span><i class="sw" style="background:${m.color}"></i>${m.label}</span>`).join("")}<span>Gaps = no instruction (the battery runs itself)</span></div>`;
    }
    return `<div class="bar-actions"><h2>Goals</h2><button class="btn pri" data-act="add-goal"><ha-icon icon="mdi:plus"></ha-icon>Add goal</button></div>
      ${goals.length ? `<div class="grid g-cards">${gcards}</div>` : `<div class="card empty"><ha-icon icon="mdi:target"></ha-icon><h3>No goals yet</h3>"Be at 90% by 07:00": HomePower charges in the cheapest half-hours to get there. It needs prices: set a rates entity or a cheap-rate tariff in Settings.<br><br><button class="btn pri" data-act="add-goal">Add a goal</button></div>`}
      ${this._priceChart()}
      <div class="card" style="margin-top:16px"><h2><ha-icon icon="mdi:timeline-clock"></ha-icon>What will happen next (24 hours)</h2>${this._tabError ? `<div class="err">${esc(this._tabError)}</div>` : ""}${timeline}</div>`;
  },

  _goalModal(g, isNew) {
    const devs = this.data.devices, sel = new Set(g.device_ids);
    this._openModal(`<div class="overlay" data-close-bg><div class="modal" role="dialog" aria-label="Goal">
      <h2>${isNew ? "Add goal" : "Edit goal"}</h2><p class="lead">Be at a charge level by a time, as cheaply as possible.</p>
      <div class="f"><label>Name</label><input type="text" id="gl-name" value="${esc(g.id)}" placeholder="e.g. Ready for the morning"></div>
      <h3>Which batteries</h3><div class="checks">${devs.map((d) => `<label><input type="checkbox" name="gl-dev" value="${esc(d.device_id)}" ${sel.has(d.device_id) ? "checked" : ""}>${esc(d.name)}</label>`).join("")}</div>
      <div class="f"><span class="hint">Leave all unticked for every battery. Batteries that cannot charge from the grid are skipped.</span></div>
      <div class="two"><div class="f"><label>Target charge (%)</label><input type="number" id="gl-target" min="1" max="100" value="${g.target_soc}"></div>
        <div class="f"><label>Be there by</label><input type="time" id="gl-by" value="${g.by}"></div></div>
      <div class="daypick">${DAYS.map((d, i) => `<label><input type="checkbox" name="gl-day" value="${i}" ${g.days.includes(i) ? "checked" : ""}><span>${d}</span></label>`).join("")}</div>
      <div class="f"><span class="hint">The day the deadline falls on.</span></div>
      <div class="f"><label>Charge from</label><select id="gl-source">${[["grid", "Grid: the cheapest half-hours before the deadline"], ["solar", "Solar only: spare solar, nothing from the grid"], ["both", "Solar first, then the cheapest grid half-hours for what is still missing"]].map(([v, l]) => `<option value="${v}" ${(g.source || "grid") === v ? "selected" : ""}>${l}</option>`).join("")}</select>
        <span class="hint">Solar charging follows your grid meter and takes only what would otherwise be exported. Between times the battery runs itself as usual.</span></div>
      <label style="display:flex;gap:8px;align-items:center;margin:8px 0"><input type="checkbox" id="gl-auto" ${g.auto_target ? "checked" : ""}>Work the target out from my usual use (the target above becomes the maximum; it learns for a few days first)</label>
      <label style="display:flex;gap:8px;align-items:center;margin:8px 0"><input type="checkbox" id="gl-forecast" ${g.use_forecast ? "checked" : ""}>Charge less when tomorrow's solar forecast is good (set the forecast entity in Settings)</label>
      <div class="f"><label>Max total charge power for this goal (W, optional)</label><input type="number" id="gl-max" min="0" step="100" value="${g.max_power_w ?? ""}"></div>
      <div class="err" id="m-err"></div>
      <div class="foot">${isNew ? "<span></span>" : `<button class="btn danger" data-act="del-goal" data-id="${esc(g.id)}">Delete</button>`}
        <div class="r"><button class="btn" data-act="close">Cancel</button><button class="btn pri" data-act="save-goal" data-orig="${isNew ? "" : esc(g.id)}">Save</button></div></div>
    </div></div>`);
  },

  _collectGoal() {
    const r = this.shadowRoot, v = (id) => r.getElementById(id).value;
    return { id: v("gl-name").trim(), device_ids: [...r.querySelectorAll('input[name="gl-dev"]:checked')].map((x) => x.value),
      target_soc: v("gl-target"), by: v("gl-by"), days: [...r.querySelectorAll('input[name="gl-day"]:checked')].map((x) => +x.value),
      use_forecast: r.getElementById("gl-forecast").checked, auto_target: r.getElementById("gl-auto").checked, source: v("gl-source") || "grid", max_power_w: v("gl-max") === "" ? null : +v("gl-max"), enabled: true };
  },

  };
};
