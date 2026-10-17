"""Abuse protection shared by the public API and the admin panel: sliding-window limiter per (bucket, ip), the client IP,
and a short in-memory list of recent abuse events (shown in the admin panel, never stored)."""
import ipaddress
import time
from collections import defaultdict, deque

from fastapi import Request

hits: dict[tuple[str, str], deque] = defaultdict(deque)
_logged: dict[tuple[str, str], float] = {}
events: deque = deque(maxlen=60)          # (timestamp, ip, kind) of the latest limit hits
state = {"paused": False}                 # admin kill switch for scanning (persisted in the DB by the admin panel)


def abuse(ip: str, bucket: str) -> None:
    """One log line per IP and kind at most every minute, so abuse is visible without flooding the log."""
    now = time.time()
    if now - _logged.get((ip, bucket), 0) > 60:
        _logged[(ip, bucket)] = now
        if len(_logged) > 2000:
            _logged.clear()
        events.append((now, ip, bucket))
        print(f"[abuse] {bucket} limit hit by {ip}", flush=True)


def allow(ip: str, bucket: str, limit: tuple[int, int], record: bool = True) -> bool:
    """Sliding-window limiter per (bucket, ip)."""
    n, window = limit
    now = time.time()
    if len(hits) > 2000:                                    # forget idle IPs so the table cannot grow forever
        for k in [k for k, q in hits.items() if not q or now - q[-1] > 86400]:
            del hits[k]
    q = hits[(bucket, ip)]
    while q and now - q[0] > window:
        q.popleft()
    if len(q) >= n:
        abuse(ip, bucket)
        return False
    if record:
        q.append(now)
    return True


def count(bucket: str, ip: str, window: int) -> int:
    now = time.time()
    return sum(1 for t in hits.get((bucket, ip), ()) if now - t <= window)


def client_ip(request: Request) -> str:
    # nginx always overwrites X-Real-IP with the visitor's address (from Cloudflare); client-supplied copies come after
    raw = request.headers.get("x-real-ip") or request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "?")
    try:
        ip = ipaddress.ip_address(raw.strip())
    except ValueError:
        return "?"
    if ip.version == 6:       # one household/attacker owns a whole /64: count the prefix, not the single address
        return str(ipaddress.ip_network(f"{ip}/64", strict=False).network_address) + "/64"
    return str(ip)
