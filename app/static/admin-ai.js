// The paid reader's section of the admin page. All data comes from the private reader service through
// /api/admin/ai/*; the API key is typed here, forwarded, and never stored by this app.
const eur = (n, d = 2) => (n == null ? "—" : Number(n).toLocaleString("es-ES", { minimumFractionDigits: d, maximumFractionDigits: d }) + " €");
const when = (ts) => new Date(ts * 1000).toLocaleString("es-ES", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });

function h(tag, attrs, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (v === true) e.setAttribute(k, "");
    else if (v !== false && v != null) e.setAttribute(k, v);
  }
  for (const k of kids.flat()) if (k != null && k !== false) e.append(k.nodeType ? k : document.createTextNode(k));
  return e;
}
const sw = (on, label, fn, disabled = false) => {
  const b = h("button", { class: "switch", role: "switch", "aria-checked": String(on), "aria-label": label, type: "button", disabled }, h("span", { class: "track" }));
  b.onclick = async () => { const want = b.getAttribute("aria-checked") !== "true"; b.setAttribute("aria-checked", String(want)); try { await fn(want); } catch (e) { b.setAttribute("aria-checked", String(!want)); throw e; } };
  return b;
};
const num = (v, step) => h("input", { class: "ai-in", type: "number", step, min: "0", value: v });
const txt = (v, ph, type = "text") => h("input", { class: "ai-in", type, value: v || "", placeholder: ph || "", autocomplete: "off" });
const field = (label, input) => h("label", { class: "ai-f" }, h("span", {}, label), input);
const stat = (l, v) => h("div", { class: "a-stat" }, h("span", {}, l), h("strong", {}, String(v)));
// a fold-out block: the content sits in its own box so it gets real spacing (a <details> does not lay out as a flex column everywhere)
const fold = (cls, summary, ...body) => h("details", { class: cls }, h("summary", {}, ...summary), h("div", { class: "ai-body" }, ...body));

export async function mountAI(box, { post, api, toast, refreshAll, configured, eyeify }) {
  box.replaceChildren();
  if (!configured) {
    box.append(h("div", { class: "a-card" }, h("strong", {}, "Lector IA"), h("p", { class: "hint" }, "Crea primero la contraseña del panel (más abajo): hasta entonces el lector IA no se puede configurar.")));
    return;
  }
  let st;
  try { st = await api("/api/admin/ai/state"); } catch (e) { box.append(h("div", { class: "a-card" }, h("strong", {}, "Lector IA"), h("p", { class: "a-err" }, e.message))); return; }
  const say = (el, t, ok) => { el.textContent = t || ""; el.className = "ai-msg" + (t ? (ok ? " ok" : " err") : ""); };
  const run = async (btn, msg, fn, okText) => { btn.disabled = true; say(msg, ""); try { await fn(); if (okText) say(msg, okText, true); } catch (e) { say(msg, e.message, false); } finally { btn.disabled = false; } };
  const reload = () => mountAI(box, { post, api, toast, refreshAll, configured, eyeify });

  // status + master switch
  // The key comes first: without it there is no AI service, so the switch is off and locked until one is saved.
  const keyless = !st.has_key && !st.mock;
  let cls = "ok", t = "Funcionando: se usará la IA cuando la lectura local no cuadre";
  if (keyless) { cls = "off"; t = "Falta la clave de la API: Cuenta solo usa su lectura local"; }
  else if (!st.enabled) { cls = "off"; t = "Apagado: Cuenta solo usa su lectura local"; }
  else if (!st.working) { cls = "bad"; t = "Saldo agotado: Cuenta usa su lectura local"; }
  else if (!st.paid) t = "Funcionando con el plan gratuito de Google: no se cobra nada";
  else if (st.mode === "always") t = "Funcionando: la IA lee todos los tickets";
  const usable = Math.max(0.0001, st.credit_eur - st.margin_eur);
  const pct = Math.min(100, Math.max(0, (st.usable_eur / usable) * 100));
  const real = h("input", { class: "ai-in", type: "number", step: "0.01", min: "0", placeholder: "Saldo que ves en Google, en €" });
  const calMsg = h("div", { class: "ai-msg" });
  const calBtn = h("button", { class: "btn small", type: "button" }, "Corregir");
  calBtn.onclick = () => run(calBtn, calMsg, async () => { if (real.value === "") throw new Error("Escribe el saldo que ves en Google"); await post("/api/admin/ai/calibrate", { real_balance: parseFloat(real.value) }); reload(); });
  const svcHint = keyless ? "Necesita una clave de la API: añádela arriba." : (st.enabled ? "Si lo apagas, no se envía ninguna foto a Google." : "No se envía ninguna foto a Google.");
  const svcRow = h("div", { class: "a-row" }, h("div", { class: "a-grow" }, h("strong", {}, "Servicio de IA"), h("span", { class: "hint" }, svcHint)),
    sw(st.enabled && !keyless, "Servicio de IA", async (v) => { await post("/api/admin/ai/settings", { enabled: v }); reload(); }, keyless));
  const main = h("div", { class: "a-card" },
    h("strong", {}, "Lector IA"),
    h("div", { class: "ai-banner " + cls }, h("i"), t),
    svcRow,
    keyless ? null : [st.paid ? h("div", { class: "ai-big" }, eur(Math.max(0, st.remaining_eur)), h("small", {}, " saldo estimado")) : h("p", { class: "hint" }, "Plan gratuito: Google no cobra, pero limita las lecturas por minuto y puede usar las fotos para mejorar sus productos. Cuando hagas el prepago, activa «Plan de pago» en Ajustes y el panel llevará el saldo."),
    st.paid ? h("div", { class: "ai-bar" + (pct < 20 ? " low" : "") }, h("i", { style: `width:${pct}%` })) : null,
    st.paid ? h("span", { class: "hint" }, `Se para al llegar a ${eur(st.margin_eur)}` + (st.est_left != null ? ` · unas ${st.est_left.toLocaleString("es-ES")} lecturas más` : "")) : null,
    h("div", { class: "a-stats" }, st.paid ? [stat("Cargado", eur(st.credit_eur)), stat("Gastado", eur(st.spent_eur, 4))] : null, stat("Lecturas con IA", st.calls_total),
      stat("Hoy", `${st.calls_today} / ${st.max_calls_day}`), stat(st.paid ? "Coste medio" : "Valdría (si pagases)", st.avg_cost ? eur(st.avg_cost, 5) : "—"), stat("Errores", st.errors)),
    !st.paid ? null : fold("", ["Corregir el saldo con el de Google"],
      h("p", { class: "hint" }, "Google no deja consultar el saldo real con la clave: lo de arriba es una estimación según los tokens usados. Si quieres afinarla, mira tu saldo en Google y escríbelo aquí."),
      h("div", { class: "a-row" }, real, calBtn), calMsg)]);

  // API key
  // the saved key: first and last characters visible, the rest as dots; the eye shows it all, the button copies it
  let savedEl = null, delEl = null;
  if (st.has_key) {
    let full = null, shown = false;
    const mask = (st.key_hint || "").replace("…", "•".repeat(10));
    const getKey = async () => full || (full = (await api("/api/admin/ai/reveal-key")).api_key);
    const box = h("input", { class: "ai-in ai-saved", type: "text", readonly: true, value: mask, "aria-label": "Clave guardada", spellcheck: "false" });
    const eye = h("button", { type: "button", class: "pw-eye", title: "Mostrar", "aria-label": "Mostrar la clave" });
    const EYE = '<svg viewBox="0 0 24 24"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/></svg>';
    const EYE_OFF = '<svg viewBox="0 0 24 24"><path d="M3 3l18 18M10.6 5.1A10 10 0 0112 5c6.5 0 10 7 10 7a17 17 0 01-3.2 4M6.5 6.6A17 17 0 002 12s3.5 7 10 7a9.7 9.7 0 004.3-1"/><path d="M9.9 9.9a3 3 0 004.2 4.2"/></svg>';
    eye.innerHTML = EYE;
    const savedMsg = h("div", { class: "ai-msg" });
    eye.onclick = async () => {
      try {
        shown = !shown;
        box.value = shown ? await getKey() : mask;
        eye.innerHTML = shown ? EYE_OFF : EYE; eye.title = shown ? "Ocultar" : "Mostrar";
        say(savedMsg, "");
      } catch (e) { shown = false; say(savedMsg, e.message, false); }
    };
    const copy = h("button", { class: "btn small", type: "button" }, "Copiar");
    copy.onclick = () => run(copy, savedMsg, async () => {
      const k = await getKey();
      try { await navigator.clipboard.writeText(k); }
      catch (e) { shown = true; box.value = k; eye.innerHTML = EYE_OFF; box.focus(); box.select(); throw new Error("No he podido copiarla sola: la he mostrado y seleccionada, cópiala a mano"); }
    }, "Clave copiada");
    const delKey = h("button", { class: "btn small danger ai-delkey", type: "button" }, "Quitar la clave");
    delKey.onclick = () => { if (confirm("¿Quitar la clave? Cuenta volverá a la lectura local.")) run(delKey, savedMsg, async () => { await post("/api/admin/ai/key", { api_key: "" }); reload(); }); };
    savedEl = h("div", { class: "ai-savedkey" }, h("span", { class: "ai-f" }, h("span", {}, "Clave guardada")),
      h("div", { class: "a-row" }, h("span", { class: "pw-wrap ai-grow" }, box, eye), copy), savedMsg);
    delEl = delKey;
  }
  const key = txt("", st.has_key ? "Pega una clave nueva" : "Pega aquí la clave", "password");
  const keyEl = eyeify(key);
  const keyMsg = h("div", { class: "ai-msg" });
  const saveKey = h("button", { class: "btn small primary", type: "button" }, "Guardar y comprobar");
  saveKey.onclick = () => run(saveKey, keyMsg, async () => { if (!key.value.trim()) throw new Error("Pega la clave primero"); await post("/api/admin/ai/key", { api_key: key.value }); key.value = ""; reload(); });
  const testKey = h("button", { class: "btn small", type: "button", disabled: !st.has_key && !st.mock }, "Probar la guardada");
  testKey.onclick = () => run(testKey, keyMsg, () => post("/api/admin/ai/test-key", {}), "La clave funciona");
  const keyCard = fold("", ["Clave de la API", st.has_key ? h("small", {}, ` · ${st.key_hint}`) : h("small", {}, " · sin clave")],
    h("p", { class: "hint" }, "Es necesaria para el servicio de IA. Créala en ", h("a", { href: "https://aistudio.google.com/apikey", target: "_blank", rel: "noopener" }, "Google AI Studio"), "."),
    savedEl, delEl, h("label", { class: "ai-f" }, h("span", {}, st.has_key ? "Cambiarla por otra" : "Clave nueva"), keyEl), h("div", { class: "a-row" }, saveKey, testKey), keyMsg);

  // e-mail
  const mailTo = txt(st.mail_to, "tu@correo.com", "email");
  const mailMsg = h("div", { class: "ai-msg" });
  const mailSave = h("button", { class: "btn small primary", type: "button" }, "Guardar");
  mailSave.onclick = () => run(mailSave, mailMsg, () => post("/api/admin/ai/settings", { mail_to: mailTo.value.trim() }), "Guardado");
  const mailTest = h("button", { class: "btn small", type: "button" }, "Enviar prueba");
  mailTest.onclick = () => run(mailTest, mailMsg, async () => { await post("/api/admin/ai/settings", { mail_to: mailTo.value.trim() }); await post("/api/admin/ai/test-mail", {}); }, "Enviado, mira tu bandeja");
  const mailCard = fold("a-card", ["Avisos por correo", h("small", {}, st.mail_on ? " · activados" : " · desactivados")],
    h("div", { class: "a-row" }, h("div", { class: "a-grow" }, h("strong", {}, "Avisar cada vez que se use la IA"), h("span", { class: "hint" }, "Un correo por lectura (máximo 30 por hora). También avisa si se agota el saldo.")),
      sw(st.mail_on, "Avisar por correo", (v) => post("/api/admin/ai/settings", { mail_on: v }))),
    field("Correo de aviso", mailTo), h("div", { class: "a-row" }, mailSave, mailTest), mailMsg);

  // settings
  const f = { mode: h("select", { class: "ai-in" }, h("option", { value: "fallback" }, "Solo si la lectura local no cuadra"), h("option", { value: "always" }, "Siempre")),
    credit: num(st.credit_eur, "0.01"), margin: num(st.margin_eur, "0.01"), cap: num(st.max_calls_day, "1"), model: txt(st.model), pin: num(st.price_in, "0.001"), pout: num(st.price_out, "0.001"), fx: num(st.eur_per_usd, "0.001"), site: txt(st.site_url) };
  f.mode.value = st.mode;
  const setMsg = h("div", { class: "ai-msg" });
  const setSave = h("button", { class: "btn small primary", type: "button" }, "Guardar ajustes");
  setSave.onclick = () => run(setSave, setMsg, async () => {
    await post("/api/admin/ai/settings", { mode: f.mode.value, credit_eur: parseFloat(f.credit.value), margin_eur: parseFloat(f.margin.value), max_calls_day: parseInt(f.cap.value, 10),
      model: f.model.value.trim(), price_in: parseFloat(f.pin.value), price_out: parseFloat(f.pout.value), eur_per_usd: parseFloat(f.fx.value), site_url: f.site.value.trim() });
    reload();
  });
  const setCard = fold("a-card", ["Ajustes"],
    h("div", { class: "a-row" }, h("div", { class: "a-grow" }, h("strong", {}, "Plan de pago"), h("span", { class: "hint" }, "Actívalo solo cuando tu clave tenga saldo prepagado en Google. Apagado = plan gratuito: no se lleva saldo ni se para por dinero.")),
      sw(st.paid, "Plan de pago", async (v) => { await post("/api/admin/ai/settings", { paid: v }); reload(); })),
    field("Cuándo usar la IA", f.mode), h("div", { class: "ai-grid" }, field("Saldo cargado (€)", f.credit), field("Parar con este saldo (€)", f.margin), field("Tope de lecturas al día", f.cap)),
    fold("", ["Avanzado"], field("Modelo", f.model),
      h("div", { class: "ai-grid" }, field("Entrada ($ / millón de tokens)", f.pin), field("Salida ($ / millón)", f.pout), field("Euros por dólar", f.fx)),
      field("Dirección pública de Cuenta", f.site)),
    h("div", { class: "a-row" }, setSave), setMsg);

  // history of paid readings
  const hist = h("div", { class: "ai-hist" }, h("p", { class: "hint" }, "Se carga al abrir."));
  const histCard = fold("a-card", ["Historial de lecturas con IA", h("small", {}, ` · ${st.calls_total}`)], hist);
  histCard.addEventListener("toggle", async () => {
    if (!histCard.open) return;
    try {
      const d = await api("/api/admin/ai/calls?limit=100");
      hist.replaceChildren(d.rows.length ? h("ul", { class: "ai-ul" }, d.rows.map((r) => h("li", {},
        h("span", {}, when(r.ts)), r.slug ? h("a", { href: "/b/" + encodeURIComponent(r.slug), target: "_blank", rel: "noopener" }, r.slug) : h("span", {}, "—"),
        h("span", {}, r.error ? "Error " + r.error : `${r.nlines} líneas · ${r.match ? "cuadra" : "no cuadra"}`), h("strong", {}, eur(r.cost, 5))))) : h("p", { class: "hint" }, "Aún no se ha usado la IA."));
    } catch (e) { hist.replaceChildren(h("p", { class: "a-err" }, e.message)); }
  });

  keyCard.open = !st.has_key && !st.mock;
  main.insertBefore(keyCard, svcRow);   // the key belongs to the AI service and goes first: without it there is nothing to switch on
  box.append(main, mailCard, setCard, histCard);
}

/** slug -> cost of the paid reading, for the "IA" badges in the accounts list. null if the reader is not available. */
export async function aiAccounts(api) {
  try {
    const d = await api("/api/admin/ai/accounts?limit=500");
    const m = new Map();
    for (const r of d.rows) if (r.call_id) m.set(r.slug, r.cost || 0);
    return m;
  } catch (e) { return null; }
}
