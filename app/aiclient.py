"""Optional second opinion from an external reader service (a separate, private container).

Off unless CUENTA_AI_URL and CUENTA_AI_SECRET are set. This app holds no API key and never talks to the internet: it
posts the already shrunk ticket photo to that internal service and takes back plain lines. The service decides whether
it is switched on and when to use it (/policy). Any failure -> None -> the local OCR reading stays."""
import json
import os
import threading
import time
import urllib.request

URL = os.environ.get("CUENTA_AI_URL", "").rstrip("/")
SECRET = os.environ.get("CUENTA_AI_SECRET", "")

_pol = {"t": 0.0, "enabled": False, "mode": "fallback", "paid": False}
_busy = threading.Lock()


def configured() -> bool:
    return bool(URL and SECRET)


def _call(path: str, data: bytes | None = None, ctype: str = "application/json", timeout: float = 60):
    req = urllib.request.Request(URL + path, data=data, headers={"x-secret": SECRET, "Content-Type": ctype})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _refresh() -> None:
    try:
        p = _call("/policy", timeout=3)
        _pol.update(enabled=bool(p.get("enabled")), mode=p.get("mode", "fallback"), paid=bool(p.get("paid")))
    except Exception:
        _pol.update(enabled=False)
    _pol["t"] = time.time()


def policy(block: bool = True) -> dict:
    """Cached for 20 s. block=False never waits: it kicks off a refresh in the background and answers with what it knows."""
    if configured() and time.time() - _pol["t"] > 20 and _busy.acquire(blocking=False):
        def go():
            try:
                _refresh()
            finally:
                _busy.release()
        if block:
            go()
        else:
            threading.Thread(target=go, daemon=True).start()
    return _pol


def enabled(block: bool = True) -> bool:
    return configured() and bool(policy(block)["enabled"])


def wanted(local: dict) -> bool:
    return enabled() and (policy()["mode"] == "always" or not local["lines"] or not local["matches"])


def read(jpeg: bytes) -> dict | None:
    try:
        d = _call("/read", jpeg, "image/jpeg")
    except Exception as e:          # 402 = balance spent, 429 = daily cap, 503 = off, network ...: the local reading is used
        print(f"[ai] unavailable: {e!r}", flush=True)
        _pol["t"] = 0.0             # ask again for the policy next time
        return None
    lines = [{"qty": l["qty"], "name": l["name"], "price": l["price"], "amount": l["amount"], "ok": True, "y": None, "h": None}
             for l in d.get("lines", [])]
    total = d.get("total")
    s = round(sum(l["amount"] for l in lines), 2)
    return {"lines": lines, "total": total, "sum": s, "title": d.get("title") or "", "date": d.get("date"),
            "matches": total is not None and abs(total - s) < 0.011, "call_id": d.get("call_id")}


def better(ai: dict | None, local: dict) -> bool:
    """The reader's answer replaces the local one when it adds up, or when the local one found nothing."""
    return bool(ai and ai["lines"] and (ai["matches"] or not local["lines"]))


def event(slug: str, res: dict, local_match: bool, call_id: int | None) -> None:
    """Tell the service about every scan (so its panel can list the accounts). Best effort."""
    if not configured():
        return
    try:
        _call("/event", json.dumps({"slug": slug, "title": res.get("title") or "", "total": res.get("total"),
                                    "nlines": len(res["lines"]), "local_match": local_match, "call_id": call_id}).encode(), timeout=5)
    except Exception as e:
        print(f"[ai] event not sent: {e!r}", flush=True)


def admin_call(method: str, path: str, query: str, body: bytes | None) -> tuple[int, dict]:
    """Forward an admin request to the service. Returns (status, json) and never raises."""
    import urllib.error
    req = urllib.request.Request(f"{URL}/admin-api/{path}" + (f"?{query}" if query else ""), data=body, method=method,
                                 headers={"x-secret": SECRET, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.load(e)
        except Exception:
            return e.code, {"detail": f"Error {e.code}"}
    except Exception:
        return 502, {"detail": "No puedo hablar con el lector IA"}
