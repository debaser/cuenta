const $ = (s) => document.querySelector(s);
const eur = (x) => (x == null ? "—" : Number(x).toLocaleString("es-ES", { style: "currency", currency: "EUR" }));
const mb = (b) => (b < 1024 * 1024 ? `${Math.max(1, Math.round(b / 1024))} KB` : `${(b / 1048576).toFixed(1).replace(".", ",")} MB`);
const gb = (b) => `${(b / 1073741824).toFixed(0)} GB`;
let toastT = null;
function toast(msg) {
  const t = $("#toast");
  t.textContent = msg; t.hidden = false;
  clearTimeout(toastT); toastT = setTimeout(() => (t.hidden = true), 3500);
}
async function api(path, opts = {}) {
  let r;
  try { r = await fetch(path, { ...opts, headers: { ...(opts.headers || {}), "X-Admin": "1" } }); }
  catch (e) { throw Object.assign(new Error("Sin conexión"), { offline: true }); }
  if (r.status === 401 && path !== "/api/admin/login") { showLogin(); throw Object.assign(new Error("Sesión caducada"), { status: 401 }); }
  if (!r.ok) {
    let msg = "No se pudo completar";
    try { msg = (await r.json()).detail || msg; } catch (e) { /* not json */ }
    throw Object.assign(new Error(msg), { status: r.status });
  }
  return r.json();
}
const post = (path, body) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });

// ---------- views ----------
function showLogin(msg) {
  $("#a-main").hidden = true; $("#a-login").hidden = false;
  if (msg) { $("#login-err").textContent = msg; $("#login-err").hidden = false; }
  setTimeout(() => $("#a-pass").focus(), 50);
}
function showMain() { $("#a-login").hidden = true; $("#a-main").hidden = false; refresh(); }
$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("#login-err").hidden = true;
  try {
    await post("/api/admin/login", { password: $("#a-pass").value });
    $("#a-pass").value = "";
    showMain();
  } catch (err) {
    $("#login-err").textContent = err.message; $("#login-err").hidden = false;
    $("#a-pass").select();
  }
});
$("#a-logout").onclick = async () => { try { await post("/api/admin/logout"); } catch (e) { /* ignore */ } showLogin(); };
$("#a-refresh").onclick = () => refresh(true);

// ---------- data ----------
let bills = [];
const selected = new Set();
const ago = (t) => {
  const s = Math.max(0, Date.now() / 1000 - t);
  if (s < 90) return "ahora";
  if (s < 5400) return `hace ${Math.round(s / 60)} min`;
  if (s < 129600) return `hace ${Math.round(s / 3600)} h`;
  return `hace ${Math.round(s / 86400)} días`;
};
const fmtDay = (iso) => { if (!iso) return ""; const [y, m, d] = iso.split("-").map(Number); return new Date(y, m - 1, d).toLocaleDateString("es-ES", { day: "numeric", month: "short", year: "numeric" }); };

async function refresh(manual) {
  try {
    const [s, b] = await Promise.all([api("/api/admin/stats"), api("/api/admin/bills")]);
    bills = b;
    for (const slug of [...selected]) if (!bills.some((x) => x.slug === slug)) selected.delete(slug);
    renderStats(s);
    renderList();
    if (manual) toast("Actualizado");
  } catch (e) { if (e.status !== 401) toast(e.message); }
}
function renderStats(s) {
  const cards = [
    ["Cuentas", s.bills], ["Personas", s.people], ["Marcas", s.marks],
    ["Fotos", mb(s.photo_bytes)], ["Base de datos", mb(s.db_bytes)], ["Disco libre", `${gb(s.disk_free)} de ${gb(s.disk_total)}`],
    ["Tickets leídos, 24 h", `${s.scans_24h} / ${s.scan_cap}`],
  ];
  const box = $("#a-stats"); box.innerHTML = "";
  for (const [k, v] of cards) {
    const d = document.createElement("div"); d.className = "a-stat";
    const a = document.createElement("span"); a.textContent = k;
    const b = document.createElement("strong"); b.textContent = v;
    d.append(a, b); box.append(d);
  }
  const sw = $("#a-pause");
  sw.setAttribute("aria-checked", String(s.paused));
  $("#pause-hint").textContent = s.paused ? "Pausada: nadie puede subir tickets nuevos." : "Activa: se pueden subir tickets.";
  const ev = $("#a-events"); ev.innerHTML = "";
  $("#a-events-card").hidden = !s.events.length;
  for (const e of s.events) {
    const li = document.createElement("li");
    li.textContent = `${ago(e.t)} · ${e.kind} · ${e.ip}`;
    ev.append(li);
  }
}
function renderList() {
  const ul = $("#a-list"); ul.innerHTML = "";
  $("#a-empty").hidden = bills.length > 0;
  $("#list-count").textContent = bills.length ? `${bills.length} cuenta${bills.length > 1 ? "s" : ""}` : "";
  for (const b of bills) {
    const li = document.createElement("li"); li.className = "a-item";
    const cb = document.createElement("input"); cb.type = "checkbox"; cb.checked = selected.has(b.slug); cb.setAttribute("aria-label", "Seleccionar " + b.slug);
    cb.onchange = () => { cb.checked ? selected.add(b.slug) : selected.delete(b.slug); syncBar(); };
    const img = document.createElement("img"); img.className = "a-thumb"; img.loading = "lazy"; img.alt = ""; img.src = `/api/admin/bills/${b.slug}/thumb`;
    const info = document.createElement("div"); info.className = "a-info";
    const t = document.createElement("strong"); t.textContent = b.title || "(sin nombre)";
    const m1 = document.createElement("span"); m1.textContent = `${fmtDay(b.date)} · ${eur(b.total)} · ${b.lines} líneas`;
    const m2 = document.createElement("span"); m2.textContent = `${b.people} persona${b.people === 1 ? "" : "s"} · ${b.marks} marcas · ${mb(b.photo_bytes)}`;
    const m3 = document.createElement("span"); m3.textContent = `${b.slug} · actividad ${ago(b.updated)}${b.locked ? " · bloqueada" : ""}${b.has_owner ? "" : " · sin dueño"}`;
    info.append(t, m1, m2, m3);
    const acts = document.createElement("div"); acts.className = "a-acts";
    const open = document.createElement("a"); open.className = "btn small ghost"; open.href = `/b/${b.slug}`; open.target = "_blank"; open.rel = "noopener"; open.textContent = "Abrir";
    const del = document.createElement("button"); del.className = "btn small danger"; del.textContent = "Borrar";
    del.onclick = () => confirmDelete([b.slug], `«${b.title || b.slug}»`);
    acts.append(open, del);
    li.append(cb, img, info, acts);
    ul.append(li);
  }
  syncBar();
}
function syncBar() {
  $("#del-sel").disabled = !selected.size;
  $("#del-sel").textContent = selected.size ? `Borrar seleccionadas (${selected.size})` : "Borrar seleccionadas";
  $("#sel-all").checked = bills.length > 0 && selected.size === bills.length;
}
$("#sel-all").onchange = (e) => { selected.clear(); if (e.target.checked) bills.forEach((b) => selected.add(b.slug)); renderList(); };
$("#del-sel").onclick = () => confirmDelete([...selected], `${selected.size} cuenta${selected.size > 1 ? "s" : ""}`);

// ---------- actions ----------
const dlg = $("#a-dlg");
let pending = null;
function ask(title, text, action) {
  $("#a-dlg-title").textContent = title; $("#a-dlg-text").textContent = text;
  pending = action; dlg.returnValue = ""; dlg.showModal();
}
dlg.addEventListener("close", () => { const a = pending; pending = null; if (dlg.returnValue === "ok" && a) a(); });
function confirmDelete(slugs, what) {
  ask(`¿Borrar ${what}?`, "Se borran la foto, las personas y las marcas. Los enlaces dejarán de funcionar y no se puede deshacer.", async () => {
    try { const r = await post("/api/admin/delete", { slugs }); selected.clear(); toast(`${r.deleted} borrada${r.deleted === 1 ? "" : "s"}`); refresh(); }
    catch (e) { toast(e.message); }
  });
}
$("#a-pause").onclick = async () => {
  const want = $("#a-pause").getAttribute("aria-checked") !== "true";
  try { await post("/api/admin/pause", { paused: want }); toast(want ? "Lectura pausada" : "Lectura reactivada"); refresh(); }
  catch (e) { toast(e.message); }
};
let idleDays = 30;
$("#idle-check").onclick = async () => {
  idleDays = Number($("#idle-days").value);
  try {
    const r = await api(`/api/admin/idle?days=${idleDays}`);
    $("#idle-result").hidden = false;
    $("#idle-text").textContent = r.count ? `${r.count} cuenta${r.count > 1 ? "s" : ""} sin actividad desde hace más de ${idleDays} días` : `Ninguna cuenta lleva más de ${idleDays} días sin actividad`;
    $("#idle-del").hidden = !r.count;
  } catch (e) { toast(e.message); }
};
$("#idle-del").onclick = () => ask(`¿Borrar las cuentas sin actividad desde hace ${idleDays} días?`, "Se borran con su foto, personas y marcas. No se puede deshacer.", async () => {
  try { const r = await post("/api/admin/delete-idle", { days: idleDays }); toast(`${r.deleted} borrada${r.deleted === 1 ? "" : "s"}`); $("#idle-result").hidden = true; refresh(); }
  catch (e) { toast(e.message); }
});

// ---------- start ----------
(async () => {
  try {
    const me = await api("/api/admin/me");
    if (me.auth) showMain();
    else showLogin(me.configured ? "" : "El panel todavía no tiene contraseña. Créala con: docker exec -it cuenta python admin_tools.py set-password");
  } catch (e) { showLogin("No se pudo conectar"); }
})();
setInterval(() => { if (!document.hidden && !$("#a-main").hidden) refresh(); }, 30000);
