/* HomePower sidebar panel - dependency-free web component for Home Assistant. */
const MODES = {
  auto: { label: "Auto", color: "#3b82f6", icon: "mdi:auto-fix", help: "Device's own self-consumption logic" },
  charge: { label: "Charge", color: "#16a34a", icon: "mdi:battery-charging", help: "Force charge" },
  discharge: { label: "Discharge", color: "#f59e0b", icon: "mdi:battery-arrow-down", help: "Force discharge" },
  hold: { label: "Hold", color: "#6b7280", icon: "mdi:pause-circle", help: "Neither charge nor discharge" },
};
const SERIES = ["#4c8bf5", "#e8710a", "#9334e6", "#12a4af", "#d93f6b", "#7a8b1f"];
const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtW = (w) => (w == null ? "–" : Math.abs(w) >= 1000 ? `${(w / 1000).toFixed(2)} kW` : `${Math.round(w)} W`);
const fmtKwh = (wh) => `${(wh / 1000).toFixed(1)} kWh`;
const slug = (s) => s.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_|_$/g, "");

const CSS = `
:host{--hp-ok:#15803d;--hp-warn:#b45309;--hp-err:#b91c1c;--hp-auto:#1d4ed8;--hp-charge:#15803d;--hp-discharge:#b45309;--hp-hold:#4b5563}
:host([dark]){--hp-ok:#4ade80;--hp-warn:#fbbf24;--hp-err:#f87171;--hp-auto:#60a5fa;--hp-charge:#4ade80;--hp-discharge:#fbbf24;--hp-hold:#9ca3af}
:host{display:block;background:var(--primary-background-color);color:var(--primary-text-color);min-height:100vh;font-family:var(--paper-font-body1_-_font-family,Roboto,sans-serif)}
*{box-sizing:border-box}
header{display:flex;align-items:center;gap:12px;padding:12px 20px;background:var(--app-header-background-color,var(--primary-color));color:var(--app-header-text-color,#fff);position:sticky;top:0;z-index:5}
header h1{font-size:20px;font-weight:500;margin:0;flex:1;display:flex;align-items:center;gap:8px}
header ha-menu-button,header ha-icon-button{color:inherit}
nav{display:flex;gap:4px;padding:0 16px;background:var(--card-background-color);border-bottom:1px solid var(--divider-color);overflow-x:auto}
nav button{background:none;border:0;border-bottom:3px solid transparent;color:var(--secondary-text-color);padding:14px 16px;font:inherit;font-weight:500;cursor:pointer;display:flex;gap:6px;align-items:center;white-space:nowrap}
nav button.on{color:var(--primary-color);border-color:var(--primary-color)}
main{padding:16px;max-width:1200px;margin:0 auto}
.grid{display:grid;gap:16px}
.g-tiles{grid-template-columns:repeat(auto-fit,minmax(190px,1fr))}
.g-cards{grid-template-columns:repeat(auto-fit,minmax(310px,1fr))}
.card{background:var(--card-background-color);border-radius:14px;padding:16px;box-shadow:var(--ha-card-box-shadow,0 1px 3px rgba(0,0,0,.18));border:1px solid var(--divider-color)}
.card h2{font-size:15px;margin:0 0 12px;font-weight:600;display:flex;align-items:center;gap:8px}
.tile .lbl{font-size:12px;text-transform:uppercase;letter-spacing:.05em;color:var(--secondary-text-color)}
.tile .val{font-size:28px;font-weight:600;margin:4px 0}
.tile .sub{font-size:12px;color:var(--secondary-text-color)}
.g-over{grid-template-columns:repeat(auto-fit,minmax(135px,1fr));gap:10px}
.g-over .tile{text-align:center;padding:14px 8px}.g-over .tile>ha-icon{--mdc-icon-size:30px}
.g-over .tile .lbl{font-size:11px;margin-top:6px}.g-over .tile .val{font-size:22px}.g-over .tile .val small{font-size:13px;font-weight:500}
.strip{display:flex;gap:12px;align-items:center;margin:0 0 16px;padding:10px 14px;border-radius:12px;font-size:14px;border:1px solid var(--hp-auto);background:color-mix(in srgb,var(--hp-auto) 10%,var(--card-background-color))}
.strip>ha-icon{color:var(--hp-auto);--mdc-icon-size:28px;flex:none}
.bat{display:flex;gap:14px;align-items:center}
.bat .info{flex:1;min-width:0}
.bat .nm{font-weight:600;font-size:16px;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.pw{font-size:22px;font-weight:600;margin:2px 0}
.chips{display:flex;gap:6px;flex-wrap:wrap;margin-top:6px}
.chip{font-size:12px;padding:3px 9px;border-radius:99px;background:var(--secondary-background-color);color:var(--primary-text-color);display:inline-flex;gap:4px;align-items:center;border:1px solid var(--divider-color)}
.chip.m{color:#fff;border:0}
.chip.warn{background:#b45309;color:#fff;border:0}
.chip.bad{background:#b91c1c;color:#fff;border:0}
.dot{width:8px;height:8px;border-radius:50%;display:inline-block}
.ring text{fill:var(--primary-text-color)}
.spark{width:100%;height:46px;margin-top:10px}
.plot{display:flex;margin:8px 0 2px}
.plot-y{position:relative;width:58px;flex:none}
.plot-y span{position:absolute;right:6px;transform:translateY(-50%);font-size:11px;color:var(--secondary-text-color);white-space:nowrap}
.plot-main{flex:1;min-width:0}
.plot-main svg{display:block;width:100%;border-left:1px solid var(--divider-color);border-bottom:1px solid var(--divider-color)}
.plot-x{position:relative;height:18px;margin-top:4px}
.plot-x span{position:absolute;transform:translateX(-50%);font-size:11px;color:var(--secondary-text-color);white-space:nowrap}
.plot-x span:first-child{transform:none}.plot-x span:last-child{transform:translateX(-100%)}
.durs{display:inline-flex;gap:4px;flex-wrap:wrap;margin-left:12px;vertical-align:middle}
.hb{display:grid;grid-template-columns:auto auto 1fr;gap:22px;align-items:center}
.hb b{font-size:20px;font-weight:600}.hb span{font-size:12px;color:var(--secondary-text-color)}
.hb .held{display:grid;gap:8px}
.hb .nums{display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));row-gap:12px}
.hb .fig{display:flex;flex-direction:column;align-items:center;text-align:center;gap:2px;padding:0 8px;border-left:1px solid var(--divider-color)}
.hb .fig ha-icon{--mdc-icon-size:28px}
.banner{display:flex;gap:12px;align-items:center;flex-wrap:wrap;background:var(--card-background-color);border:1px solid var(--warning-color,#b45309);border-left-width:5px;border-radius:12px;padding:12px 16px;margin-bottom:16px}
.banner .t{flex:1;min-width:200px}
.scenes{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.presets{display:flex;gap:6px;flex-wrap:wrap;margin:4px 0 10px}
.ordlist{display:grid;gap:6px;margin:4px 0}.ord{display:grid;grid-template-columns:24px 1fr auto auto;gap:8px;align-items:center;padding:6px 8px;border:1px solid var(--divider-color);border-radius:8px}
.chk{display:grid;gap:6px}.chk .it{display:grid;grid-template-columns:24px 1fr auto;gap:8px;align-items:center;padding:6px 0;border-bottom:1px solid var(--divider-color)}
.chk .it:last-child{border-bottom:0}.chk .ok{color:var(--hp-ok)}.chk .no{color:var(--hp-warn)}.chk small{display:block;color:var(--secondary-text-color)}
@media(max-width:700px){header{flex-wrap:wrap;padding:10px 12px;gap:8px}header h1{flex-basis:100%;order:-1;font-size:18px}header .btn{padding:6px 10px;font-size:12px}
 main{padding:10px}.hb{grid-template-columns:1fr;justify-items:center;text-align:center}.hb .nums{align-self:stretch;justify-self:stretch}.plot-y{width:46px}.card{padding:14px}nav button{padding:12px 10px}.durs{margin-left:0;margin-top:6px;display:flex}}
.tl{margin:8px 0 14px}
.tl .row{display:grid;grid-template-columns:130px 1fr;gap:10px;align-items:center;margin:8px 0}
.tl .nm{font-size:13px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.bar{position:relative;height:26px;background:var(--secondary-background-color);border-radius:6px;overflow:hidden}
.bar.zi{background:repeating-linear-gradient(45deg,var(--secondary-background-color),var(--secondary-background-color) 6px,rgba(59,130,246,.18) 6px,rgba(59,130,246,.18) 12px)}
.seg{position:absolute;top:3px;bottom:3px;border-radius:4px;font-size:11px;color:#fff;padding:0 6px;display:flex;align-items:center;overflow:hidden;white-space:nowrap}
.now{position:absolute;top:0;bottom:0;width:2px;background:var(--primary-text-color);z-index:2}
.axis{display:grid;grid-template-columns:130px 1fr;gap:10px;font-size:11px;color:var(--secondary-text-color)}
.axis div:last-child{display:flex;justify-content:space-between}
.legend{display:flex;gap:14px;flex-wrap:wrap;font-size:12px;color:var(--secondary-text-color);margin-top:8px}
.legend span{display:inline-flex;gap:6px;align-items:center}
.legend button.lg{font:inherit;color:inherit;background:none;border:0;padding:0;cursor:pointer;display:inline-flex;gap:6px;align-items:center}
.legend button.lg.off{opacity:.45;text-decoration:line-through}
.sw{width:12px;height:12px;border-radius:3px;display:inline-block}
button.btn,a.btn{text-decoration:none;font:inherit;font-weight:500;border-radius:8px;border:1px solid var(--divider-color);background:var(--card-background-color);color:var(--primary-text-color);padding:8px 14px;cursor:pointer;display:inline-flex;gap:6px;align-items:center}
button.btn:hover,a.btn:hover{background:var(--secondary-background-color)}
button.btn.pri{background:var(--primary-color);color:var(--text-primary-color,#fff);border-color:transparent}
button.btn.danger{color:var(--hp-err)}
button.btn.sm{padding:4px 10px;font-size:13px}
.bar-actions{display:flex;justify-content:space-between;align-items:center;margin:0 0 14px;gap:10px;flex-wrap:wrap}
.bar-actions h2{margin:0;font-size:18px}
.empty{text-align:center;padding:40px 20px;color:var(--secondary-text-color)}
.empty ha-icon{--mdc-icon-size:56px;opacity:.5}
.empty h3{color:var(--primary-text-color);margin:10px 0 4px}
.sch{display:flex;gap:12px;align-items:center}
.sch .when{font-size:20px;font-weight:600}
.sch .meta{flex:1;min-width:0}
.sch .sub{font-size:13px;color:var(--secondary-text-color);margin-top:2px}
.sch.off{opacity:.55}
.dayrow{display:flex;gap:3px;margin-top:6px}
.dayrow span{font-size:10px;width:26px;text-align:center;padding:2px 0;border-radius:4px;background:var(--secondary-background-color);color:var(--secondary-text-color)}
.dayrow span.on{background:var(--primary-color);color:var(--text-primary-color,#fff)}
.log{font-family:monospace;font-size:12px;max-height:240px;overflow:auto}
.log div{padding:3px 0;border-bottom:1px solid var(--divider-color)}
.log .t{color:var(--secondary-text-color)}
.switch{position:relative;width:42px;height:24px;flex:none}
.switch input{opacity:0;width:0;height:0}
.switch i{position:absolute;inset:0;background:var(--disabled-text-color,#999);border-radius:99px;cursor:pointer;transition:.15s}
.switch i:before{content:"";position:absolute;width:18px;height:18px;left:3px;top:3px;background:#fff;border-radius:50%;transition:.15s}
.switch input:checked+i{background:var(--primary-color)}
.switch input:checked+i:before{transform:translateX(18px)}
.overlay{position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:20;display:flex;align-items:flex-start;justify-content:center;overflow:auto;padding:24px 12px}
.modal{background:var(--card-background-color);border-radius:16px;width:100%;max-width:640px;padding:22px;box-shadow:0 10px 40px rgba(0,0,0,.4)}
.modal h2{margin:0 0 4px;font-size:20px}
.modal .lead{color:var(--secondary-text-color);margin:0 0 16px;font-size:14px}
.modal h3{font-size:13px;text-transform:uppercase;letter-spacing:.05em;color:var(--secondary-text-color);margin:20px 0 8px}
.f{display:flex;flex-direction:column;gap:4px;margin:10px 0}
.f label{font-size:13px;color:var(--secondary-text-color)}
.f .hint{font-size:12px;color:var(--secondary-text-color)}
input[type=text],input[type=number],input[type=time],input[type=date],input[type=month],select{font:inherit;padding:9px 10px;border-radius:8px;border:1px solid var(--divider-color);background:var(--secondary-background-color);color:var(--primary-text-color);width:100%}
input:focus,select:focus{outline:2px solid var(--primary-color);outline-offset:-1px}
.two{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.three{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}
.seg-pick{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}
.seg-pick label{border:2px solid var(--divider-color);border-radius:10px;padding:10px 6px;text-align:center;cursor:pointer;font-size:13px;display:flex;flex-direction:column;align-items:center;gap:4px;color:var(--primary-text-color)}
.seg-pick input{display:none}
.seg-pick input:checked+span{font-weight:700}
.seg-pick label:has(input:checked){border-color:var(--c);background:color-mix(in srgb,var(--c) 18%,transparent)}
.daypick{display:flex;gap:6px;flex-wrap:wrap}
.daypick label{cursor:pointer}
.daypick input{display:none}
.daypick span{display:block;padding:8px 11px;border-radius:8px;border:1px solid var(--divider-color);font-size:13px}
.daypick input:checked+span{background:var(--primary-color);color:var(--text-primary-color,#fff);border-color:transparent}
.presets{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:8px}
.presets label{border:2px solid var(--divider-color);border-radius:10px;padding:10px;cursor:pointer;font-size:13px}
.presets input{display:none}
.presets label:has(input:checked){border-color:var(--primary-color);background:color-mix(in srgb,var(--primary-color) 14%,transparent)}
.checks{display:flex;gap:8px;flex-wrap:wrap}
.checks label{display:flex;gap:6px;align-items:center;border:1px solid var(--divider-color);border-radius:8px;padding:6px 10px;font-size:14px;color:var(--primary-text-color)}
.err{color:var(--hp-err);font-size:14px;margin-top:12px;min-height:18px}
.foot{display:flex;justify-content:space-between;gap:8px;margin-top:18px;flex-wrap:wrap}
.foot .r{display:flex;gap:8px;margin-left:auto}
details summary{cursor:pointer;color:var(--secondary-text-color);font-size:13px;margin:12px 0 0}
.rng{display:flex;gap:10px;align-items:center}.rng input[type=range]{flex:1}.rng output{min-width:70px;text-align:right;font-weight:600}
.tbl{width:100%;border-collapse:collapse;font-size:13px}
.tbl th,.tbl td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--divider-color)}
.tbl th{color:var(--secondary-text-color);font-weight:500}
.banner{border-radius:10px;padding:10px 14px;margin:0 0 14px;font-size:14px}
.banner.warn{background:#b45309;color:#fff}.banner.info{background:var(--secondary-background-color)}
.seg2{position:absolute;top:3px;bottom:3px;border-radius:4px;color:#fff;font-size:11px;padding:0 5px;display:flex;align-items:center;overflow:hidden;white-space:nowrap}
.rule{display:grid;grid-template-columns:2fr 1fr 1fr auto;gap:8px;align-items:end;margin:6px 0}
.steps div{padding:3px 0}.ok{color:var(--hp-ok)}.bad{color:var(--hp-err)}
@media(max-width:600px){.tl .row,.axis{grid-template-columns:80px 1fr}.two,.three{grid-template-columns:1fr}.seg-pick{grid-template-columns:repeat(2,1fr)}}
`;


// Meaning carried by text, icons or thin marks needs a shade per theme: no one colour reads at 4.5:1 on both a white and a
// near-black card. These follow Home Assistant's light / dark mode (the panel sets [dark] from hass.themes.darkMode).
const INK = { ok: "var(--hp-ok)", warn: "var(--hp-warn)", err: "var(--hp-err)", auto: "var(--hp-auto)",
  charge: "var(--hp-charge)", discharge: "var(--hp-discharge)", hold: "var(--hp-hold)" };
// Text on a coloured fill (mode chips, schedule bars): near-black or white, whichever has more contrast on that fill.
const onFill = (hex) => {
  const m = /^#?([0-9a-f]{6})$/i.exec(String(hex || ""));
  if (!m) return "#fff";
  const ch = (i) => { const c = parseInt(m[1].slice(i, i + 2), 16) / 255; return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4; };
  const L = 0.2126 * ch(0) + 0.7152 * ch(2) + 0.0722 * ch(4);
  return (L + 0.05) / (0.0056 + 0.05) > 1.05 / (L + 0.05) ? "#111" : "#fff";      // #111 has a luminance of 0.0056
};

export { MODES, SERIES, DAYS, esc, fmtW, fmtKwh, slug, CSS, INK, onFill };
