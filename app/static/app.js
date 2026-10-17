const $ = (s) => document.querySelector(s);
const screens = { cam: $("#s-cam"), scan: $("#s-scan"), list: $("#s-list"), sum: $("#s-sum"), mine: $("#s-mine") };
const LIST = "cuenta_list_v1";
const marksKey = (slug) => "cuenta_marks_" + slug;

// ---------- helpers ----------
const eur = (c) => (c / 100).toLocaleString("es-ES", { style: "currency", currency: "EUR" });
const cents = (x) => Math.round(Number(x) * 100);
const parseNum = (s) => Number(String(s).trim().replace(",", "."));
const fmtQty = (q) => (Number.isInteger(q) ? String(q) : String(q).replace(".", ","));
const lsGet = (k) => { try { return localStorage.getItem(k); } catch (e) { return null; } };
const lsSet = (k, v) => { try { localStorage.setItem(k, v); } catch (e) { /* private mode */ } };
let toastT = null;
function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toastT);
  toastT = setTimeout(() => (t.hidden = true), 3800);
}
let current = "cam";
// ---------- "install as app" invitation (mobile only, camera screen only) ----------
// Chrome rarely shows its own prompt any more, so catch the install event and offer it ourselves; iOS has no such
// event (manual steps shown); on Android without the event a hint to the browser menu appears after a moment.
const installBox = $("#install");
let installEvt = null, installWanted = false, installFallbackT = null;
const standalone = matchMedia("(display-mode: standalone)").matches || navigator.standalone;
const isIOS = /iPhone|iPad|iPod/i.test(navigator.userAgent);
const isAndroid = /Android/i.test(navigator.userAgent);
const installDismissed = () => lsGet("cuenta_install_dismissed") === "1";
function paintInstall() { installBox.hidden = !(installWanted && current === "cam"); }
function wantInstall(hint) {
  if (standalone || installDismissed()) return;
  if (hint) $("#install-hint").innerHTML = hint;
  installWanted = true;
  paintInstall();
}
window.addEventListener("beforeinstallprompt", (e) => {
  e.preventDefault();
  installEvt = e;
  clearTimeout(installFallbackT);
  $("#install-go").hidden = false;
  wantInstall("Ábrela desde tu pantalla de inicio, a pantalla completa.");
});
window.addEventListener("appinstalled", () => { installWanted = false; paintInstall(); lsSet("cuenta_install_dismissed", "1"); });
$("#install-x").onclick = () => { installWanted = false; paintInstall(); lsSet("cuenta_install_dismissed", "1"); };
$("#install-go").onclick = async () => {
  if (!installEvt) return;
  installEvt.prompt();
  await installEvt.userChoice;
  installEvt = null;
  installWanted = false;
  paintInstall();
};
if (isIOS) wantInstall('Toca <b>Compartir</b> y luego <b>Añadir a pantalla de inicio</b>.');
else if (isAndroid) installFallbackT = setTimeout(() => wantInstall('Menú <b>⋮</b> del navegador → <b>Instalar aplicación</b>.'), 4000);

function show(name) {
  current = name;
  paintInstall();
  for (const [k, el] of Object.entries(screens)) el.hidden = k !== name;
  if (name === "cam") startCamera(); else stopCamera();
}
// Per-device secret: it is how the server knows which person is calling (stored there only as a hash).
let _dev = null;
function deviceToken() {
  _dev = _dev || lsGet("cuenta_device");
  if (!_dev || _dev.length < 20) {
    const a = crypto.getRandomValues(new Uint8Array(24));
    _dev = btoa(String.fromCharCode(...a)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
    lsSet("cuenta_device", _dev);
  }
  return _dev;
}
async function api(path, opts = {}) {
  let r;
  if (path.startsWith("/api/")) opts = { ...opts, headers: { ...(opts.headers || {}), "X-Person-Token": deviceToken() } };
  try { r = await fetch(path, opts); }
  catch (e) { const err = new Error("Sin conexión"); err.offline = true; throw err; }
  if (!r.ok) {
    let msg = "No se pudo completar";
    try { msg = (await r.json()).detail || msg; } catch (e) { /* not json */ }
    const err = new Error(msg); err.status = r.status; throw err;
  }
  return r.json();
}
// The owner's secret (kept only on the device that scanned the ticket) travels with every change to the bill.
const ownerKey = (slug) => "cuenta_owner_" + slug;
const tokenOf = (slug) => lsGet(ownerKey(slug));
const postJson = (path, body) => {
  const m = path.match(/^\/api\/bills\/([^/]+)\//);
  const t = m && tokenOf(m[1]);
  return api(path, { method: "POST", headers: { "Content-Type": "application/json", ...(t ? { "X-Owner-Token": t } : {}) }, body: JSON.stringify(body) });
};

// ---------- my bills (this device only) ----------
// [{ slug, title, total, mine (cents), added (ms) }] newest first. The bills themselves live on the server and are never
// deleted there; removing one here only forgets it on this device (and the marks made here).
// Most recent ticket first (ticket date, then the moment it was added here).
function getList() {
  let l = [];
  try { l = JSON.parse(lsGet(LIST) || "[]"); if (!Array.isArray(l)) l = []; } catch (e) { l = []; }
  return l.sort((a, b) => (b.date || "").localeCompare(a.date || "") || (b.added || 0) - (a.added || 0));
}
const fmtDate = (iso) => {
  if (!iso) return "";
  const [y, m, d] = iso.split("-").map(Number);
  return new Date(y, m - 1, d).toLocaleDateString("es-ES", { day: "numeric", month: "short", year: "numeric" });
};
function putList(l) { lsSet(LIST, JSON.stringify(l)); }
function upsertEntry(patch) {
  const l = getList();
  const i = l.findIndex((e) => e.slug === patch.slug);
  if (i >= 0) l[i] = { ...l[i], ...patch };
  else l.unshift({ title: "", total: null, mine: 0, added: Date.now(), ...patch });
  putList(l);
}
function removeEntry(slug) {
  dropCopy(slug);
  putList(getList().filter((e) => e.slug !== slug));
  try { localStorage.removeItem(marksKey(slug)); } catch (e) { /* ignore */ }
}
function paintResume() {
  const n = getList().length;
  $("#resume").hidden = !n;
  $("#resume-n").textContent = n;
}

// ---------- offline copy ----------
// Every bill opened is kept on this device (lines in localStorage, photo in the service worker's cache), so it can be
// opened without connection. The photo never changes; the lines might have been corrected by someone else meanwhile,
// which can't be checked offline (banner says so).
const billKey = (slug) => "cuenta_bill_" + slug;
const photoKey = (b) => { const m = b && b.photo && b.photo.match(/[?&]v=([^&]+)/); return m ? "/photo/" + m[1] : null; };
function saveCopy(b) { lsSet(billKey(b.slug), JSON.stringify(b)); }
function loadCopy(slug) {
  try { return JSON.parse(lsGet(billKey(slug)) || "null"); } catch (e) { return null; }
}
function dropCopy(slug) {
  const b = loadCopy(slug);
  try { localStorage.removeItem(billKey(slug)); } catch (e) { /* ignore */ }
  const k = photoKey(b);
  if (k && window.caches) caches.open("cuenta-photos").then((c) => c.delete(k)).catch(() => {});
}
const warmed = new Set();
function warmPhoto(b) {   // fetch once while online: the service worker stores it for good
  if (!b.photo || warmed.has(b.photo)) return;
  warmed.add(b.photo);
  fetch(b.photo).catch(() => warmed.delete(b.photo));
}
let offline = false;
function setOffline(v) {
  offline = v;
  $("#offline").hidden = !v;
}

// ---------- bill state ----------
// bill = { slug, version, total, lines:[{id,qty,name,price,amount,ok,fixed,y,h}], photo,
//          people:[{id,name,color}], marks:{personId:{lineId:units}}, me }   (shared, lives on the server)
// marks = { lineId: units }  MY marks: kept on this device and sent to the server (all at once) so the others see them
let bill = null;
let marks = {};
function loadMarks() {
  try { marks = JSON.parse(lsGet(marksKey(bill.slug)) || "{}") || {}; } catch (e) { marks = {}; }
}
function saveMarks() { lsSet(marksKey(bill.slug), JSON.stringify(marks)); queueSync(); }
function setBill(b, { keepMarks = false } = {}) {
  const slugChanged = !bill || bill.slug !== b.slug;
  bill = b;
  saveCopy(b);
  warmPhoto(b);
  if (slugChanged && !keepMarks) loadMarks();
  let pruned = false;
  for (const id of Object.keys(marks)) { // drop marks of deleted lines, clamp to a changed quantity
    const l = bill.lines.find((x) => x.id === id);
    if (!l) { delete marks[id]; pruned = true; }
    else if (marks[id] > l.qty) { marks[id] = l.qty; pruned = true; }
  }
  if (pruned) saveMarks();
  $("#title").textContent = b.title || "Cuenta";
  $("#title").parentElement.classList.toggle("editable", !b.has_owner || !!tokenOf(b.slug));
  $("#subtitle").textContent = [fmtDate(b.date), b.slug].filter(Boolean).join(" · ");
  upsertEntry({ slug: b.slug, title: b.title || "", total: b.total, date: b.date || "" });
  if (current === "list") render();
  if (current === "sum") renderSummary();
}
const isOwner = () => !!(bill && tokenOf(bill.slug));
const canEdit = () => !!bill && (bill.allow_edits !== false || isOwner());
const units = (l) => marks[l.id] || 0;
const isMulti = (l) => Number.isInteger(l.qty) && l.qty > 1;
const needsCheck = (l) => !l.ok && !l.fixed;

// ---------- who took what ----------
// Colours of the others: pastel, assigned by the server per bill. Mine is always green on my own screen.
const PALETTE = ["#f28b82", "#f4a6c0", "#f8b48a", "#d3a6ee", "#a6c4f5", "#f2d27a", "#b7a8f0", "#f1a3d4", "#ffb8a6", "#c6a2e2"];
const colorOf = (p) => PALETTE[(p.color || 0) % PALETTE.length];
const ME = "me";
const others = () => (bill.people || []).filter((p) => p.id !== bill.me);
function holders(l) {    // everybody who marked part of this line: me (my local marks) first, then the others
  const out = [];
  const mine = units(l);
  if (mine > 0) out.push({ id: ME, units: Math.min(mine, l.qty) });
  for (const p of others()) {
    const u = ((bill.marks || {})[p.id] || {})[l.id];
    if (u > 0) out.push({ id: p.id, p, units: Math.min(u, l.qty) });
  }
  return out;
}
// Split one line among the people who marked it, in cents that always add up.
//  - taken <= quantity: each pays unit price x units, the rest is still unassigned ("free")
//  - taken  > quantity: over-assigned = shared; the whole line is divided in proportion to units
function allocate(l) {
  const hs = holders(l), A = cents(l.amount), Q = l.qty;
  const S = hs.reduce((s, h) => s + h.units, 0);
  const res = { holders: hs, units: S, over: S > Q + 1e-9, cents: {}, free: 0 };
  if (!hs.length) { res.free = A; return res; }
  if (!res.over) {
    let used = 0;
    for (const h of hs) { const c = Math.round((A * h.units) / Q); res.cents[h.id] = c; used += c; }
    if (Math.abs(S - Q) < 1e-9) { res.cents[hs[hs.length - 1].id] += A - used; used = A; }   // fully taken: exact cents
    res.free = Math.max(0, A - used);
  } else {
    let used = 0; const rem = [];
    for (const h of hs) { const exact = (A * h.units) / S, c = Math.floor(exact); res.cents[h.id] = c; used += c; rem.push([exact - c, h.id]); }
    rem.sort((a, b) => b[0] - a[0]);
    for (let i = 0; i < A - used; i++) res.cents[rem[i % rem.length][1]]++;
  }
  return res;
}
function overview() {
  const per = {}; let free = 0; const over = [], open = [];
  const total = bill.lines.reduce((s, l) => s + cents(l.amount), 0);
  for (const l of bill.lines) {
    const a = allocate(l);
    for (const [id, c] of Object.entries(a.cents)) per[id] = (per[id] || 0) + c;
    free += a.free;
    if (a.over) over.push([l, a]);
    if (a.free > 0) open.push([l, a]);
  }
  return { per, free, total, over, open };
}

// ---------- list ----------
const chip = (text, color) => {
  const s = document.createElement("span"); s.className = "chip"; s.textContent = text;
  if (color) s.style.setProperty("--c", color);
  return s;
};
function render() {
  const ul = $("#lines");
  ul.innerHTML = "";
  // Once others are in and something is already taken, a line nobody (fully) took must stand out among the taken ones
  const showOpen = (bill.people || []).length >= 2 && bill.lines.some((x) => allocate(x).holders.length > 0);
  for (const l of bill.lines) {
    const li = document.createElement("li");
    const u = units(l);
    const chk = needsCheck(l);
    const a = allocate(l);
    const others_ = a.holders.filter((h) => h.id !== ME);
    const open = showOpen && a.free > 0;
    li.className = "line" + (u ? " on" : "") + (chk ? " bad" : "") + (a.over ? " over" : "") + (open ? " open" : "") +
      (!u && others_.length && !a.over && a.free === 0 ? " taken" : "");
    if (!u && others_.length === 1 && !a.over) li.style.setProperty("--c", colorOf(others_[0].p));   // stripe in their colour
    const name = document.createElement("span"); name.className = "name"; name.textContent = l.name || "(sin nombre)";
    const meta = document.createElement("span"); meta.className = "meta";
    meta.textContent = l.qty === 1 ? "" : `${fmtQty(l.qty)} × ${eur(cents(l.amount / l.qty))}`;
    const amt = document.createElement("span"); amt.className = "amt"; amt.textContent = eur(cents(l.amount));
    const ed = document.createElement("button"); ed.className = "edit"; ed.setAttribute("aria-label", "Editar línea");
    ed.innerHTML = '<svg viewBox="0 0 24 24"><path d="M4 20h4L19 9l-4-4L4 16z"/><path d="M13.5 6.5l4 4"/></svg>';
    ed.onclick = (e) => { e.stopPropagation(); openEdit(l); };
    li.append(name, amt);
    if (canEdit()) li.append(ed);
    if (meta.textContent) li.append(meta);
    if (chk) {
      const row = document.createElement("div"); row.className = "chkrow";
      const tag = document.createElement("span"); tag.className = "tag"; tag.textContent = "⚠ Chequear";
      const see = document.createElement("button"); see.className = "btn small"; see.textContent = "Ver en la foto";
      see.onclick = (e) => { e.stopPropagation(); openPhoto(l); };
      const okb = document.createElement("button"); okb.className = "btn small"; okb.textContent = "Está bien";
      okb.onclick = (e) => { e.stopPropagation(); sendOps([{ op: "ok", id: l.id }]); };
      if (canEdit()) row.append(tag, see, okb); else row.append(tag, see);
      li.append(row);
    }
    if (others_.length || a.over || open) {  // who else took this line, and what is still missing
      const who = document.createElement("div"); who.className = "who";
      for (const h of others_) who.append(chip(h.p.name + (l.qty > 1 ? ` ×${fmtQty(h.units)}` : ""), colorOf(h.p)));
      if (a.over) { const c = chip("Compartida", null); c.classList.add("warnchip"); who.append(c); }
      if (open) {
        const left = +(l.qty - a.units).toFixed(2);
        const c = chip(a.holders.length ? `Faltan ${fmtQty(left)} de ${fmtQty(l.qty)}` : "Sin asignar", null);
        c.classList.add("openchip");
        who.append(c);
      }
      li.append(who);
    }
    if (isMulti(l) && l.qty <= 12 && (others_.length || u)) {   // one pip per unit: mine green, theirs in their colour
      const pips = document.createElement("div"); pips.className = "pips";
      const colors = [];
      for (const h of a.holders) for (let i = 0; i < h.units; i++) colors.push(h.id === ME ? "var(--accent)" : colorOf(h.p));
      for (let i = 0; i < l.qty; i++) {
        const p = document.createElement("i");
        if (colors[i]) { p.style.setProperty("--c", colors[i]); p.className = "full"; }
        pips.append(p);
      }
      li.append(pips);
    }
    if (isMulti(l) && u) {
      const st = document.createElement("div"); st.className = "step";
      const mk = (txt, fn, label) => { const b = document.createElement("button"); b.textContent = txt; b.setAttribute("aria-label", label);
        b.onclick = (e) => { e.stopPropagation(); fn(); }; return b; };
      const n = document.createElement("span"); n.className = "n"; n.textContent = `${u} / ${fmtQty(l.qty)}`;
      const part = document.createElement("span"); part.className = "part"; part.textContent = eur(a.cents[ME] || 0);
      const all = mk("Todo", () => setUnits(l, l.qty), "Marcar todas");
      all.classList.add("all");
      st.append(mk("−", () => setUnits(l, u - 1), "Una menos"), n, mk("+", () => setUnits(l, u + 1), "Una más"), all, part);
      li.append(st);
    }
    li.onclick = () => setUnits(l, u ? 0 : 1);
    ul.appendChild(li);
  }
  totals();
}
function setUnits(l, n) {
  n = Math.max(0, Math.min(l.qty, n));
  if (n) marks[l.id] = n; else delete marks[l.id];
  saveMarks();
  upsertEntry({ slug: bill.slug, done: false });
  render();
}
function totals() {
  $("#add").hidden = !canEdit();
  const ov = overview();
  const mine = ov.per[ME] || 0;
  $("#mine").textContent = eur(mine);
  upsertEntry({ slug: bill.slug, mine });
  const sum = ov.total;
  const tot = $("#tot"), warn = $("#warn");
  const nCheck = bill.lines.filter(needsCheck).length;
  const mismatch = bill.total != null && cents(bill.total) !== sum;
  if (bill.total == null) tot.textContent = `Suma de las líneas: ${eur(sum)}`;
  else if (!mismatch) tot.innerHTML = `Total del ticket ${eur(cents(bill.total))} <span class="good">✓ cuadra</span>`;
  else tot.innerHTML = `Ticket ${eur(cents(bill.total))} · líneas ${eur(sum)} <span class="bad">⚠</span>`;
  if (nCheck || mismatch) {
    warn.hidden = false;
    warn.innerHTML = "";
    const t = document.createElement("span");
    t.textContent = mismatch
      ? `Las líneas suman ${eur(sum)} pero el ticket dice ${eur(cents(bill.total))}.` + (nCheck ? ` ${nCheck} por chequear.` : " Compáralo con la foto.")
      : `${nCheck} línea${nCheck > 1 ? "s" : ""} por chequear con la foto.`;
    const b = document.createElement("button"); b.className = "btn small"; b.textContent = "Ver foto";
    b.onclick = () => openPhoto(bill.lines.find(needsCheck) || null);
    warn.append(t, b);
  } else warn.hidden = true;
  // state of the whole bill, in the sticky bar under "Lo tuyo" (only worth it once more than one person is in)
  const sbar = $("#statusbar"), many = (bill.people || []).length >= 2;
  sbar.hidden = !many;
  tot.hidden = many;
  if (many) {
    const done = ov.free === 0 && !ov.over.length;
    sbar.classList.toggle("done", done);
    $("#sb-main").textContent = ov.free > 0 ? `Sin asignar ${eur(ov.free)}` : (ov.over.length ? "Todo asignado" : "✓ Todo asignado");
    $("#sb-sub").textContent = `de ${eur(ov.total)}  ›`;
    const note = $("#sb-note");
    note.hidden = !ov.over.length;
    note.textContent = ov.over.length ? `${ov.over.length} línea${ov.over.length > 1 ? "s" : ""} compartida${ov.over.length > 1 ? "s" : ""} por varias personas` : "";
    $("#sb-fill").style.width = (ov.total ? Math.round(((ov.total - ov.free) / ov.total) * 100) : 0) + "%";
  }
}

// ---------- rename the bill (its label, not its link): owner only ----------
const tdlg = $("#tdlg");
$(".top .ttl").onclick = () => {
  if (!bill || !(isOwner() || !bill.has_owner)) return;
  $("#t-name").value = bill.title || "";
  $("#t-date").value = bill.date || "";
  $("#t-date").max = new Date(Date.now() + 86400000).toISOString().slice(0, 10);
  tdlg.returnValue = "";
  tdlg.showModal();
};
tdlg.addEventListener("close", async () => {
  if (tdlg.returnValue !== "ok") return;
  const title = $("#t-name").value.trim(), date = $("#t-date").value;
  const body = {};
  if (title !== (bill.title || "")) body.title = title;
  if (date && date !== bill.date) body.date = date;
  if (!Object.keys(body).length) return;
  try {
    setBill(await postJson(`/api/bills/${bill.slug}/meta`, body), { keepMarks: true });
    toast("Datos guardados");
  } catch (e) { toast(e.offline ? "Sin conexión: no se han podido guardar los cambios" : e.message); }
});

// ---------- who am I: name (kept on this device for good, editable) ----------
const myName = () => (lsGet("cuenta_name") || "").trim();
const pdlg = $("#pdlg");
let nameResolve = null;
function askName(cancelable) {          // resolves with the saved name, or null if cancelled
  return new Promise((resolve) => {
    if (nameResolve) nameResolve(null);
    $("#p-name").value = myName();
    $("#p-cancel").hidden = !cancelable;
    pdlg.dataset.cancelable = cancelable ? "1" : "";
    pdlg.returnValue = "";
    nameResolve = resolve;
    pdlg.showModal();
  });
}
pdlg.addEventListener("cancel", (e) => { if (!pdlg.dataset.cancelable) e.preventDefault(); });   // first time: a name is required
pdlg.addEventListener("close", () => {
  const name = $("#p-name").value.replace(/\s+/g, " ").trim().slice(0, 24);
  const ok = pdlg.returnValue === "ok" && !!name;
  if (ok) lsSet("cuenta_name", name);
  const r = nameResolve; nameResolve = null;
  if (r) r(ok ? name : null);
});

// ---------- joining a bill + sending my marks ----------
let joining = false, syncing = false, dirty = false, syncTimer = null;
async function ensureJoined() {
  if (!bill || joining) return;
  joining = true;
  try {
    const slug = bill.slug;
    let name = myName();
    if (!name) name = await askName(false);
    if (!name || !bill || bill.slug !== slug) return;
    const me = (bill.people || []).find((p) => p.id === bill.me);
    if (!me || me.name !== name) {
      try {
        const b = await postJson(`/api/bills/${slug}/join`, { name });
        if (bill && bill.slug === slug) setBill(b, { keepMarks: true });
      } catch (e) {
        if (e.offline) setOffline(true); else toast(e.message);
        return;
      }
    }
    queueSync(50);       // upload what this device already had marked (sending the same set twice changes nothing)
  } finally { joining = false; }
}
function queueSync(delay = 250) {
  dirty = true;
  clearTimeout(syncTimer);
  syncTimer = setTimeout(syncMarks, delay);
}
async function syncMarks() {
  if (!bill || !bill.me || !dirty) return;
  if (syncing) return queueSync(400);
  syncing = true; dirty = false;
  const slug = bill.slug, snapshot = { ...marks };
  try {
    const b = await postJson(`/api/bills/${slug}/marks`, { marks: snapshot });
    setOffline(false);
    if (bill && bill.slug === slug) setBill(b, { keepMarks: true });
  } catch (e) {
    if (e.offline) setOffline(true);
    if (e.status === 403 && bill && bill.slug === slug) { bill.me = null; ensureJoined(); }        // server forgot me: join again
    else if (e.offline || e.status >= 500 || e.status === 429) { dirty = true; syncTimer = setTimeout(syncMarks, 2500); }  // try again soon
  } finally { syncing = false; }
}

// ---------- summary: everybody's part, what is still unassigned, what is shared ----------
function openSummary() {
  history.pushState({ sum: 1 }, "");
  show("sum");
  renderSummary();
  $("#s-sum").scrollTop = 0;
}
function closeSummary(fromPop) {
  show("list");
  render();
  if (!fromPop && history.state && history.state.sum) history.back();
}
const sumHead = (text) => { const h = document.createElement("h3"); h.className = "sumh"; h.textContent = text; return h; };
const dot = (color) => { const d = document.createElement("i"); d.className = "dot"; d.style.setProperty("--c", color); return d; };
function renderSummary() {
  const ov = overview();
  const box = $("#sum-body");
  box.innerHTML = "";
  // progress
  const prog = document.createElement("div"); prog.className = "prog" + (ov.free === 0 && !ov.over.length ? " done" : "");
  const bar = document.createElement("div"); bar.className = "bar2";
  const fill = document.createElement("i"); fill.style.width = (ov.total ? Math.round(((ov.total - ov.free) / ov.total) * 100) : 0) + "%";
  bar.append(fill);
  const txt = document.createElement("div"); txt.className = "progtxt";
  txt.textContent = ov.free > 0 ? `Asignado ${eur(ov.total - ov.free)} de ${eur(ov.total)} · sin asignar ${eur(ov.free)}` : `Todo asignado · ${eur(ov.total)}`;
  prog.append(bar, txt);
  box.append(prog);
  // people (me first)
  box.append(sumHead("Personas"));
  const people = [...(bill.people || [])].sort((a, b) => (b.id === bill.me) - (a.id === bill.me));
  if (!people.length) {
    const p = document.createElement("p"); p.className = "empty"; p.textContent = "Todavía no hay nadie en la cuenta.";
    box.append(p);
  }
  for (const p of people) {
    const mine = p.id === bill.me, key = mine ? ME : p.id, total = ov.per[key] || 0;
    const li = document.createElement("div"); li.className = "person";
    const head = document.createElement("button"); head.className = "phead";
    const nm = document.createElement("span"); nm.className = "pname"; nm.textContent = mine ? `Tú (${p.name})` : p.name;
    const am = document.createElement("span"); am.className = "pamt"; am.textContent = eur(total);
    head.append(dot(mine ? "var(--accent)" : colorOf(p)), nm, am);
    const det = document.createElement("div"); det.className = "pdet"; det.hidden = true;
    let any = false;
    for (const l of bill.lines) {
      const a = allocate(l), h = a.holders.find((x) => x.id === key);
      if (!h) continue;
      any = true;
      const row = document.createElement("div"); row.className = "prow";
      const a1 = document.createElement("span"); a1.textContent = (l.qty > 1 ? `${fmtQty(h.units)} × ` : "") + (l.name || "(sin nombre)") + (a.over ? " (compartida)" : "");
      const a2 = document.createElement("span"); a2.textContent = eur(a.cents[key] || 0);
      row.append(a1, a2); det.append(row);
    }
    if (!any) { const e = document.createElement("div"); e.className = "prow muted"; e.textContent = "Todavía no ha marcado nada"; det.append(e); }
    if (mine) {
      const rn = document.createElement("button"); rn.className = "btn small ghost"; rn.textContent = "Cambiar mi nombre";
      rn.onclick = async () => { if (await askName(true)) { await ensureJoined(); renderSummary(); } };
      det.append(rn);
    } else if (isOwner() && any) {
      const cl = document.createElement("button"); cl.className = "btn small danger"; cl.textContent = `Quitar las marcas de ${p.name}`;
      cl.onclick = () => askConfirm(`¿Quitar las marcas de ${p.name}?`, "Se borra todo lo que ha marcado en esta cuenta. Puede volver a marcar.", "Quitar", async () => {
        try { setBill(await postJson(`/api/bills/${bill.slug}/people/${p.id}/clear`, {}), { keepMarks: true }); toast("Marcas quitadas"); }
        catch (e) { toast(e.offline ? "Sin conexión" : e.message); }
      });
      det.append(cl);
    }
    head.onclick = () => { det.hidden = !det.hidden; li.classList.toggle("open", !det.hidden); };
    li.append(head, det);
    box.append(li);
  }
  // unassigned + shared
  if (ov.open.length) {
    box.append(sumHead(`Sin asignar · ${eur(ov.free)}`));
    for (const [l, a] of ov.open) {
      const row = document.createElement("div"); row.className = "prow lone";
      const left = (l.qty > 1 && a.holders.length) ? ` (faltan ${fmtQty(+(l.qty - a.units).toFixed(2))} de ${fmtQty(l.qty)})` : "";
      const a1 = document.createElement("span"); a1.textContent = (l.name || "(sin nombre)") + left;
      const a2 = document.createElement("span"); a2.textContent = eur(a.free);
      row.append(a1, a2); box.append(row);
    }
  }
  if (ov.over.length) {
    box.append(sumHead("Compartidas (más de una persona la marcó)"));
    for (const [l, a] of ov.over) {
      const row = document.createElement("div"); row.className = "prow lone";
      const names = a.holders.map((h) => (h.id === ME ? "tú" : h.p.name)).join(", ");
      const a1 = document.createElement("span"); a1.textContent = `${l.name || "(sin nombre)"} — ${names}`;
      const a2 = document.createElement("span"); a2.textContent = eur(cents(l.amount));
      row.append(a1, a2); box.append(row);
    }
  }
}
$("#statusbar").onclick = () => openSummary();
$("#sum-close").onclick = () => closeSummary(false);

// generic confirmation dialog
const cdlg = $("#cdlg");
let confirmAction = null;
function askConfirm(title, text, okLabel, action) {
  $("#c-title").textContent = title; $("#c-text").textContent = text; $("#c-ok").textContent = okLabel;
  confirmAction = action; cdlg.returnValue = ""; cdlg.showModal();
}
cdlg.addEventListener("close", () => { const a = confirmAction; confirmAction = null; if (cdlg.returnValue === "ok" && a) a(); });

// ---------- finish: "Listo" -> summary of my part, then back to the list ----------
const fdlg = $("#fdlg");
$("#done").onclick = () => {
  const mine = bill.lines.filter((l) => units(l) > 0);
  const ov = overview();
  const total = ov.per[ME] || 0;
  const n = mine.reduce((s, l) => s + units(l), 0);
  $("#fin-amount").textContent = eur(total);
  $("#fin-label").textContent = "Tu parte";
  $("#fin-sub").textContent = n ? `${n} ${n === 1 ? "artículo" : "artículos"} de ${bill.title || "la cuenta"}` : "Todavía no has marcado nada";
  const ul = $("#fin-list"); ul.innerHTML = "";
  for (const l of mine) {
    const li = document.createElement("li");
    const a = document.createElement("span"); a.textContent = (units(l) > 1 || l.qty > 1 ? `${units(l)} × ` : "") + (l.name || "(sin nombre)");
    const b = document.createElement("span"); b.textContent = eur(allocate(l).cents[ME] || 0);
    li.append(a, b); ul.appendChild(li);
  }
  ul.hidden = !mine.length;
  const fw = $("#fin-warn"), notes = [];
  if ((bill.people || []).length > 1) {
    if (ov.free > 0) notes.push(`Aún quedan ${eur(ov.free)} sin asignar en la cuenta.`);
    if (ov.over.length) notes.push(`${ov.over.length} línea${ov.over.length > 1 ? "s" : ""} compartida${ov.over.length > 1 ? "s" : ""} por varias personas.`);
  }
  fw.textContent = notes.join(" ");
  fw.hidden = !notes.length;
  fdlg.returnValue = "";
  fdlg.showModal();
};
fdlg.addEventListener("close", () => {
  if (fdlg.returnValue !== "ok") return;     // "Seguir marcando" or Esc: stay on the bill
  upsertEntry({ slug: bill.slug, done: true });
  goMine();
});

// ---------- shared edits ----------
async function sendOps(ops) {
  if (!bill) return;
  try {
    setBill(await postJson(`/api/bills/${bill.slug}/ops`, { ops }), { keepMarks: true });
  } catch (e) {
    toast(e.offline ? "Sin conexión: la corrección no se ha podido guardar" : e.message);
    if (e.status === 403) { try { setBill(await api(`/api/bills/${bill.slug}`), { keepMarks: true }); } catch (x) { /* later */ } }
    if (e.offline) setOffline(true);
    if (e.status === 404) goMine();
  }
}

// ---------- edit dialog ----------
const dlg = $("#dlg");
let editing = null; // line or null (new)
function openEdit(l) {
  editing = l;
  $("#e-name").value = l ? l.name : "";
  $("#e-qty").value = l ? fmtQty(l.qty) : "1";
  $("#e-amount").value = l ? l.amount.toFixed(2).replace(".", ",") : "";
  $("#e-del").hidden = !l;
  dlg.returnValue = "";      // closed with Esc / Back it would otherwise keep the previous answer and repeat it
  dlg.showModal();
}
$("#add").onclick = () => openEdit(null);
$("#e-del").onclick = () => { const id = editing.id; dlg.close("del"); sendOps([{ op: "del", id }]); };
dlg.addEventListener("close", () => {
  if (dlg.returnValue !== "ok") return;
  const name = $("#e-name").value.trim();
  const qty = parseNum($("#e-qty").value), amount = parseNum($("#e-amount").value);
  if (!(qty > 0) || !isFinite(amount)) return toast("Cantidad o importe no válidos");
  sendOps([editing ? { op: "upd", id: editing.id, name, qty, amount } : { op: "add", name, qty, amount }]);
});

// ---------- photo viewer (own pinch-zoom: the app itself does not zoom) ----------
const photo = $("#photo"), pImg = $("#photo-img"), pWrap = $("#photo-wrap"), pBand = $("#photo-band"), pScroll = $("#photo-scroll");
const pReset = $("#photo-reset"), pHint = $("#photo-hint");
const pz = { s: 1, x: 0, y: 0 };
const MAXZ = 6;
function pzApply() {
  const W = pScroll.clientWidth, H = pScroll.clientHeight, w = W * pz.s, h = pImg.clientHeight * pz.s;
  pz.x = w <= W ? (W - w) / 2 : Math.min(0, Math.max(W - w, pz.x));
  pz.y = h <= H ? 0 : Math.min(0, Math.max(H - h, pz.y));
  pWrap.style.transform = `translate(${pz.x}px, ${pz.y}px) scale(${pz.s})`;
  pReset.hidden = pz.s <= 1.02;
}
function pzAt(cx, cy, ns) {            // zoom keeping the point (cx,cy) of the screen fixed
  ns = Math.min(MAXZ, Math.max(1, ns));
  pz.x = cx - ((cx - pz.x) * ns) / pz.s;
  pz.y = cy - ((cy - pz.y) * ns) / pz.s;
  pz.s = ns;
  pzApply();
}
function pzHome() { pBand.hidden = true; pz.s = 1; pz.x = 0; pz.y = 0; pzApply(); }
function showLine(line) {
  if (line && line.y != null) {        // highlight the row and bring it to the upper third; 1.3x keeps description AND amount of the row in view
    pBand.hidden = false;
    pBand.style.top = ((line.y - line.h / 2) * 100) + "%";
    pBand.style.height = (line.h * 100) + "%";
    const W = pScroll.clientWidth, H = pScroll.clientHeight;
    pz.s = 1.3;
    pz.x = W / 2 - 0.51 * W * pz.s;       // ticket rows run from ~14% to ~88% of the photo width: centre on 51%
    pz.y = H * 0.33 - pImg.clientHeight * line.y * pz.s;
    pzApply();
  } else pzHome();
}
function openPhoto(line) {
  const src = bill.photo;
  if (pImg.getAttribute("src") !== src) pImg.src = src;
  $("#photo-title").textContent = line ? `${line.name || "Línea"} · ${eur(cents(line.amount))}` : "Foto del ticket";
  photo.hidden = false;
  pHint.classList.remove("gone");
  setTimeout(() => pHint.classList.add("gone"), 3500);
  const go = () => showLine(line);
  if (pImg.complete && pImg.naturalWidth) go(); else pImg.onload = go;
  history.pushState({ photo: 1 }, "");
}
function closePhoto(fromPop) {
  if (photo.hidden) return;
  photo.hidden = true;
  if (!fromPop && history.state && history.state.photo) history.back();
}
$("#photo-close").onclick = () => closePhoto(false);
pReset.onclick = pzHome;
$("#b-photo").onclick = () => openPhoto(null);
{ // pointers: two fingers = pinch (+pan), one finger = pan when zoomed, double tap = zoom in / out, wheel = zoom
  const pts = new Map();
  let g = null, lastTap = 0, moved = 0;
  const rel = (e) => { const r = pScroll.getBoundingClientRect(); return { x: e.clientX - r.left, y: e.clientY - r.top }; };
  const startGesture = () => {
    const [p1, p2] = [...pts.values()];
    g = p2
      ? { d: Math.hypot(p1.x - p2.x, p1.y - p2.y) || 1, mid: { x: (p1.x + p2.x) / 2, y: (p1.y + p2.y) / 2 }, s: pz.s, x: pz.x, y: pz.y }
      : { mid: { x: p1.x, y: p1.y }, s: pz.s, x: pz.x, y: pz.y };
  };
  pScroll.addEventListener("pointerdown", (e) => {
    pScroll.setPointerCapture(e.pointerId);
    pts.set(e.pointerId, rel(e));
    moved = 0;
    startGesture();
  });
  pScroll.addEventListener("pointermove", (e) => {
    if (!pts.has(e.pointerId)) return;
    pts.set(e.pointerId, rel(e));
    const [p1, p2] = [...pts.values()];
    if (p2) {
      const d = Math.hypot(p1.x - p2.x, p1.y - p2.y), mid = { x: (p1.x + p2.x) / 2, y: (p1.y + p2.y) / 2 };
      const ns = Math.min(MAXZ, Math.max(1, g.s * d / g.d));
      pz.s = ns;
      pz.x = mid.x - ((g.mid.x - g.x) * ns) / g.s;   // the content point under the start midpoint follows the fingers
      pz.y = mid.y - ((g.mid.y - g.y) * ns) / g.s;
      moved = 99;
      pzApply();
    } else if (p1 && g) {
      moved = Math.max(moved, Math.hypot(p1.x - g.mid.x, p1.y - g.mid.y));
      if (pz.s > 1) { pz.x = g.x + (p1.x - g.mid.x); pz.y = g.y + (p1.y - g.mid.y); pzApply(); }
    }
  });
  const end = (e) => {
    const was = pts.get(e.pointerId);
    pts.delete(e.pointerId);
    if (pts.size) { startGesture(); return; }
    if (e.type === "pointerup" && was && moved < 8) {                    // a tap
      const now = Date.now();
      if (now - lastTap < 320) { lastTap = 0; if (pz.s > 1.05) pzHome(); else pzAt(was.x, was.y, 2.6); }
      else lastTap = now;
    }
    g = null;
  };
  pScroll.addEventListener("pointerup", end);
  pScroll.addEventListener("pointercancel", end);
  pScroll.addEventListener("wheel", (e) => { e.preventDefault(); const p = rel(e); pzAt(p.x, p.y, pz.s * (e.deltaY < 0 ? 1.2 : 1 / 1.2)); }, { passive: false });
  window.addEventListener("resize", () => { if (!photo.hidden) pzApply(); });
}

// ---------- share ----------
const sdlg = $("#sdlg");
const linkOf = (slug) => location.origin + "/b/" + slug;
function paintShare() {
  const owner = isOwner(), legacy = !bill.has_owner;
  $("#s-owner").hidden = !owner;
  $("#s-claim").hidden = !legacy;
  const on = bill.allow_edits !== false;
  $("#s-allow").setAttribute("aria-checked", String(on));
  $("#s-allow-hint").textContent = on
    ? "Cualquiera con el enlace puede corregir líneas e importes."
    : "Solo tú puedes corregir. Úsalo cuando ya hayas comprobado que todo es correcto.";
}
$("#b-share").onclick = () => {
  $("#s-link").value = linkOf(bill.slug);
  $("#s-share").hidden = !navigator.share;
  paintShare();
  sdlg.showModal();
};
$("#s-allow").onclick = async () => {
  const want = $("#s-allow").getAttribute("aria-checked") !== "true";
  try {
    setBill(await postJson(`/api/bills/${bill.slug}/settings`, { allow_edits: want }), { keepMarks: true });
    paintShare();
    toast(want ? "Correcciones permitidas" : "Correcciones bloqueadas para los demás");
  } catch (e) { toast(e.offline ? "Sin conexión" : e.message); }
};
$("#s-claim").onclick = async () => {
  try {
    const r = await postJson(`/api/bills/${bill.slug}/claim`, {});
    lsSet(ownerKey(bill.slug), r.owner_token);
    setBill(await api(`/api/bills/${bill.slug}`), { keepMarks: true });
    paintShare();
    toast("Ahora eres el dueño de esta cuenta");
  } catch (e) { toast(e.message); }
};
$("#s-close").onclick = () => sdlg.close();
$("#s-copy").onclick = async () => {
  const url = $("#s-link").value;
  try { await navigator.clipboard.writeText(url); toast("Enlace copiado"); }
  catch (e) { $("#s-link").select(); toast("Selecciona y copia el enlace"); }
};
$("#s-share").onclick = () => navigator.share({ title: "Cuenta", text: "Marca lo que es tuyo", url: $("#s-link").value }).catch(() => {});

// ---------- camera ----------
let stream = null;
const video = $("#cam");
let imageCapture = null;
async function startCamera() {
  paintResume();
  if (stream) return;
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) return noCamera();
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      video: { facingMode: { ideal: "environment" }, width: { ideal: 1920 }, height: { ideal: 1080 } }, audio: false,
    });
  } catch (e) { return noCamera(); }
  if (screens.cam.hidden) { stopCamera(); return; } // user left meanwhile
  video.srcObject = stream;
  screens.cam.classList.add("has-cam");
  $("#cam-fallback").hidden = true;
  try { imageCapture = "ImageCapture" in window ? new ImageCapture(stream.getVideoTracks()[0]) : null; } catch (e) { imageCapture = null; }
}
function noCamera() {
  screens.cam.classList.remove("has-cam");
  $("#cam-fallback").hidden = false;
}
function stopCamera() {
  if (stream) { stream.getTracks().forEach((t) => t.stop()); stream = null; }
  video.srcObject = null;
  imageCapture = null;
}
$("#shutter").onclick = async () => {
  let blob = null;
  if (imageCapture) { try { blob = await imageCapture.takePhoto(); } catch (e) { blob = null; } } // full-resolution still, autofocus
  if (!blob && video.videoWidth) {
    const c = document.createElement("canvas");
    c.width = video.videoWidth; c.height = video.videoHeight;
    c.getContext("2d").drawImage(video, 0, 0);
    blob = await new Promise((r) => c.toBlob(r, "image/jpeg", 0.92));
  }
  if (blob) scanBlob(blob);
};
for (const id of ["#in-gallery", "#in-shot"]) {
  $(id).addEventListener("change", (e) => {
    const f = e.target.files && e.target.files[0];
    e.target.value = "";
    if (f) scanBlob(f);
  });
}

// ---------- navigation ----------
function goCamera(push = true) {
  if (push && location.pathname !== "/") history.pushState(null, "", "/");
  show("cam");
}
async function openBill(slug, { replace = false } = {}) {
  try {
    const b = await api(`/api/bills/${encodeURIComponent(slug)}`);
    bill = null; // force marks reload for this slug
    setOffline(false);
    setBill(b);
    if (location.pathname !== "/b/" + b.slug) (replace ? history.replaceState : history.pushState).call(history, null, "", "/b/" + b.slug);
    show("list");
    render();
    $("#s-list").scrollTop = 0;
    ensureJoined();
    return true;
  } catch (e) {
    if (e.offline) {                       // no connection: open the copy kept on this device, if any
      const copy = loadCopy(slug);
      if (copy) {
        bill = null;
        setBill(copy);
        setOffline(true);
        if (location.pathname !== "/b/" + copy.slug) (replace ? history.replaceState : history.pushState).call(history, null, "", "/b/" + copy.slug);
        show("list");
        render();
        return true;
      }
      toast("Sin conexión, y esta cuenta aún no está guardada en este móvil");
      return false;
    }
    if (e.status === 404) {
      const wasListed = getList().some((x) => x.slug === slug);
      removeEntry(slug);
      showProblem("Esta cuenta no existe", wasListed ? "Ya no está en el servidor, así que la he quitado de tu lista. Si alguien te la compartió, pídele el enlace otra vez." : MISSING_TEXT);
    } else if (e.status === 429) {
      showProblem("Demasiados intentos", "Has probado varios enlaces que no existen. Espera unos minutos y vuelve a intentarlo.");
    } else toast(e.message);
    return false;
  }
}
// ---------- "this account does not exist" lightbox ----------
const ndlg = $("#ndlg");
function showProblem(title, text) {
  $("#n-title").textContent = title;
  $("#n-text").textContent = text;
  ndlg.returnValue = "";
  if (!ndlg.open) ndlg.showModal();
}
const MISSING_TEXT = "El enlace puede estar incompleto o mal copiado, o la cuenta ya no está en el servidor. Pídele a quien te lo mandó que lo vuelva a compartir desde la app.";
ndlg.addEventListener("close", () => { if (getList().length) goMine(); else goCamera(); });

// ---------- my bills screen + close / delete ----------
function goMine(push = true) {
  if (push && location.pathname !== "/cuentas") history.pushState(null, "", "/cuentas");
  paintName();
  renderMine();
  show("mine");
}
function renderMine() {
  const ul = $("#mine-list"), l = getList();
  ul.innerHTML = "";
  $("#mine-empty").hidden = l.length > 0;
  for (const e of l) {
    const li = document.createElement("li"); li.className = "entry";
    const name = document.createElement("span"); name.className = "name"; name.textContent = e.title || "Cuenta " + e.slug;
    const meta = document.createElement("span"); meta.className = "meta";
    meta.textContent = (fmtDate(e.date) || "Sin fecha") +
      (e.total != null ? " · ticket " + eur(cents(e.total)) : "") + (tokenOf(e.slug) ? " · tuya" : "");
    const mine = document.createElement("span"); mine.className = "mine" + (e.done ? " done" : ""); mine.textContent = e.mine ? "Lo tuyo: " + eur(e.mine) : "Nada marcado";
    const del = document.createElement("button"); del.className = "iconbtn"; del.setAttribute("aria-label", "Eliminar de mis cuentas");
    del.innerHTML = '<svg viewBox="0 0 24 24"><path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/></svg>';
    del.onclick = (ev) => { ev.stopPropagation(); askDelete(e.slug, e.title); };
    li.append(name, del, meta, mine);
    li.onclick = () => openBill(e.slug);
    ul.appendChild(li);
  }
}
const ddlg = $("#ddlg");
let delSlug = null, delFromBill = false;
function askDelete(slug, title, fromBill = false) {
  delSlug = slug; delFromBill = fromBill;
  $("#ddlg-text").textContent = `«${title || slug}» desaparece de tu lista y pierdes lo que marcaste en este móvil. La cuenta sigue guardada en el servidor: puedes volver a abrirla con su enlace.` +
    (tokenOf(slug) ? " Sigues siendo su dueño en este móvil." : "");
  ddlg.returnValue = "";
  ddlg.showModal();
}
ddlg.addEventListener("close", () => {
  if (ddlg.returnValue !== "del" || !delSlug) return;
  removeEntry(delSlug);
  if (delFromBill) { bill = null; marks = {}; goMine(); } else renderMine();
  toast("Cuenta eliminada de tu lista");
});
$("#b-del").onclick = () => askDelete(bill.slug, bill.title, true);
$("#b-close").onclick = () => goMine();
$("#m-close").onclick = () => goCamera();
const paintName = () => { $("#m-name").textContent = myName() ? "👤 " + myName() : "👤 Tu nombre"; };
$("#m-name").onclick = async () => { if (await askName(true)) paintName(); };
$("#m-new").onclick = () => goCamera();
$("#resume").onclick = () => goMine();
window.addEventListener("popstate", () => {
  if (!photo.hidden) return closePhoto(true);
  if (current === "sum") return closeSummary(true);
  const m = location.pathname.match(/^\/b\/([^/]+)/);
  if (location.pathname === "/cuentas") { renderMine(); show("mine"); return; }
  if (m) { if (!bill || bill.slug !== m[1]) openBill(m[1], { replace: true }); else show("list"), render(); }
  else show("cam");
});

// Other people may correct lines: pick it up without reloading (cheap poll, only the version travels)
setInterval(async () => {
  if ((current !== "list" && current !== "sum") || document.hidden || !bill || bill.gone) return;
  const meNow = (bill.people || []).find((p) => p.id === bill.me);
  if (myName() && !joining && (!meNow || meNow.name !== myName())) ensureJoined();   // not in yet, or renamed here
  if (dirty && !syncing && !syncTimer) queueSync(50);
  try {
    const r = await api(`/api/bills/${bill.slug}?since=${bill.version}`);
    setOffline(false);
    if (!r.unchanged) setBill(r, { keepMarks: true });
  } catch (e) {
    if (e.offline) setOffline(true);
    if (e.status === 404) { bill.gone = true; showProblem("Esta cuenta ya no existe", "Se ha borrado del servidor mientras la tenías abierta. Lo que marcaste sigue en tu móvil hasta que la quites de tu lista."); }
  }
}, 3000);
document.addEventListener("visibilitychange", async () => {
  if (document.hidden || (current !== "list" && current !== "sum") || !bill) return;
  try {
    const r = await api(`/api/bills/${bill.slug}?since=${bill.version}`);
    setOffline(false);
    if (!r.unchanged) setBill(r, { keepMarks: true });
  } catch (e) { if (e.offline) setOffline(true); }
});

// ---------- scan ----------
// Shrink big photos before uploading (phone photos are 4-8 MB; 1800 px on the long side is plenty for the OCR).
async function shrink(blob) {
  try {
    const bmp = await createImageBitmap(blob, { imageOrientation: "from-image" });
    const k = Math.min(1, 1800 / Math.max(bmp.width, bmp.height));
    const c = document.createElement("canvas");
    c.width = Math.round(bmp.width * k); c.height = Math.round(bmp.height * k);
    c.getContext("2d").drawImage(bmp, 0, 0, c.width, c.height);
    bmp.close && bmp.close();
    return await new Promise((r) => c.toBlob((b) => r(b || blob), "image/jpeg", 0.9));
  } catch (e) { return blob; }
}
let scanning = false;
async function scanBlob(blob) {
  if (scanning) return;
  scanning = true;
  const url = URL.createObjectURL(blob);
  $("#scan-img").src = url;
  show("scan");
  try {
    const small = await shrink(blob);
    const fd = new FormData();
    fd.append("photo", small, "ticket.jpg");
    const b = await api("/api/scan", { method: "POST", body: fd }); // the server stores the photo and creates the shared bill
    bill = null;
    if (b.owner_token) { lsSet(ownerKey(b.slug), b.owner_token); delete b.owner_token; }
    setBill(b);
    history.pushState(null, "", "/b/" + b.slug);
    show("list");
    render();
    $("#s-list").scrollTop = 0;
    ensureJoined();
  } catch (e) {
    toast(e.message || "Error de red");
    show("cam");
  } finally {
    scanning = false;
    URL.revokeObjectURL(url);
  }
}

// ---------- start ----------
async function sharedPhoto() {
  if (!new URLSearchParams(location.search).has("shared")) return null;
  history.replaceState(null, "", "/");
  try {
    const c = await caches.open("cuenta-shared");
    const r = await c.match("/shared-photo");
    if (!r) return null;
    const b = await r.blob();
    await c.delete("/shared-photo");
    return b;
  } catch (e) { return null; }
}
if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js?v=" + (new URL(import.meta.url).searchParams.get("v") || "")).catch(() => {});
// The app does not zoom (like a native app); only the photo viewer has its own pinch-zoom. iOS Safari ignores
// user-scalable=no, so its gesture events are cancelled (the photo viewer uses pointer events, unaffected).
["gesturestart", "gesturechange", "gestureend"].forEach((n) => document.addEventListener(n, (e) => e.preventDefault(), { passive: false }));
const m = location.pathname.match(/^\/b\/([^/]+)/);
const shared = await sharedPhoto();
if (shared) scanBlob(shared);
else if (m) { if (!(await openBill(m[1], { replace: true }))) { history.replaceState(null, "", "/"); show("cam"); } }
else show("cam");
