import asyncio
import hashlib
import html
import os
import shutil
import time
from contextlib import asynccontextmanager
from collections import defaultdict
from pathlib import Path

from fastapi import FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import admin
import aiclient
import limits
import ocr
import store

BASE = Path(__file__).parent
STATIC = BASE / "static"
MAX_BYTES = 12 * 1024 * 1024
MAX_WAITING = 3            # scans queued behind the one running; more -> 429 (CPU protection, the site is public)
PER_IP = (20, 3600)        # 20 scans / hour / IP
MAX_SCANS_DAY = int(os.environ.get("CUENTA_MAX_SCANS_DAY", "200"))   # whole site, per day; 0 = scans switched off
MIN_FREE_BYTES = 5 * 2**30  # no new tickets when the disk has less than 5 GB free (photos are kept for good)
MAX_JSON_BYTES = 64 * 1024  # bodies of the JSON API (edits, settings...): a few KB are plenty
WRITES = (120, 60)         # edits of bills: 120 / minute / IP
MARKS = (600, 60)         # marks are saved on every tap: 600 / minute / IP (a whole table may share one address)
MISSES = (30, 300)         # lookups of bills that do not exist: 30 / 5 min / IP (slugs cannot be enumerated)

@asynccontextmanager
async def lifespan(app: FastAPI):
    store.init()
    limits.state["paused"] = store.kv_get("paused") == "1"      # the admin's pause switch survives restarts
    limits.state["scan_cap"] = MAX_SCANS_DAY
    try:
        os.nice(10)     # OCR is CPU-heavy: yield to the rest of the server (cams, Jellyfin...) when it is busy
    except OSError:
        pass
    warm = asyncio.create_task(run_in_threadpool(ocr.engine))   # load the models now, not on the first scan
    yield
    warm.cancel()


__version__ = "1.0"

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
_busy = asyncio.Semaphore(1)
_waiting = 0
_inflight: dict[str, int] = defaultdict(int)     # scans being processed per IP
_allow, _abuse, _ip, _hits = limits.allow, limits.abuse, limits.client_ip, limits.hits


def _miss(request: Request) -> HTTPException:
    """A lookup of a bill that is not there: 404, or 429 for someone who keeps guessing."""
    if not _allow(_ip(request), "miss", MISSES):
        return HTTPException(429, "Demasiadas búsquedas. Espera un poco.")
    return HTTPException(404, "Esa cuenta no existe")


def _writes(request: Request) -> None:
    if not _allow(_ip(request), "write", WRITES):
        raise HTTPException(429, "Demasiados cambios seguidos. Espera un poco.")


def _version() -> str:
    h = hashlib.sha1()
    for p in sorted(STATIC.glob("*")):
        if p.is_file():
            h.update(p.read_bytes())
    return h.hexdigest()[:10]


VERSION = _version()
INDEX = (STATIC / "index.html").read_text(encoding="utf-8").replace("{{v}}", VERSION)
AI_NOTES = {   # shown on the home screen while the paid reader is on; the wording depends on the plan the service reports
    True: "Si la lectura automática no basta, la foto se envía a un servicio de IA (Google) únicamente para leerla.",
    False: "Si la lectura automática no basta, la foto se envía a un servicio de IA (Google) para leerla. En su plan gratuito, Google puede usarla para mejorar sus productos.",
}
SW = (STATIC / "sw.js").read_text(encoding="utf-8").replace("{{v}}", VERSION)
ADMIN_PAGE = (STATIC / "admin.html").read_text(encoding="utf-8").replace("{{v}}", VERSION)


@app.middleware("http")
async def headers(request: Request, call_next):
    tok = request.headers.get("x-person-token", "")
    store.PTOKEN.set(tok if 16 <= len(tok) <= 80 else None)
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Permissions-Policy"] = "camera=(self), microphone=(), geolocation=()"
    if request.url.path.startswith("/api/") and "cache-control" not in resp.headers:
        resp.headers["Cache-Control"] = "no-store"          # bill data (and the owner token at creation) is never cached
    if resp.headers.get("content-type", "").startswith("text/html"):
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' blob: data:; style-src 'self'; script-src 'self'; "
            "connect-src 'self'; media-src 'self' blob:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
    return resp


@app.get("/", response_class=HTMLResponse)
@app.get("/b/{slug}", response_class=HTMLResponse)
@app.get("/cuentas", response_class=HTMLResponse)
def index(request: Request, slug: str = ""):
    note = f'<p class="ai-note">{AI_NOTES[bool(aiclient.policy(False)["paid"])]}</p>' if aiclient.enabled(block=False) else ""
    page = INDEX.replace("{{ai_note}}", note)
    prev = store.preview_of(slug) if slug else None
    if prev:                                    # link preview (WhatsApp ...): the ticket's name instead of just "Cuenta"
        title, total = prev
        title = html.escape(title or "Cuenta", quote=True)
        desc = "Marca lo que has pedido y mira cuánto te toca."
        if total is not None:
            desc = f"Total {total:.2f} € · ".replace(".", ",") + desc
        origin = f"{request.headers.get('x-forwarded-proto', 'https')}://{request.headers.get('host', '')}"
        tags = (f'<title>{title} · Cuenta</title>\n<meta property="og:site_name" content="Cuenta">\n'
                f'<meta property="og:title" content="{title}">\n<meta property="og:description" content="{desc}">\n'
                f'<meta property="og:image" content="{html.escape(origin, quote=True)}/static/icons/apple-touch-icon.png">')
        page = page.replace("<title>Cuenta</title>", tags, 1)
    return HTMLResponse(page, headers={"Cache-Control": "no-cache"})


@app.get("/admin", response_class=HTMLResponse)
def admin_page():
    # the page itself is public (it only holds the login form); everything it does goes through /api/admin/* and needs the session
    return HTMLResponse(ADMIN_PAGE, headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex, nofollow"})


@app.get("/sw.js")
def sw():
    return Response(SW, media_type="text/javascript", headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})


@app.get("/manifest.webmanifest")
def manifest():
    return FileResponse(STATIC / "manifest.webmanifest", media_type="application/manifest+json",
                        headers={"Cache-Control": "no-cache"})


@app.post("/share")
def share_fallback():
    # The service worker normally takes this POST (Android share sheet). Without it, just open the app.
    return RedirectResponse("/", status_code=303)


@app.get("/healthz")
def healthz():
    return {"ok": True, "version": __version__}


@app.middleware("http")
async def early_size_check(request: Request, call_next):
    # refuse an oversized body from its declared length, before it is read
    if request.method == "POST" and request.url.path.startswith("/api/"):
        limit = MAX_BYTES + 65536 if request.url.path == "/api/scan" else MAX_JSON_BYTES
        try:
            if int(request.headers.get("content-length", "0")) > limit:
                return Response('{"detail":"Petición demasiado grande"}', status_code=413, media_type="application/json")
        except ValueError:
            return Response('{"detail":"Petición no válida"}', status_code=400, media_type="application/json")
    return await call_next(request)


@app.post("/api/scan")
async def scan(request: Request, photo: UploadFile = File(...)):
    global _waiting
    ip = _ip(request)
    if MAX_SCANS_DAY <= 0 or limits.state["paused"]:
        raise HTTPException(503, "El servicio de lectura está pausado")
    if not _allow(ip, "scan", PER_IP, record=False):
        raise HTTPException(429, "Demasiados tickets seguidos. Prueba más tarde.")
    if not _allow("*", "scan_day", (MAX_SCANS_DAY, 86400), record=False):      # whole-site daily budget
        raise HTTPException(429, "Hoy se ha alcanzado el límite de tickets del servicio. Prueba mañana.")
    if shutil.disk_usage(store.DATA).free < MIN_FREE_BYTES:
        print("[abuse] disk almost full: scans refused", flush=True)
        raise HTTPException(503, "El servicio está lleno ahora mismo")
    if _inflight[ip] >= 1:             # one ticket at a time per visitor: nobody can fill the queue alone
        _abuse(ip, "inflight")
        raise HTTPException(429, "Ya tienes un ticket en proceso. Espera a que termine.")
    data = await photo.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise HTTPException(413, "Foto demasiado grande")
    if _waiting >= MAX_WAITING:
        raise HTTPException(429, "Servidor ocupado, inténtalo en unos segundos")
    _allow(ip, "scan", PER_IP)          # counts the scan
    _allow("*", "scan_day", (MAX_SCANS_DAY, 86400))
    _waiting += 1
    _inflight[ip] += 1
    try:
        async with _busy:
            try:
                res = await run_in_threadpool(ocr.scan, data)
            except Exception as e:
                print(f"[scan] failed: {e!r}", flush=True)
                raise HTTPException(422, "No he podido leer esa imagen")
        local_match, call_id = res["matches"], None
        if await run_in_threadpool(aiclient.wanted, res):
            ai = await run_in_threadpool(aiclient.read, res["photo"])
            if ai:
                call_id = ai.get("call_id")        # paid either way, even if the local reading ends up being kept
            if aiclient.better(ai, res):
                ai["photo"] = res["photo"]
                res = ai
        if not res["lines"]:
            raise HTTPException(422, "No he encontrado líneas. Prueba con una foto más recta y con más luz.")
        bill = await run_in_threadpool(store.create, res["lines"], res["total"], res["photo"], res.get("title", ""), res.get("date"))
        if aiclient.configured():
            asyncio.create_task(run_in_threadpool(aiclient.event, bill["slug"], res, local_match, call_id))
        return bill
    finally:
        _waiting -= 1
        _inflight[ip] -= 1
        if _inflight[ip] <= 0:
            _inflight.pop(ip, None)


@app.get("/api/bills/{slug}")
def bill_get(slug: str, request: Request, since: int | None = None):
    if since is not None and store.version_of(slug) == since:
        return {"unchanged": True, "version": since}
    b = store.get(slug)
    if not b:
        raise _miss(request)
    return b


@app.get("/api/bills/{slug}/photo")
def bill_photo(slug: str, request: Request):
    p = store.photo_of(slug)
    if not p or not os.path.exists(p):
        raise _miss(request)
    return FileResponse(p, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=31536000, immutable"})


class OpsBody(BaseModel):
    ops: list[dict]


@app.post("/api/bills/{slug}/ops")
def bill_ops(slug: str, body: OpsBody, request: Request, x_owner_token: str | None = Header(None)):
    _writes(request)
    ok = store.can_edit(slug, x_owner_token)
    if ok is None:
        raise _miss(request)
    if not ok:
        raise HTTPException(403, "El dueño ha bloqueado las correcciones de esta cuenta")
    try:
        b = store.apply_ops(slug, body.ops)
    except (ValueError, KeyError, TypeError):
        raise HTTPException(422, "Datos no válidos")
    if not b:
        raise _miss(request)
    return b


class SettingsBody(BaseModel):
    allow_edits: bool


@app.post("/api/bills/{slug}/settings")
def bill_settings(slug: str, body: SettingsBody, request: Request, x_owner_token: str | None = Header(None)):
    _writes(request)
    ok = store.owner_can_manage(slug, x_owner_token)
    if ok is None:
        raise _miss(request)
    if not ok:
        raise HTTPException(403, "Solo el dueño de la cuenta puede cambiar esto")
    return store.set_allow(slug, body.allow_edits)


class MetaBody(BaseModel):
    title: str | None = None
    date: str | None = None


@app.post("/api/bills/{slug}/meta")
def bill_meta(slug: str, body: MetaBody, request: Request, x_owner_token: str | None = Header(None)):
    _writes(request)
    ok = store.owner_can_manage(slug, x_owner_token)
    if ok is None:
        raise _miss(request)
    if not ok:
        raise HTTPException(403, "Solo el dueño de la cuenta puede cambiar el nombre")
    try:
        return store.set_meta(slug, body.title, body.date)
    except ValueError as e:
        raise HTTPException(422, str(e))


class JoinBody(BaseModel):
    name: str


@app.post("/api/bills/{slug}/join")
def bill_join(slug: str, body: JoinBody, request: Request):
    """A device says who it is on this bill (name, and the server picks its colour). Safe to call again to rename."""
    _writes(request)
    token = store.PTOKEN.get()
    if not token:
        raise HTTPException(400, "Falta la clave del dispositivo")
    try:
        b = store.join(slug, body.name, token)
    except ValueError as e:
        raise HTTPException(422, str(e))
    except OverflowError as e:
        raise HTTPException(409, str(e))
    if not b:
        raise _miss(request)
    return b


class MarksBody(BaseModel):
    marks: dict[str, float]


@app.post("/api/bills/{slug}/marks")
def bill_marks(slug: str, body: MarksBody, request: Request):
    """Replaces the caller's own marks (the whole set at once)."""
    if not _allow(_ip(request), "marks", MARKS):
        raise HTTPException(429, "Demasiados cambios seguidos. Espera un poco.")
    token = store.PTOKEN.get()
    if not token:
        raise HTTPException(400, "Falta la clave del dispositivo")
    if len(body.marks) > 300:
        raise HTTPException(422, "Demasiadas marcas")
    try:
        b = store.set_marks(slug, token, body.marks)
    except PermissionError as e:
        raise HTTPException(403, str(e))
    if not b:
        raise _miss(request)
    return b


@app.post("/api/bills/{slug}/people/{pid}/clear")
def bill_clear_person(slug: str, pid: str, request: Request, x_owner_token: str | None = Header(None)):
    _writes(request)
    ok = store.owner_can_manage(slug, x_owner_token)
    if ok is None:
        raise _miss(request)
    if not ok:
        raise HTTPException(403, "Solo el dueño de la cuenta puede quitar las marcas de otra persona")
    b = store.clear_person(slug, pid)
    if not b:
        raise _miss(request)
    return b


@app.post("/api/bills/{slug}/claim")
def bill_claim(slug: str, request: Request):
    _writes(request)
    if store.version_of(slug) is None:
        raise _miss(request)
    token = store.claim(slug)
    if not token:
        raise HTTPException(409, "Esta cuenta ya tiene dueño")
    return {"owner_token": token}


app.include_router(admin.router)
app.mount("/static", StaticFiles(directory=STATIC), name="static")
