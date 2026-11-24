// ---- pezzi d'interfaccia comuni agli strumenti che lavorano sulla timeline aperta
const fmtS = s => { s = Math.max(0, s); const m = Math.floor(s / 60), x = s - m * 60;
  return m + ":" + (x < 10 ? "0" : "") + x.toFixed(1).replace(".", ","); };
function seg2(id, opts, val, onPick) {        // pulsanti a scelta singola: opts = [[valore, titolo, sotto]]
  const el = $(id);
  el.innerHTML = opts.map(o => "<button data-v='" + o[0] + "'><b>" + esc(o[1]) + "</b>" +
    (o[2] ? "<small>" + esc(o[2]) + "</small>" : "") + "</button>").join("");
  for (const b of el.children) b.onclick = () => { pick2(id, b.dataset.v); if (onPick) onPick(b.dataset.v); push(); };
  pick2(id, val);
}
function pick2(id, v) { for (const b of $(id).children) b.classList.toggle("on", b.dataset.v === String(v)); }
const val2 = id => { const b = $(id).querySelector(".on"); return b ? b.dataset.v : ""; };
function chips(id, list, sel, single) {        // tracce da scegliere: list = [{i, name, n}]
  const el = $(id);
  el.innerHTML = list.length ? list.map(t => "<button class='tchip" + (sel.includes(t.i) ? " on" : "") + "' data-i='" + t.i + "' title='" +
    esc(t.name) + " · " + t.n + " clip'><b>" + esc(t.name) + "</b><small>" + t.n + " clip</small></button>").join("")
    : "<span class='hint' style='margin:0'>nessuna traccia</span>";
  for (const b of el.querySelectorAll(".tchip")) b.onclick = () => {
    if (single) for (const x of el.children) x.classList.toggle("on", x === b); else b.classList.toggle("on");
    push(); if (window.onChips) onChips(id);
  };
}
const chosen = id => [...$(id).querySelectorAll(".tchip.on")].map(b => +b.dataset.i);
function sectionUI() {                         // tutta la timeline / da-a con la playhead
  $("rAll").onclick = () => { $("rAll").classList.add("on"); $("rSel").classList.remove("on"); $("rBox").hidden = true; push(); };
  $("rSel").onclick = () => { $("rSel").classList.add("on"); $("rAll").classList.remove("on"); $("rBox").hidden = false; push(); };
  for (const f of ["from", "to"]) {
    $(f).addEventListener("change", () => push());
    $("ph_" + f).onclick = () => cmd("playhead", f, true);
  }
}
let seenPh = -1;
function sectionSync(s) {
  $("rAll").classList.toggle("on", !s.range); $("rSel").classList.toggle("on", !!s.range); $("rBox").hidden = !s.range;
  $("from").value = s.from || ""; $("to").value = s.to || "";
}
function playheadIn() {                        // il timecode della playhead e' arrivato
  const ph = ST.playhead;
  if (!ph || ph.seq === seenPh) return;
  if (seenPh >= 0 && ph.tc) { $(ph.field).value = ph.tc; push(); }
  seenPh = ph.seq;
}
const sectionGet = () => ({range: $("rSel").classList.contains("on"), from: $("from").value.trim(), to: $("to").value.trim()});
