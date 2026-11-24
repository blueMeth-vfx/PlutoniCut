const $ = id => document.getElementById(id);
const esc = s => String(s).replace(/[&<>"]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));
let ST = null, inited = false, lastLog = null, fails = 0, lastLocal = 0;
const post = (p, d) => fetch(p, {method: "POST", body: JSON.stringify(d)}).catch(() => {});
const full = () => Object.assign({look: document.body.dataset.look, ontop: $("pin").classList.contains("on")}, collect());
function push(extra) { lastLocal = Date.now(); post("/api/set", Object.assign(full(), extra || {})); }
function cmd(c, arg, quiet) {
  if (ST && ST.busy && !quiet) return;
  lastLocal = Date.now();
  post("/api/cmd", {cmd: c, arg: arg, set: full()});
  if (ST && !quiet) { ST.busy = c; renderCommon(); render(); }
}
function setLook(l) {
  document.body.className = "look-" + l;
  document.body.dataset.look = l;
  for (const b of document.querySelectorAll(".looks button")) b.classList.toggle("on", b.dataset.look === l);
}
function favicon() {                          // logo Plutonic quadrato: icona della finestra e della barra
  const im = new Image();
  im.onload = () => {
    const c = document.createElement("canvas"), g = c.getContext("2d");
    c.width = c.height = 128;
    const s = Math.min(128 / im.width, 128 / im.height);
    g.drawImage(im, (128 - im.width * s) / 2, (128 - im.height * s) / 2, im.width * s, im.height * s);
    $("fav").href = c.toDataURL("image/png");
  };
  im.src = "/logo";
}
function renderCommon() {
  if (ST.log !== lastLog) { const L = $("log"); L.textContent = ST.log; L.scrollTop = 0; lastLog = ST.log; }
  $("status").textContent = ST.busy ? (BUSY[ST.busy] || "Lavoro…") : ST.status;
  document.body.classList.toggle("busy", !!ST.busy);
  for (const [c, id] of Object.entries(BTN)) {
    $(id).disabled = !!ST.busy && c !== ST.busy;
    $(id).classList.toggle("working", ST.busy === c);
  }
}
async function poll() {
  try {
    const r = await fetch("/api/state", {cache: "no-store"});
    ST = await r.json();
    fails = 0; $("off").hidden = true;
    if (!inited) { inited = true; setLook(ST.set.look || "studio"); $("pin").classList.toggle("on", ST.set.ontop !== false); init(); }
    renderCommon();
    render();
    if (ST.quit) { $("off").hidden = false; setTimeout(() => window.close(), 400); return; }
  } catch (e) { if (++fails > 3) $("off").hidden = false; }
  setTimeout(poll, ST && ST.busy ? 300 : 600);
}
for (const b of document.querySelectorAll(".looks button")) b.onclick = () => { setLook(b.dataset.look); push(); };
// Dentro PlutoniCut (la casa degli strumenti): niente intestazione propria, l'aspetto arriva dalla casa
if (new URLSearchParams(location.search).has("embed")) document.documentElement.classList.add("embed");
window.addEventListener("message", e => { if (e.data && e.data.look) setLook(e.data.look); });
$("pin").onclick = () => { $("pin").classList.toggle("on"); push(); };
function start() {
  for (const [c, id] of Object.entries(BTN)) if (!$(id).onclick) $(id).onclick = () => cmd(c);
  favicon();
  poll();
}

