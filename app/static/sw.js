// v{{v}}
const SHELL = "cuenta-shell-{{v}}";
const SHARED = "cuenta-shared";
const PHOTOS = "cuenta-photos";   // ticket photos never change: kept for good (until the bill is removed from the device)
const KEEP = [SHELL, SHARED, PHOTOS];

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => {
  e.waitUntil((async () => {
    for (const k of await caches.keys()) if (!KEEP.includes(k)) await caches.delete(k);
    await self.clients.claim();
  })());
});

const isPage = (p) => p === "/" || p === "/cuentas" || p.startsWith("/b/");

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (url.origin !== location.origin) return;

  // Android share sheet: keep the photo, then open the app, which picks it up and scans it
  if (e.request.method === "POST" && url.pathname === "/share") {
    e.respondWith((async () => {
      try {
        const fd = await e.request.formData();
        const f = fd.get("photo");
        if (f && f.size) {
          const c = await caches.open(SHARED);
          await c.put("/shared-photo", new Response(f, { headers: { "Content-Type": f.type || "image/jpeg" } }));
          return Response.redirect("/?shared=1", 303);
        }
      } catch (err) { /* fall through */ }
      return Response.redirect("/", 303);
    })());
    return;
  }
  if (e.request.method !== "GET") return;

  // ticket photo: cache first. Keyed by its id (?v=), not by the slug, so renaming a bill keeps it cached.
  const pm = url.pathname.match(/^\/api\/bills\/[^/]+\/photo$/);
  if (pm && url.searchParams.get("v")) {
    const key = "/photo/" + url.searchParams.get("v");
    e.respondWith((async () => {
      const c = await caches.open(PHOTOS);
      const hit = await c.match(key);
      if (hit) return hit;
      const r = await fetch(e.request);
      if (r.ok) await c.put(key, r.clone());
      return r;
    })());
    return;
  }
  if (url.pathname.startsWith("/api/")) return;   // bill data is always asked fresh (the app keeps its own offline copy)

  // app shell: network first, cached copy when offline. Every page URL serves the same HTML, cached once as "/".
  e.respondWith((async () => {
    try {
      const r = await fetch(e.request);
      if (r.ok) {
        const c = await caches.open(SHELL);
        if (isPage(url.pathname)) c.put("/", r.clone());
        else if (url.pathname.startsWith("/static/")) c.put(e.request, r.clone());
      }
      return r;
    } catch (err) {
      const hit = isPage(url.pathname) ? await caches.match("/") : await caches.match(e.request);
      if (hit) return hit;
      throw err;
    }
  })());
});
