"""Admin panel API (/api/admin/*). The site is public and has no accounts, so this is the one place with a password:
- the password is set from the command line (`admin_tools.py set-password`), only its scrypt hash is stored;
- a login gives a random session cookie (HttpOnly, SameSite=Strict, Secure behind https), kept server-side as a hash;
- every call except login/me needs the session; state-changing calls also need the `X-Admin: 1` header and a same-origin
  request (CSRF), and logins are throttled per IP and for the whole site."""
import io
import os

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool

from pydantic import BaseModel

import adminauth
import limits
import store

router = APIRouter()
COOKIE = "cuenta_admin"
SESSION_SECONDS = 12 * 3600
FAIL_IP = (5, 600)         # wrong passwords: 5 / 10 min / IP
FAIL_ALL = (30, 3600)      # ... and 30 / hour for the whole site, whoever they come from
THUMB = 240


def require_admin(request: Request) -> None:
    token = request.cookies.get(COOKIE)
    if not token or not store.valid_session(token):
        raise HTTPException(401, "No autorizado")
    if request.method not in ("GET", "HEAD"):
        if request.headers.get("x-admin") != "1" or request.headers.get("sec-fetch-site", "same-origin") not in ("same-origin", "none"):
            raise HTTPException(403, "Petición no permitida")


class LoginBody(BaseModel):
    password: str


@router.get("/api/admin/me")
def me(request: Request):
    token = request.cookies.get(COOKIE)
    return {"auth": bool(token and store.valid_session(token)), "configured": store.admin_hash_get() is not None}


@router.post("/api/admin/login")
async def login(body: LoginBody, request: Request, response: Response):
    ip = limits.client_ip(request)
    if request.headers.get("x-admin") != "1":
        raise HTTPException(403, "Petición no permitida")
    if not limits.allow(ip, "adminfail", FAIL_IP, record=False) or not limits.allow("*", "adminfail", FAIL_ALL, record=False):
        raise HTTPException(429, "Demasiados intentos. Espera unos minutos.")
    stored = store.admin_hash_get()
    if not stored:
        raise HTTPException(409, "El panel todavía no tiene contraseña. Créala con: docker exec -it cuenta python admin_tools.py set-password")
    ok = await run_in_threadpool(adminauth.verify, body.password[:200], stored)
    if not ok:
        limits.allow(ip, "adminfail", FAIL_IP)
        limits.allow("*", "adminfail", FAIL_ALL)
        print(f"[admin] failed login from {ip}", flush=True)
        raise HTTPException(401, "Contraseña incorrecta")
    token = store.new_session(SESSION_SECONDS)
    response.set_cookie(COOKIE, token, max_age=SESSION_SECONDS, httponly=True, samesite="strict", path="/",
                        secure=request.headers.get("x-forwarded-proto") == "https")
    print(f"[admin] login from {ip}", flush=True)
    return {"ok": True}


@router.post("/api/admin/logout")
def logout(request: Request, response: Response, _=Depends(require_admin)):
    store.drop_session(request.cookies.get(COOKIE, ""))
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


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
