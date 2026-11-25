/* HomePower panel: part of homepower-panel.js (loaded by it; methods are added to the panel class). */
export default (U) => {
  const { MODES, SERIES, DAYS, esc, fmtW, fmtKwh, slug, CSS } = U;
  return {
  _log() {
    const ev = (this.live.events || []).slice().reverse().slice(0, 30);
    if (!ev.length) return `<div class="sub" style="color:var(--secondary-text-color)">Nothing yet. Commands HomePower sends to your batteries will be listed here.</div>`;
    const name = (id) => (this.data.devices.find((d) => d.device_id === id) || {}).name || id;
    return `<div class="log">${ev.map(([t, id, m]) => `<div><span class="t">${esc(new Date(t).toLocaleTimeString([], this._tzOpt()))}</span> <b>${esc(name(id))}</b> ${esc(m)}</div>`).join("")}</div>`;
  },

  // ---- schedules tab -----------------------------------------------------
  _schedules() {
    const ss = this.data.schedules;
    const head = `<div class="bar-actions"><h2>Schedules</h2><span><button class="btn" data-act="add-schedule" data-tpl="hold" title="Keep the batteries from charging or discharging for a time, on one day: an expensive evening event, a planned outage"><ha-icon icon="mdi:pause-circle"></ha-icon>Hold window</button> <button class="btn pri" data-act="add-schedule"><ha-icon icon="mdi:plus"></ha-icon>Add schedule</button></span></div>`;
    if (!this.data.devices.length) return head + `<div class="card empty"><ha-icon icon="mdi:home-battery-outline"></ha-icon><h3>Add a battery first</h3>Schedules tell your batteries what to do and when.<br><br><button class="btn pri" data-act="add-battery">Add battery</button></div>`;
    const tl = ss.length ? `<div class="card" style="margin-bottom:16px"><h2><ha-icon icon="mdi:timeline-clock"></ha-icon>Today</h2>${this._timeline()}</div>` : "";
    if (!ss.length) return head + `<div class="card empty"><ha-icon icon="mdi:calendar-clock"></ha-icon><h3>No schedules yet</h3>Try a cheap-rate overnight charge or an evening discharge.<br><br>
      <button class="btn pri" data-act="add-schedule" data-tpl="cheap">Cheap-rate overnight charge</button>
      <button class="btn" data-act="add-schedule" data-tpl="evening">Evening discharge</button>
      <button class="btn" data-act="add-schedule" data-tpl="hold">Hold window (one day)</button></div>`;
    const names = (s) => !s.device_ids.length ? "All batteries" : s.device_ids.map((id) => (this.data.devices.find((d) => d.device_id === id) || {}).name || id).join(", ");
    const cards = ss.slice().sort((a, b) => a.start.localeCompare(b.start)).map((s) => {
      const m = MODES[s.mode];
      const bits = [];
      if (s.power_w) bits.push(fmtW(s.power_w));
      if (s.mode === "charge" && s.source === "solar") bits.push("solar only");
      if (s.mode === "charge" && s.source === "both") bits.push("grid + solar");
      if (s.mode === "charge" && s.target_soc != null) bits.push(`up to ${s.target_soc}%`);
      if (s.mode === "discharge" && s.reserve_soc != null) bits.push(`keep ${s.reserve_soc}%`);
      if (s.condition_entity) bits.push(`while ${s.condition_entity} is on`);
      if (s.on_date) bits.push(`only ${s.on_date}`);
      if (s.season_start) bits.push(`${s.season_start} → ${s.season_end}`);
      if (s.priority) bits.push(`priority ${s.priority}`);
      return `<div class="card sch ${s.enabled ? "" : "off"}">
        <label class="switch" title="Enable / disable"><input type="checkbox" data-toggle="${esc(s.id)}" ${s.enabled ? "checked" : ""}><i></i></label>
        <div class="meta"><div class="when">${s.all_day ? "All day" : `${s.start} – ${s.end}`} <span class="chip m" style="background:${m.color};color:${U.onFill(m.color)};vertical-align:middle;margin-left:6px"><ha-icon icon="${m.icon}" style="--mdc-icon-size:14px"></ha-icon>${m.label}</span></div>
          <div style="font-weight:600;margin-top:2px">${esc(s.id)}</div>
          <div class="sub">${esc(names(s))}${bits.length ? " · " + esc(bits.join(" · ")) : ""}</div>
          <div class="dayrow">${DAYS.map((d, i) => `<span class="${s.days.includes(i) ? "on" : ""}">${d.slice(0, 2)}</span>`).join("")}</div></div>
        <div><button class="btn sm" data-act="edit-schedule" data-id="${esc(s.id)}"><ha-icon icon="mdi:pencil" style="--mdc-icon-size:16px"></ha-icon>Edit</button></div></div>`;
    }).join("");
    return head + tl + `<div class="grid g-cards">${cards}</div>
      <p class="sub" style="color:var(--secondary-text-color);font-size:13px;margin-top:16px">When several schedules overlap, the higher priority wins. With no schedule active, a battery follows its own settings (or zero-import, if enabled in Settings).</p>`;
  },

  _scheduleModal(s, isNew) {
    const devs = this.data.devices;
    const maxP = Math.max(1000, ...devs.map((d) => Math.max(d.max_charge_w, d.max_discharge_w)));
    const sel = new Set(s.device_ids);
    const html = `<div class="overlay" data-close-bg><div class="modal" role="dialog" aria-label="Schedule">
      <h2>${isNew ? "Add schedule" : "Edit schedule"}</h2><p class="lead">What should your batteries do, and when?</p>
      <div class="f"><label>Name</label><input type="text" id="s-name" value="${esc(s.id)}" placeholder="e.g. Cheap-rate overnight charge"></div>
      <h3>Which batteries</h3>
      <div class="checks">${devs.map((d) => `<label><input type="checkbox" name="s-dev" value="${esc(d.device_id)}" ${sel.has(d.device_id) ? "checked" : ""}>${esc(d.name)}</label>`).join("")}</div>
      <div class="f"><span class="hint">Leave all unticked to apply to every battery.</span></div>
      <h3>What to do</h3>
      <div class="seg-pick">${Object.entries(MODES).map(([k, m]) => `<label style="--c:${m.color}"><input type="radio" name="s-mode" value="${k}" ${s.mode === k ? "checked" : ""}><ha-icon icon="${m.icon}" style="color:${U.INK[k] || m.color}"></ha-icon><span>${m.label}</span></label>`).join("")}</div>
      <div class="f"><span class="hint" id="s-modehelp"></span><span class="hint" id="s-unsup" style="color:var(--hp-err)"></span></div>
      <div id="s-power" class="f"><label>Power</label><div class="rng"><input type="range" id="s-pw" min="0" max="${maxP}" step="100" value="${s.power_w ?? Math.min(3000, maxP)}"><output id="s-pwo"></output></div></div>
      <div class="f" id="s-srcwrap"><label>Charge from</label><select id="s-source">${[["grid", "Grid: charge at the power above, whatever the sun is doing"], ["solar", "Solar only: use spare solar, never import to charge"], ["both", "Grid + solar: the power above, plus any spare solar on top"]].map(([v, l]) => `<option value="${v}" ${(s.source || "grid") === v ? "selected" : ""}>${l}</option>`).join("")}</select>
        <span class="hint" id="s-srchelp"></span></div>
      <div class="two"><div class="f" id="s-tgt"><label>Stop charging at SOC (%)</label><input type="number" id="s-target" min="0" max="100" value="${s.target_soc ?? ""}" placeholder="100"></div>
        <div class="f" id="s-res"><label>Don't discharge below SOC (%)</label><input type="number" id="s-reserve" min="0" max="100" value="${s.reserve_soc ?? ""}" placeholder="device minimum"></div></div>
      <h3>When</h3>
      <div class="two" id="s-times"><div class="f"><label>Start</label><input type="time" id="s-start" value="${s.start}"></div><div class="f"><label>End</label><input type="time" id="s-end" value="${s.end}"></div></div>
      <div class="f" id="s-overnight"><span class="hint">An end time earlier than the start runs overnight (e.g. 23:30 → 07:30).</span></div>
      <h3>Only while… (optional)</h3>
      <div class="f"><input type="text" id="s-cond" list="dl-cond" aria-label="Only while this entity is on (optional)" value="${esc(s.condition_entity || "")}" placeholder="binary_sensor.cheap_rate" autocomplete="off"><datalist id="dl-cond">${this._entityOptions(["binary_sensor", "input_boolean", "switch"])}</datalist>
        <span class="hint">Follow a tariff or any on/off entity (e.g. an Octopus or Agile cheap-rate sensor). The schedule only runs while it is on.</span></div>
      <label style="display:flex;gap:8px;align-items:center;margin:4px 0 12px"><input type="checkbox" id="s-allday" ${s.all_day ? "checked" : ""}>Ignore the times above; follow the entity only</label>
      <div class="daypick">${DAYS.map((d, i) => `<label><input type="checkbox" name="s-day" value="${i}" ${s.days.includes(i) ? "checked" : ""}><span>${d}</span></label>`).join("")}</div>
      <details ${s.season_start || s.on_date || s.priority ? "open" : ""}><summary>More options: season, one-off date, priority</summary>
        <div class="two"><div class="f"><label>Season starts (MM-DD)</label><input type="text" id="s-ss" value="${esc(s.season_start || "")}" placeholder="10-01" pattern="\\d\\d-\\d\\d"></div>
          <div class="f"><label>Season ends (MM-DD)</label><input type="text" id="s-se" value="${esc(s.season_end || "")}" placeholder="03-31" pattern="\\d\\d-\\d\\d"></div></div>
        <div class="two"><div class="f"><label>Only on this date</label><input type="date" id="s-date" value="${esc(s.on_date || "")}"></div>
          <div class="f"><label>Priority (higher wins)</label><input type="number" id="s-prio" value="${s.priority || 0}" min="-100" max="100"></div></div>
        <div class="two"><div class="f"><label>Applies when</label><select id="s-presence">${[["any", "Always"], ["home", "Home only"], ["away", "Away mode only"]].map(([v, l]) => `<option value="${v}" ${(s.presence || "any") === v ? "selected" : ""}>${l}</option>`).join("")}</select></div>
          <div class="f"><label>Share one total across batteries (W)</label><input type="number" id="s-total" min="0" step="100" value="${s.total_power_w ?? ""}" placeholder="off: each battery uses the power above"></div></div></details>
      <div class="err" id="m-err"></div>
      <div class="foot">${isNew ? "<span></span>" : `<button class="btn danger" data-act="del-schedule" data-id="${esc(s.id)}"><ha-icon icon="mdi:delete" style="--mdc-icon-size:16px"></ha-icon>Delete</button>`}
        <div class="r"><button class="btn" data-act="close">Cancel</button><button class="btn pri" data-act="save-schedule" data-orig="${isNew ? "" : esc(s.id)}">Save</button></div></div>
    </div></div>`;
    this._openModal(html);
    this._syncSchedForm();
  },

  _syncSchedForm() {
    const r = this.shadowRoot;
    const mode = (r.querySelector('input[name="s-mode"]:checked') || {}).value || "charge";
    const q = (id) => r.getElementById(id);
    const allDay = q("s-allday").checked;
    q("s-times").style.display = allDay ? "none" : "";
    q("s-overnight").style.display = allDay ? "none" : "";
    q("s-power").style.display = mode === "charge" || mode === "discharge" ? "" : "none";
    q("s-tgt").style.display = mode === "charge" ? "" : "none";
    q("s-srcwrap").style.display = mode === "charge" ? "" : "none";
    const src = mode === "charge" ? (q("s-source").value || "grid") : "grid";
    q("s-srchelp").textContent = src === "solar" ? "Follows your grid meter: charges with whatever would otherwise be exported (the power above is the most it will take) and waits when there is none. Needs a grid reading. Batteries whose charge power cannot be set (EcoFlow) are held so their own panels charge them."
      : src === "both" ? "Charges at the power above from the grid, and takes any extra solar that would otherwise be exported." : "";
    q("s-res").style.display = mode === "discharge" ? "" : "none";
    q("s-modehelp").textContent = MODES[mode].help;
    const picked = [...r.querySelectorAll('input[name="s-dev"]:checked')].map((x) => x.value);
    const targets = this.data.devices.filter((d) => !picked.length || picked.includes(d.device_id));
    const bad = targets.filter((d) => d.supported_modes && !d.supported_modes.includes(mode) && !(src === "solar" && d.supported_modes.includes("hold"))).map((d) => d.name);
    q("s-unsup").textContent = bad.length ? `${MODES[mode].label} isn't available on: ${bad.join(", ")}. Those batteries will be left to their own control.` : "";
    q("s-pwo").textContent = fmtW(+q("s-pw").value);
  },

  _collectSchedule() {
    const r = this.shadowRoot, v = (id) => r.getElementById(id).value;
    const mode = r.querySelector('input[name="s-mode"]:checked').value;
    const n = (id) => (v(id) === "" ? null : +v(id));
    return {
      id: v("s-name").trim(),
      device_ids: [...r.querySelectorAll('input[name="s-dev"]:checked')].map((x) => x.value),
      mode,
      power_w: mode === "charge" || mode === "discharge" ? +v("s-pw") : null,
      target_soc: mode === "charge" ? n("s-target") : null,
      reserve_soc: mode === "discharge" ? n("s-reserve") : null,
      start: v("s-start"), end: v("s-end"),
      days: [...r.querySelectorAll('input[name="s-day"]:checked')].map((x) => +x.value),
      season_start: v("s-ss").trim() || null, season_end: v("s-se").trim() || null,
      on_date: v("s-date") || null, priority: +v("s-prio") || 0, enabled: true,
      presence: v("s-presence") || "any", total_power_w: n("s-total"), source: mode === "charge" ? (v("s-source") || "grid") : "grid",
      all_day: r.getElementById("s-allday").checked, condition_entity: v("s-cond").trim() || null,
    };
  },

  _overrideModal(did) {
    const devs = this.data.devices;
    const html = `<div class="overlay" data-close-bg><div class="modal" role="dialog" aria-label="Manual override">
      <h2>Manual override</h2><p class="lead">Take control for a while. This beats any schedule, then HomePower goes back to normal.</p>
      <h3>Batteries</h3><div class="checks">${devs.map((d) => `<label><input type="checkbox" name="o-dev" value="${esc(d.device_id)}" ${d.device_id === did ? "checked" : ""}>${esc(d.name)}</label>`).join("")}</div>
      <h3>What to do</h3>
      <div class="seg-pick">${Object.entries(MODES).map(([k, m]) => `<label style="--c:${m.color}"><input type="radio" name="o-mode" value="${k}" ${k === "charge" ? "checked" : ""}><ha-icon icon="${m.icon}" style="color:${U.INK[k] || m.color}"></ha-icon><span>${m.label}</span></label>`).join("")}</div>
      <div class="f"><label>Power (W) — for charge / discharge</label><input type="number" id="o-pw" min="0" step="100" value="3000"></div>
      <div class="two"><div class="f"><label>Stop charging at SOC (%)</label><input type="number" id="o-soc" min="0" max="100" value="100"></div>
        <div class="f"><label>For how long</label><select id="o-min"><option value="30">30 minutes</option><option value="60" selected>1 hour</option><option value="120">2 hours</option><option value="240">4 hours</option><option value="720">12 hours</option></select></div></div>
      <div class="err" id="m-err"></div>
      <div class="foot"><span></span><div class="r"><button class="btn" data-act="close">Cancel</button><button class="btn pri" data-act="save-override">Start override</button></div></div>
    </div></div>`;
    this._openModal(html);
  },

  };
};
