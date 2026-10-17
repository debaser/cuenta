"""Admin password: salted scrypt (standard library, no extra dependency). Stored as 'scrypt$n$r$p$salt$hash' (hex)."""
import hashlib
import hmac
import os

N, R, P = 2**15, 8, 1          # ~32 MB and ~100 ms per check
MAXMEM = 96 * 1024 * 1024


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    h = hashlib.scrypt(password.encode(), salt=salt, n=N, r=R, p=P, maxmem=MAXMEM, dklen=32)
    return f"scrypt${N}${R}${P}${salt.hex()}${h.hex()}"


def verify(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, h = stored.split("$")
        if scheme != "scrypt":
            return False
        calc = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p), maxmem=MAXMEM, dklen=32)
        return hmac.compare_digest(calc.hex(), h)
    except (ValueError, TypeError):
        return False
