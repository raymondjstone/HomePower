const els = {};
globalThis.HTMLElement = class { attachShadow(){ this.shadowRoot = { innerHTML:"", addEventListener(){}, getElementById:(i)=>(els[i] ||= {}), querySelector(){return null}, querySelectorAll(){return []} }; } };
globalThis.localStorage = { getItem:()=>null, setItem(){} };
let cls; globalThis.customElements = { define:(n,c)=>cls=c };
await import("../custom_components/homepower/frontend/homepower-panel.js");
const p = new cls();
const now = Date.now();
p.data = { devices:[{device_id:"a",name:"A"},{device_id:"b",name:"B"}], entity_ids:{devices:{a:{power:"sensor.a_p"}}}, settings:{} };
p._hist = { a: Array.from({length:200},(_,i)=>({t: now-(200-i)*5000, p: Math.sin(i/20)*2400})), b: Array.from({length:200},(_,i)=>({t: now-(200-i)*5000, p: -300})) };
p._hass = { callWS: async()=>({"sensor.a_p":[{start: now-3*3600e3, mean: 1500},{start: now-2*3600e3, mean: -800}]}) };
for (const m of [15,30,180]) { p._flowMinV = m; const h = p._flowChart();
  console.log(m, "svg:", h.includes("<svg"), "ylabels:", (h.match(/plot-y/g)||[]).length, "xlabels:", (h.match(/<span style="left:/g)||[]).length, "W label:", /\d+(\.\d+)? kW|\d+ W/.test(h)); }
p._flowMinV = 180; await p._backfillFlow(); const h = p._flowChart(); console.log("backfilled path pts:", (h.match(/L/g)||[]).length > 2, h.includes("older data comes"));
console.log("buttons:", p._durButtons().includes('data-m="1440"'), p._durLabel(180), p._durLabel(60));
const t = p._niceTicks(-2400, 2400); console.log(JSON.stringify(t));
console.log(p._lineChart([{name:"x",color:"#f00",pts:[{t:now-1e6,v:10},{t:now,v:90}]}], "%").includes("100%"));
