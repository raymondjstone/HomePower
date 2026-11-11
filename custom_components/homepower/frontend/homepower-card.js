/* HomePower Lovelace card: all batteries as one. Add it with:  type: custom:homepower-card
   Optional:  entity: sensor.homepower_home_battery   (found automatically when omitted)
              title: Home battery   |   show_batteries: false */
const esc = (v) => String(v ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
const fmtW = (w) => (w == null ? "–" : Math.abs(w) >= 1000 ? `${(w / 1000).toFixed(2)} kW` : `${Math.round(w)} W`);
const hours = (h) => (h == null ? "–" : h < 1 ? `${Math.round(h * 60)} min` : h >= 48 ? `${Math.round(h / 24)} days` : `${h.toFixed(1)} h`);

class HomePowerCard extends HTMLElement {
  setConfig(config) {
    this._config = { show_batteries: true, ...config };
    if (!this.shadowRoot) this.attachShadow({ mode: "open" });
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  getCardSize() { return 4; }

  static getStubConfig() { return {}; }

  _entity() {
    const states = (this._hass && this._hass.states) || {};
    const id = this._config && this._config.entity;
    if (id) return states[id];
    return Object.values(states).find((s) => s.entity_id.startsWith("sensor.") && s.attributes && s.attributes.homepower === true && Array.isArray(s.attributes.batteries));
  }

  _ring(soc) {
    const r = 42, c = 2 * Math.PI * r, v = soc == null ? 0 : Math.max(0, Math.min(100, soc));
    const color = soc == null ? "var(--disabled-text-color)" : soc < 20 ? "#dc2626" : soc < 40 ? "#d97706" : "#16a34a";
    return `<svg width="104" height="104" viewBox="0 0 100 100" role="img" aria-label="Charge ${soc == null ? "unknown" : Math.round(soc) + " percent"}">
      <circle cx="50" cy="50" r="${r}" fill="none" stroke="var(--divider-color)" stroke-width="9"/>
      <circle cx="50" cy="50" r="${r}" fill="none" stroke="${color}" stroke-width="9" stroke-linecap="round" stroke-dasharray="${(c * v) / 100} ${c}" transform="rotate(-90 50 50)"/>
      <text x="50" y="57" text-anchor="middle" font-size="24" font-weight="700" fill="var(--primary-text-color)">${soc == null ? "–" : Math.round(soc) + "%"}</text></svg>`;
  }

  _render() {
    if (!this.shadowRoot || !this._config) return;
    const st = this._entity(), title = this._config.title || "Home battery";
    let body;
    if (!st) {
      body = `<div class="empty">No HomePower battery sensor found. Set <code>entity:</code> to the “Home battery” sensor.</div>`;
    } else {
      const a = st.attributes, flow = a.flow_w || 0, kind = Math.abs(flow) < 20 ? "Idle" : flow > 0 ? "Charging" : "Discharging";
      const n = (v, l) => `<div><b>${v}</b><span>${l}</span></div>`;
      const bats = this._config.show_batteries === false ? "" : `<div class="bats">${(a.batteries || []).map((b) => `<div class="bat${b.online ? "" : " off"}">
          <span>${esc(b.name)}</span><div class="bar"><i style="width:${b.soc == null ? 0 : Math.max(0, Math.min(100, b.soc))}%"></i></div>
          <span>${b.soc == null ? "–" : Math.round(b.soc) + "%"}${b.power_w == null ? "" : " · " + fmtW(b.power_w)}</span></div>`).join("")}</div>`;
      body = `<div class="top">${this._ring(a.soc)}<div class="nums">
          ${n(`${(a.stored_kwh ?? 0).toFixed(1)} kWh`, `stored of ${(a.capacity_kwh ?? 0).toFixed(1)}`)}
          ${n(`${kind} ${fmtW(Math.abs(flow))}`, "now")}
          ${a.runtime_h != null ? n(hours(a.runtime_h), "will last") : ""}
          ${a.full_in_h != null ? n(hours(a.full_in_h), "until full") : ""}</div></div>${bats}`;
    }
    this.shadowRoot.innerHTML = `<style>
      ha-card{padding:16px}h2{margin:0 0 12px;font-size:16px;font-weight:500;color:var(--primary-text-color)}
      .top{display:flex;gap:16px;align-items:center;flex-wrap:wrap}.nums{display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:8px 16px;flex:1;min-width:200px}
      .nums b{display:block;font-size:18px;color:var(--primary-text-color)}.nums span{font-size:12px;color:var(--secondary-text-color)}
      .bats{margin-top:14px;display:grid;gap:8px}.bat{display:grid;grid-template-columns:minmax(70px,1fr) 2fr auto;gap:8px;align-items:center;font-size:13px;color:var(--primary-text-color)}
      .bat.off{opacity:.5}.bar{height:8px;border-radius:4px;background:var(--divider-color);overflow:hidden}.bar i{display:block;height:100%;background:var(--primary-color)}
      .empty{color:var(--secondary-text-color)}code{background:var(--divider-color);padding:1px 4px;border-radius:4px}</style>
      <ha-card><h2>${esc(title)}</h2>${body}</ha-card>`;
  }
}

if (!customElements.get("homepower-card")) customElements.define("homepower-card", HomePowerCard);
window.customCards = window.customCards || [];
if (!window.customCards.some((c) => c.type === "homepower-card")) {
  window.customCards.push({ type: "homepower-card", name: "HomePower battery", description: "All your batteries as one: charge, flow, and how long it will last." });
}
