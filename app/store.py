"""Shared bills: SQLite + photo files in /data. A bill = the (possibly corrected) lines of one ticket, its photo
and a `version` that grows with every change, so other devices can poll cheaply and pick changes up.
People (a name + a colour per device that joined the bill) and their marks ("this is mine", units per line) live here
too, so everybody sees who took what. A person is identified by a secret per-device token (stored as a hash), so nobody
can mark or unmark on behalf of someone else."""
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
from contextvars import ContextVar
from datetime import datetime
from zoneinfo import ZoneInfo
from contextlib import contextmanager

DATA = os.environ.get("CUENTA_DATA", "/data")
PHOTOS = os.path.join(DATA, "photos")
_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"   # no look-alikes (i l o 0 1)
PALETTE_SIZE = 10        # colours are indexes into a pastel palette defined in the app; green is reserved for "me"
MAX_PEOPLE = 20
# the visitor's per-device secret for the request being served (set by the middleware, read when building a bill)
PTOKEN: ContextVar[str | None] = ContextVar("ptoken", default=None)

SCHEMA = """
CREATE TABLE IF NOT EXISTS bills (
  id TEXT PRIMARY KEY, slug TEXT NOT NULL UNIQUE,
  created REAL NOT NULL, updated REAL NOT NULL, version INTEGER NOT NULL DEFAULT 1,
  total REAL, lines TEXT NOT NULL, title TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS people (
  id TEXT PRIMARY KEY, bill_id TEXT NOT NULL, name TEXT NOT NULL, color INTEGER NOT NULL,
  token_hash TEXT NOT NULL, created REAL NOT NULL, UNIQUE(bill_id, token_hash));
CREATE TABLE IF NOT EXISTS marks (
  bill_id TEXT NOT NULL, person_id TEXT NOT NULL, line_id TEXT NOT NULL, units REAL NOT NULL,
  PRIMARY KEY (person_id, line_id));
CREATE INDEX IF NOT EXISTS marks_bill ON marks(bill_id);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS admin_sessions (token_hash TEXT PRIMARY KEY, expires REAL NOT NULL);
"""


@contextmanager
def conn():
    c = sqlite3.connect(os.path.join(DATA, "cuenta.db"), timeout=10, isolation_level=None)   # autocommit: transactions are explicit
    c.row_factory = sqlite3.Row
    try:
        yield c
    finally:
        c.close()


@contextmanager
def write_tx():
    """Read-modify-write under one write lock (BEGIN IMMEDIATE): two people correcting at the same moment queue up
    instead of overwriting each other's change."""
    with conn() as c:
        c.execute("BEGIN IMMEDIATE")
        try:
            yield c
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise


def init() -> None:
    os.makedirs(PHOTOS, exist_ok=True)
    with conn() as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript(SCHEMA)
        cols = [r["name"] for r in c.execute("PRAGMA table_info(bills)")]
        if "title" not in cols:
            c.execute("ALTER TABLE bills ADD COLUMN title TEXT NOT NULL DEFAULT ''")
        if "date" not in cols:            # ticket date (ISO), NULL on bills from before dates existed
            c.execute("ALTER TABLE bills ADD COLUMN date TEXT")
        if "owner_hash" not in cols:      # sha256 of the owner's secret token (NULL = bill from before owners existed)
            c.execute("ALTER TABLE bills ADD COLUMN owner_hash TEXT")
        if "allow_edits" not in cols:     # may people other than the owner correct lines?
            c.execute("ALTER TABLE bills ADD COLUMN allow_edits INTEGER NOT NULL DEFAULT 1")


def _rid(n: int = 6) -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(n))


def photo_path(bill_id: str) -> str:
    return os.path.join(PHOTOS, bill_id + ".jpg")


def today() -> str:
    return datetime.now(ZoneInfo("Europe/Madrid")).date().isoformat()


def _people(c, bill_id: str) -> list[dict]:
    return [{"id": p["id"], "name": p["name"], "color": p["color"]}
            for p in c.execute("SELECT id, name, color FROM people WHERE bill_id=? ORDER BY created, id", (bill_id,))]


def _marks(c, bill_id: str) -> dict:
    out: dict = {}
    for m in c.execute("SELECT person_id, line_id, units FROM marks WHERE bill_id=?", (bill_id,)):
        out.setdefault(m["person_id"], {})[m["line_id"]] = m["units"]
    return out


def _me(c, bill_id: str) -> str | None:
    t = PTOKEN.get()
    if not t:
        return None
    r = c.execute("SELECT id FROM people WHERE bill_id=? AND token_hash=?", (bill_id, _h(t))).fetchone()
    return r["id"] if r else None


def _out(c, r) -> dict:
    date = r["date"] or datetime.fromtimestamp(r["created"], ZoneInfo("Europe/Madrid")).date().isoformat()
    return {"slug": r["slug"], "version": r["version"], "total": r["total"], "title": r["title"], "date": date,
            "lines": json.loads(r["lines"]),
            "people": _people(c, r["id"]), "marks": _marks(c, r["id"]), "me": _me(c, r["id"]),
            "allow_edits": bool(r["allow_edits"]), "has_owner": r["owner_hash"] is not None,
            "photo": f"/api/bills/{r['slug']}/photo?v={r['id']}"}


def _h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def is_owner(slug: str, token: str | None) -> bool:
    if not token:
        return False
    with conn() as c:
        r = c.execute("SELECT owner_hash FROM bills WHERE slug=?", (slug,)).fetchone()
    return bool(r and r["owner_hash"] and hmac.compare_digest(r["owner_hash"], _h(token)))


def can_edit(slug: str, token: str | None) -> bool | None:
    """None = no such bill. Everyone may edit unless the owner switched corrections off."""
    with conn() as c:
        r = c.execute("SELECT allow_edits FROM bills WHERE slug=?", (slug,)).fetchone()
    if not r:
        return None
    return bool(r["allow_edits"]) or is_owner(slug, token)


def owner_can_manage(slug: str, token: str | None) -> bool | None:
    """Renaming / settings: the owner only. A bill from before owners existed has none: anyone may manage it."""
    with conn() as c:
        r = c.execute("SELECT owner_hash FROM bills WHERE slug=?", (slug,)).fetchone()
    if not r:
        return None
    return r["owner_hash"] is None or is_owner(slug, token)


def set_allow(slug: str, allow: bool) -> dict | None:
    with write_tx() as c:
        r = c.execute("SELECT id FROM bills WHERE slug=?", (slug,)).fetchone()
        if not r:
            return None
        c.execute("UPDATE bills SET allow_edits=?, version=version+1, updated=? WHERE id=?", (1 if allow else 0, time.time(), r["id"]))
        return _out(c, c.execute("SELECT * FROM bills WHERE id=?", (r["id"],)).fetchone())


def set_meta(slug: str, title: str | None, date: str | None) -> dict | None:
    """Label and ticket date (ISO) of a bill; a None field is left as it is."""
    from datetime import date as _d, timedelta
    if title is not None:
        title = " ".join("".join(ch for ch in title if ch.isprintable()).split())[:40]
    if date is not None:
        try:
            d = _d.fromisoformat(date)
        except ValueError:
            raise ValueError("Fecha no válida")
        if not (_d(2000, 1, 1) <= d <= datetime.now(ZoneInfo("Europe/Madrid")).date() + timedelta(days=1)):
            raise ValueError("Fecha fuera de rango")
        date = d.isoformat()
    with write_tx() as c:
        r = c.execute("SELECT id, title, date FROM bills WHERE slug=?", (slug,)).fetchone()
        if not r:
            return None
        c.execute("UPDATE bills SET title=?, date=?, version=version+1, updated=? WHERE id=?",
                  (r["title"] if title is None else title, r["date"] if date is None else date, time.time(), r["id"]))
        return _out(c, c.execute("SELECT * FROM bills WHERE id=?", (r["id"],)).fetchone())


def claim(slug: str) -> str | None:
    """Bills made before owners existed: the first person to claim one becomes its owner. Returns the token."""
    token = secrets.token_urlsafe(24)
    with conn() as c:
        cur = c.execute("UPDATE bills SET owner_hash=? WHERE slug=? AND owner_hash IS NULL", (_h(token), slug))
        return token if cur.rowcount else None


def create(lines: list[dict], total: float | None, photo: bytes, title: str = "", date: str | None = None) -> dict:
    owner_token = secrets.token_urlsafe(24)
    # Locked by default: the owner checks the ticket and nobody else can touch it. Unless the OCR could not vouch for
    # the reading - a doubtful (amber) line, or the lines do not add up to the ticket total (or no total was read) -
    # in which case others may help correct it until the owner decides to lock it.
    verified = total is not None and round(sum(l.get("amount", 0) for l in lines) * 100) == round(total * 100)
    allow = 1 if (any(not l.get("ok", True) for l in lines) or not verified) else 0
    for l in lines:
        l["id"] = _rid(5)
    bid = secrets.token_hex(8)
    with open(photo_path(bid), "wb") as f:
        f.write(photo)
    now = time.time()
    try:
        with conn() as c:
            for _ in range(20):
                slug = _rid(6)
                try:
                    c.execute("INSERT INTO bills(id,slug,created,updated,version,total,lines,title,owner_hash,allow_edits,date) VALUES (?,?,?,?,1,?,?,?,?,?,?)",
                              (bid, slug, now, now, total, json.dumps(lines), title, _h(owner_token), allow, date or today()))
                    break
                except sqlite3.IntegrityError:
                    continue
            else:
                raise RuntimeError("no free slug")
            out = _out(c, c.execute("SELECT * FROM bills WHERE id=?", (bid,)).fetchone())
    except BaseException:
        try:
            os.remove(photo_path(bid))      # do not leave the photo orphaned when the bill could not be stored
        except FileNotFoundError:
            pass
        raise
    out["owner_token"] = owner_token      # handed out once, only to the person who scanned
    return out


def get(slug: str) -> dict | None:
    with conn() as c:
        r = c.execute("SELECT * FROM bills WHERE slug=?", (slug,)).fetchone()
        return _out(c, r) if r else None


def photo_of(slug: str) -> str | None:
    with conn() as c:
        r = c.execute("SELECT id FROM bills WHERE slug=?", (slug,)).fetchone()
    return photo_path(r["id"]) if r else None


def _clean_line(o: dict, old: dict | None) -> dict:
    name = str(o.get("name", "")).strip()[:60]
    qty = float(o["qty"])
    amount = round(float(o["amount"]), 2)
    if not (0 < qty <= 999) or not (-99999 < amount < 99999):
        raise ValueError("valores fuera de rango")
    line = {"id": (old or {}).get("id") or _rid(5), "name": name, "qty": qty, "amount": amount,
            "price": round(amount / qty, 4), "ok": True, "fixed": True}
    for k in ("y", "h"):                # where the row is in the photo (kept when the line is edited)
        if old and old.get(k) is not None:
            line[k] = old[k]
    return line


def apply_ops(slug: str, ops: list[dict]) -> dict | None:
    """Ops are per line, so two people correcting DIFFERENT lines never overwrite each other;
    the same line: last write wins."""
    with write_tx() as c:
        r = c.execute("SELECT * FROM bills WHERE slug=?", (slug,)).fetchone()
        if not r:
            return None
        lines = json.loads(r["lines"])
        total = r["total"]
        for op in ops[:50]:
            kind = op.get("op")
            idx = next((i for i, l in enumerate(lines) if l["id"] == op.get("id")), -1)
            if kind == "upd" and idx >= 0:
                lines[idx] = _clean_line(op, lines[idx])
            elif kind == "add":
                if len(lines) < 200:
                    lines.append(_clean_line(op, None))
            elif kind == "del" and idx >= 0:
                lines.pop(idx)
            elif kind == "ok" and idx >= 0:    # "I checked it, it's right"
                lines[idx]["ok"] = True
                lines[idx]["fixed"] = True
            elif kind == "total":
                if op.get("value") is None:
                    total = None
                else:
                    total = round(float(op["value"]), 2)
                    if not (0 <= total < 99999):          # also rejects NaN / infinity
                        raise ValueError("total fuera de rango")
        c.execute("UPDATE bills SET lines=?, total=?, version=version+1, updated=? WHERE id=?",
                  (json.dumps(lines), total, time.time(), r["id"]))
        _tidy_marks(c, r["id"], lines)
        return _out(c, c.execute("SELECT * FROM bills WHERE id=?", (r["id"],)).fetchone())


def _tidy_marks(c, bill_id: str, lines: list[dict]) -> None:
    """After lines were edited/deleted: drop marks of lines that are gone and cap units at the (new) quantity."""
    ids = {l["id"]: l["qty"] for l in lines}
    for lid in {m["line_id"] for m in c.execute("SELECT DISTINCT line_id FROM marks WHERE bill_id=?", (bill_id,))}:
        if lid not in ids:
            c.execute("DELETE FROM marks WHERE bill_id=? AND line_id=?", (bill_id, lid))
        else:
            c.execute("UPDATE marks SET units=? WHERE bill_id=? AND line_id=? AND units>?", (ids[lid], bill_id, lid, ids[lid]))


def clean_name(name: str) -> str:
    name = " ".join("".join(ch for ch in str(name) if ch.isprintable()).split())[:24]
    if not name:
        raise ValueError("Escribe un nombre")
    return name


def _bump(c, bill_id: str) -> None:
    c.execute("UPDATE bills SET version=version+1, updated=? WHERE id=?", (time.time(), bill_id))


def join(slug: str, name: str, token: str) -> dict | None:
    """Idempotent: the first call of a device adds the person (lowest free colour); later calls only update the name."""
    name = clean_name(name)
    with write_tx() as c:
        r = c.execute("SELECT * FROM bills WHERE slug=?", (slug,)).fetchone()
        if not r:
            return None
        p = c.execute("SELECT * FROM people WHERE bill_id=? AND token_hash=?", (r["id"], _h(token))).fetchone()
        if p:
            if p["name"] != name:
                c.execute("UPDATE people SET name=? WHERE id=?", (name, p["id"]))
                _bump(c, r["id"])
        else:
            people = c.execute("SELECT color FROM people WHERE bill_id=?", (r["id"],)).fetchall()
            if len(people) >= MAX_PEOPLE:
                raise OverflowError("Esta cuenta ya tiene el máximo de personas")
            used = {x["color"] for x in people}
            color = next((i for i in range(PALETTE_SIZE) if i not in used), len(people) % PALETTE_SIZE)
            c.execute("INSERT INTO people(id, bill_id, name, color, token_hash, created) VALUES (?,?,?,?,?,?)",
                      (_rid(6), r["id"], name, color, _h(token), time.time()))
            _bump(c, r["id"])
        r = c.execute("SELECT * FROM bills WHERE id=?", (r["id"],)).fetchone()
        return _out(c, r)


def set_marks(slug: str, token: str, marks: dict) -> dict | None:
    """Replaces MY marks of the bill with {line_id: units}. Sending the same set again changes nothing (so a device
    that was offline can simply resend everything)."""
    with write_tx() as c:
        r = c.execute("SELECT * FROM bills WHERE slug=?", (slug,)).fetchone()
        if not r:
            return None
        p = c.execute("SELECT id FROM people WHERE bill_id=? AND token_hash=?", (r["id"], _h(token))).fetchone()
        if not p:
            raise PermissionError("Primero di cómo te llamas")
        qty = {l["id"]: float(l["qty"]) for l in json.loads(r["lines"])}
        new: dict[str, float] = {}
        for lid, u in marks.items():
            if lid in qty:
                u = min(float(u), qty[lid])
                if u > 0 and u == u:                 # not NaN
                    new[lid] = round(u, 4)
        old = {m["line_id"]: m["units"] for m in c.execute("SELECT line_id, units FROM marks WHERE person_id=?", (p["id"],))}
        if old != new:
            c.execute("DELETE FROM marks WHERE person_id=?", (p["id"],))
            c.executemany("INSERT INTO marks(bill_id, person_id, line_id, units) VALUES (?,?,?,?)",
                          [(r["id"], p["id"], lid, u) for lid, u in new.items()])
            _bump(c, r["id"])
        return _out(c, c.execute("SELECT * FROM bills WHERE id=?", (r["id"],)).fetchone())


def clear_person(slug: str, person_id: str) -> dict | None:
    """Owner's tool for fixing mistakes: remove every mark of one person."""
    with write_tx() as c:
        r = c.execute("SELECT * FROM bills WHERE slug=?", (slug,)).fetchone()
        if not r:
            return None
        n = c.execute("DELETE FROM marks WHERE bill_id=? AND person_id=?", (r["id"], person_id)).rowcount
        if n:
            _bump(c, r["id"])
        return _out(c, c.execute("SELECT * FROM bills WHERE id=?", (r["id"],)).fetchone())


def version_of(slug: str) -> int | None:
    with conn() as c:
        r = c.execute("SELECT version FROM bills WHERE slug=?", (slug,)).fetchone()
    return r["version"] if r else None


def preview_of(slug: str) -> tuple[str, float | None] | None:
    """Title and total of a bill, for the link preview. Nothing else about it leaves the database."""
    with conn() as c:
        r = c.execute("SELECT title, total FROM bills WHERE slug=?", (slug,)).fetchone()
    return (r["title"], r["total"]) if r else None


# ---------------- admin panel ----------------
def kv_get(k: str, default: str | None = None) -> str | None:
    with conn() as c:
        r = c.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    return r["v"] if r else default


def kv_set(k: str, v: str) -> None:
    with conn() as c:
        c.execute("INSERT INTO kv(k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))


def admin_hash_get() -> str | None:
    return kv_get("admin_hash")


def admin_hash_set(h: str) -> None:
    kv_set("admin_hash", h)
    drop_all_sessions()                      # a new password signs everybody out


def new_session(seconds: int) -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    with conn() as c:
        c.execute("DELETE FROM admin_sessions WHERE expires < ?", (now,))
        c.execute("INSERT INTO admin_sessions(token_hash, expires) VALUES (?, ?)", (_h(token), now + seconds))
    return token


def valid_session(token: str) -> bool:
    with conn() as c:
        r = c.execute("SELECT expires FROM admin_sessions WHERE token_hash=?", (_h(token),)).fetchone()
    return bool(r and r["expires"] > time.time())


def drop_session(token: str) -> None:
    with conn() as c:
        c.execute("DELETE FROM admin_sessions WHERE token_hash=?", (_h(token),))


def drop_all_sessions() -> None:
    with conn() as c:
        c.execute("DELETE FROM admin_sessions")


def _size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def admin_bills() -> list[dict]:
    with conn() as c:
        rows = c.execute("""
            SELECT b.id, b.slug, b.title, b.date, b.total, b.created, b.updated, b.allow_edits, b.lines,
                   (b.owner_hash IS NOT NULL) AS has_owner,
                   (SELECT COUNT(*) FROM people p WHERE p.bill_id = b.id) AS people,
                   (SELECT COUNT(*) FROM marks m WHERE m.bill_id = b.id) AS marks
            FROM bills b ORDER BY b.updated DESC""").fetchall()
    return [{"slug": r["slug"], "title": r["title"], "date": r["date"], "total": r["total"], "created": r["created"],
             "updated": r["updated"], "locked": not r["allow_edits"], "has_owner": bool(r["has_owner"]),
             "lines": len(json.loads(r["lines"])), "people": r["people"], "marks": r["marks"],
             "photo_bytes": _size(photo_path(r["id"]))} for r in rows]


def admin_stats() -> dict:
    import shutil
    with conn() as c:
        n = c.execute("SELECT COUNT(*) FROM bills").fetchone()[0]
        people = c.execute("SELECT COUNT(*) FROM people").fetchone()[0]
        marks = c.execute("SELECT COUNT(*) FROM marks").fetchone()[0]
        first, last = c.execute("SELECT MIN(created), MAX(created) FROM bills").fetchone()
    photos = sum(_size(os.path.join(PHOTOS, f)) for f in os.listdir(PHOTOS)) if os.path.isdir(PHOTOS) else 0
    db = sum(_size(os.path.join(DATA, f)) for f in ("cuenta.db", "cuenta.db-wal", "cuenta.db-shm"))
    du = shutil.disk_usage(DATA)
    return {"bills": n, "people": people, "marks": marks, "photo_bytes": photos, "db_bytes": db,
            "disk_free": du.free, "disk_total": du.total, "first": first, "last": last}


def delete_bills(slugs: list[str]) -> int:
    """Removes bills with their people, marks and photos. Returns how many bills existed."""
    ids = []
    with write_tx() as c:
        for slug in slugs:
            r = c.execute("SELECT id FROM bills WHERE slug=?", (slug,)).fetchone()
            if r:
                ids.append(r["id"])
                c.execute("DELETE FROM marks WHERE bill_id=?", (r["id"],))
                c.execute("DELETE FROM people WHERE bill_id=?", (r["id"],))
                c.execute("DELETE FROM bills WHERE id=?", (r["id"],))
    for i in ids:
        try:
            os.remove(photo_path(i))
        except FileNotFoundError:
            pass
    return len(ids)


def idle_slugs(days: int) -> list[str]:
    cutoff = time.time() - days * 86400
    with conn() as c:
        return [r["slug"] for r in c.execute("SELECT slug FROM bills WHERE updated < ?", (cutoff,))]
