"""Admin panel API (/api/admin/*). The site is public and has no accounts, so this is the one place with a password:
- the panel has no link anywhere: only whoever knows /admin finds it. While no password is defined, opening it counts
  as being logged in and it offers to create one (or `admin_tools.py set-password`); only its scrypt hash is stored;
- a login gives a random session cookie (HttpOnly, SameSite=Strict, Secure behind https), kept server-side as a hash.
  It never expires: the cookie is renewed whenever the panel is opened;
- every call except login/me needs the session; state-changing calls also need the `X-Admin: 1` header and a same-origin
  request (CSRF), and logins are throttled per IP and for the whole site;
- /api/admin/ai/* forwards to the private AI reader service (see aiclient.py); it needs a real password."""
import io
import os

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from pydantic import BaseModel

import adminauth
import aiclient
import limits
import store

router = APIRouter()
COOKIE = "cuenta_admin"
SESSION_SECONDS = 20 * 365 * 86400   # the admin session never expires...
COOKIE_AGE = 400 * 86400             # ...the cookie is the longest browsers keep, renewed on every check-in
FAIL_IP = (5, 600)         # wrong passwords: 5 / 10 min / IP
FAIL_ALL = (30, 3600)      # ... and 30 / hour for the whole site, whoever they come from
THUMB = 240


def _open() -> bool:
    """No admin password defined yet: opening the panel is the same as being logged in (and it offers to create one)."""
    return store.admin_hash_get() is None


def _cookie(request: Request, response: Response, token: str) -> None:
    response.set_cookie(COOKIE, token, max_age=COOKIE_AGE, httponly=True, samesite="strict", path="/",
                        secure=request.headers.get("x-forwarded-proto") == "https")


def _logged(request: Request) -> bool:
    token = request.cookies.get(COOKIE)
    return bool(token and store.valid_session(token))


def require_admin(request: Request) -> None:
    if not _open() and not _logged(request):
        raise HTTPException(401, "No autorizado")
    if request.method not in ("GET", "HEAD"):
        if request.headers.get("x-admin") != "1" or request.headers.get("sec-fetch-site", "same-origin") not in ("same-origin", "none"):
            raise HTTPException(403, "Petición no permitida")


def require_password(request: Request) -> None:
    """Everything that touches the paid reader needs a real password, even while the rest of the panel is open."""
    require_admin(request)
    if _open():
        raise HTTPException(403, "Crea primero la contraseña del panel")


class LoginBody(BaseModel):
    password: str


@router.get("/api/admin/me")
def me(request: Request, response: Response):
    response.headers["Cache-Control"] = "no-store"
    configured = store.admin_hash_get() is not None
    if not configured:
        if not _logged(request):                                 # opening the admin page without a password = logged in
            _cookie(request, response, store.new_session(SESSION_SECONDS))
        return {"auth": True, "configured": False, "ai": aiclient.configured()}
    ok = _logged(request)
    if ok:
        _cookie(request, response, request.cookies[COOKIE])
    return {"auth": ok, "configured": True, "ai": aiclient.configured()}


@router.post("/api/admin/login")
async def login(body: LoginBody, request: Request, response: Response):
    ip = limits.client_ip(request)
    if request.headers.get("x-admin") != "1":
        raise HTTPException(403, "Petición no permitida")
    if not limits.allow(ip, "adminfail", FAIL_IP, record=False) or not limits.allow("*", "adminfail", FAIL_ALL, record=False):
        raise HTTPException(429, "Demasiados intentos. Espera unos minutos.")
    stored = store.admin_hash_get()
    if not stored:
        raise HTTPException(409, "El panel todavía no tiene contraseña: ábrelo sin más y créala")
    ok = await run_in_threadpool(adminauth.verify, body.password[:200], stored)
    if not ok:
        limits.allow(ip, "adminfail", FAIL_IP)
        limits.allow("*", "adminfail", FAIL_ALL)
        print(f"[admin] failed login from {ip}", flush=True)
        raise HTTPException(401, "Contraseña incorrecta")
    _cookie(request, response, store.new_session(SESSION_SECONDS))
    print(f"[admin] login from {ip}", flush=True)
    return {"ok": True}


class PasswordBody(BaseModel):
    new: str
    current: str = ""


@router.post("/api/admin/password")
async def password(body: PasswordBody, request: Request, response: Response, _=Depends(require_admin)):
    """Create the password (while there is none) or change it (needs the current one). Everybody else is signed out;
    this browser stays signed in."""
    if len(body.new) < 8:
        raise HTTPException(422, "Mínimo 8 caracteres")
    stored = store.admin_hash_get()
    if stored:
        ip = limits.client_ip(request)
        if not limits.allow(ip, "adminfail", FAIL_IP, record=False):
            raise HTTPException(429, "Demasiados intentos. Espera unos minutos.")
        if not await run_in_threadpool(adminauth.verify, body.current[:200], stored):
            limits.allow(ip, "adminfail", FAIL_IP)
            raise HTTPException(401, "La contraseña actual no es correcta")
    h = await run_in_threadpool(adminauth.hash_password, body.new[:200])
    store.admin_hash_set(h)
    _cookie(request, response, store.new_session(SESSION_SECONDS))
    print("[admin] password " + ("changed" if stored else "created"), flush=True)
    return {"ok": True}


@router.post("/api/admin/logout")
def logout(request: Request, response: Response, _=Depends(require_admin)):
    store.drop_session(request.cookies.get(COOKIE, ""))
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


# ---- the paid reader's settings live in the private service; this only forwards (the key is never stored here)
AI_PATHS = {"state", "settings", "key", "test-key", "calibrate", "test-mail", "accounts", "calls", "reveal-key"}


@router.api_route("/api/admin/ai/{path}", methods=["GET", "POST"])
async def ai_proxy(path: str, request: Request, _=Depends(require_password)):
    if path not in AI_PATHS or not aiclient.configured():
        raise HTTPException(404, "El lector IA no está configurado")
    body = await request.body() if request.method == "POST" else None
    code, data = await run_in_threadpool(aiclient.admin_call, request.method, path, request.url.query, body)
    return JSONResponse(data, status_code=code, headers={"Cache-Control": "no-store"})


@router.get("/api/admin/stats")
def stats(_=Depends(require_admin)):
    s = store.admin_stats()
    s["scans_24h"] = limits.count("scan_day", "*", 86400)
    s["scan_cap"] = limits.state["scan_cap"]
    s["paused"] = limits.state["paused"]
    s["events"] = [{"t": t, "ip": ip, "kind": kind} for t, ip, kind in list(limits.events)[-15:]][::-1]
    return s


@router.get("/api/admin/bills")
def bills(_=Depends(require_admin)):
    return store.admin_bills()


@router.get("/api/admin/bills/{slug}/thumb")
def thumb(slug: str, _=Depends(require_admin)):
    path = store.photo_of(slug)
    if not path or not os.path.exists(path):
        raise HTTPException(404)
    from PIL import Image
    im = Image.open(path)
    im.thumbnail((THUMB, THUMB * 2))
    buf = io.BytesIO()
    im.convert("RGB").save(buf, "JPEG", quality=70)
    return Response(buf.getvalue(), media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})


class DeleteBody(BaseModel):
    slugs: list[str]


@router.post("/api/admin/delete")
def delete(body: DeleteBody, _=Depends(require_admin)):
    if not body.slugs or len(body.slugs) > 500:
        raise HTTPException(422, "Nada que borrar")
    n = store.delete_bills(body.slugs)
    print(f"[admin] deleted {n} bill(s)", flush=True)
    return {"deleted": n}


@router.get("/api/admin/idle")
def idle(days: int, _=Depends(require_admin)):
    if not 1 <= days <= 3650:
        raise HTTPException(422, "Días no válidos")
    return {"count": len(store.idle_slugs(days))}


class IdleBody(BaseModel):
    days: int


@router.post("/api/admin/delete-idle")
def delete_idle(body: IdleBody, _=Depends(require_admin)):
    if not 1 <= body.days <= 3650:
        raise HTTPException(422, "Días no válidos")
    n = store.delete_bills(store.idle_slugs(body.days))
    print(f"[admin] deleted {n} idle bill(s) (> {body.days} days)", flush=True)
    return {"deleted": n}


class PauseBody(BaseModel):
    paused: bool


@router.post("/api/admin/pause")
def pause(body: PauseBody, _=Depends(require_admin)):
    store.kv_set("paused", "1" if body.paused else "0")
    limits.state["paused"] = body.paused
    print(f"[admin] scanning {'paused' if body.paused else 'resumed'}", flush=True)
    return {"paused": body.paused}
