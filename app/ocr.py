"""Ticket photo -> list of lines. RapidOCR (PaddleOCR models on onnxruntime, CPU) finds and reads text pieces;
this module rebuilds the printed rows from them (the photo is usually tilted, so pieces are de-skewed first),
parses `qty  description  price  amount` and checks the arithmetic against the ticket's own total.
"""
import io
import math
import os
import re
import statistics
import tempfile

import numpy as np
import onnxruntime
from PIL import Image, ImageOps
from rapidocr_onnxruntime import RapidOCR

import vocab

_engine = None
MAX_SIDE = 1600
OCR_THREADS = 2          # 2 threads read a ticket as fast as 4 (measured) and leave the rest of the server alone
onnxruntime.disable_telemetry_events()   # onnxruntime reports usage to Microsoft by default: off
Image.MAX_IMAGE_PIXELS = 40_000_000   # refuse decompression bombs (a tiny file that expands to gigabytes)

MONEY = r"\d{1,5}[.,]\d{2}"
SKIP = re.compile(r"\b(TOTAL|SUBTOTAL|BASE|I\.?V\.?A|IMPONIBLE|CAMBIO|ENTREGADO|TARJETA|EFECTIVO|PROPINA|CIF|C\.I\.F|TEL|TELEF|FECHA|MESA|CAJER|ATENDI|FACTURA|GRACIAS|IMPORTE|PRECIO|DESCRIP)", re.I)


def engine() -> RapidOCR:
    global _engine
    if _engine is None:
        # same config as the package default, only with fewer onnxruntime threads
        import rapidocr_onnxruntime as pkg
        base = os.path.dirname(pkg.__file__)
        cfg = open(os.path.join(base, "config.yaml"), encoding="utf-8").read()
        cfg = re.sub(r"(intra_op_num_threads: &intra_nums) -1", rf"\1 {OCR_THREADS}", cfg)
        cfg = cfg.replace("model_path: models/", "model_path: " + base.replace("\\", "/") + "/models/")
        fd, path = tempfile.mkstemp(suffix=".yaml")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(cfg)
        try:
            _engine = RapidOCR(config_path=path)
        finally:
            os.remove(path)
    return _engine


def _num(s: str) -> float:
    return float(s.replace(",", "."))


def _clean_digits(tok: str) -> str:
    """OCR often confuses look-alikes inside numbers (O/0, l/1, '/' for ',')."""
    t = tok.translate(str.maketrans({"O": "0", "o": "0", "l": "1", "I": "1", "/": ",", "S": "5"}))
    return t


def prepare(data: bytes) -> tuple[np.ndarray, bytes]:
    """Orient + shrink the photo. Returns (BGR array for the OCR, the same picture as JPEG for storage:
    line positions are fractions of THIS image, so the stored photo must be exactly it)."""
    im = Image.open(io.BytesIO(data))
    im.draft("RGB", (MAX_SIDE * 2, MAX_SIDE * 2))      # JPEG: decode at reduced size when the photo is huge
    im = ImageOps.exif_transpose(im).convert("RGB")
    if max(im.size) > MAX_SIDE:
        im.thumbnail((MAX_SIDE, MAX_SIDE), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=86, optimize=True)
    return np.array(im)[:, :, ::-1], buf.getvalue()  # BGR for OpenCV-style engines


def _rows(res, width: int, height: int) -> list[tuple[str, float, float]]:
    """Group the text pieces into printed rows. Pieces are rotated by the median text angle first."""
    pieces = []
    angles = []
    for box, txt, conf in res:
        (x0, y0), (x1, y1) = box[0], box[1]
        angles.append(math.atan2(y1 - y0, x1 - x0))
        pieces.append((box, str(txt)))
    ang = statistics.median(angles) if angles else 0.0
    ca, sa = math.cos(-ang), math.sin(-ang)
    items = []
    for box, txt in pieces:
        cx = sum(p[0] for p in box) / 4
        cy = sum(p[1] for p in box) / 4
        h = math.dist(box[0], box[3])
        # rotate the centre around the origin by -angle (de-skew)
        items.append((cx * ca - cy * sa, cx * sa + cy * ca, h, txt, cy))
    if not items:
        return []
    med_h = statistics.median(i[2] for i in items)
    items.sort(key=lambda i: i[1])
    rows: list[list] = []
    for it in items:
        if rows and abs(it[1] - statistics.mean(r[1] for r in rows[-1])) < 0.55 * med_h:
            rows[-1].append(it)
        else:
            rows.append([it])
    out = []
    for r in rows:
        r.sort(key=lambda i: i[0])
        text = " ".join(i[3].strip() for i in r)
        cy = statistics.mean(i[4] for i in r) / height
        out.append((text, round(cy, 4), round(med_h * 1.5 / height, 4)))
    return out


def _fix_numbers(line: str) -> str:
    # tokens that look numeric but carry letters/slashes: fix them. Only tokens made of digits + look-alikes.
    toks = []
    for t in line.split():
        if re.fullmatch(r"[\dOolIS/.,]{3,9}", t) and re.search(r"\d", t) and re.search(r"[,./]", t):
            t = _clean_digits(t)
        toks.append(t)
    return " ".join(toks)


QTY_ROW = re.compile(rf"^\s*(\d{{1,3}}(?:[.,]\d{{1,2}})?)\s+(.+?)\s+({MONEY})(?:\s+({MONEY}))?\s*$")
ITEM_ROW = re.compile(rf"^\s*(.+?)\s+((?:{MONEY}\s*){{1,2}})$")      # "CON LECHE 2,05 2,05" (quantity not read)
END_ROW = re.compile(r"^\W*(BASE|TOTAL|SUBTOTAL|I\.?V\.?A)\b", re.I)     # the item list is over
TAX_LETTER = re.compile(rf"({MONEY})\s*[A-E]\s*$")                        # Lidl, Dia...: "2,99 B" (the VAT band letter printed after the amount)
# "2 X 3,99 EUR": its own row under the item it multiplies. The OCR sometimes drops the comma ("2X399EUR#A"):
# a separator-less price is only accepted when quantity x price equals the amount of the item above (see parse_rows)
MULT_ROW = re.compile(rf"^\W*(\d{{1,3}})\s*[xX×]\s*({MONEY}|\d{{3,5}})\s*(?:EUR|€)?[^\d]{{0,8}}$")


DATE_RE = re.compile(r"(?<!\d)(\d{1,2})\s*[/.\-]\s*(\d{1,2})\s*[/.\-]\s*(\d{4}|\d{2})(?!\d)")


def _date(rows: list[str]) -> str | None:
    """Ticket date as ISO yyyy-mm-dd (dd/mm/yyyy, dd-mm-yy, dd.mm.yyyy...), or None when none is readable."""
    import datetime as dt
    today = dt.date.today()
    for r in rows:
        # look-alikes inside a date: 1O/1O/2O26 -> 10/10/2026
        t = re.sub(r"(?<=[\d/.\-])[Oo](?=[\d/.\-])|(?<=\d)[Oo]", "0", r)
        t = re.sub(r"(?<=[\d/.\-])[lI](?=[\d/.\-])", "1", t)
        for m in DATE_RE.finditer(t):
            d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if y < 100:
                y += 2000
            try:
                date = dt.date(y, mo, d)
            except ValueError:
                continue
            if dt.date(2000, 1, 1) <= date <= today + dt.timedelta(days=2):   # a printed future date is a misread
                return date.isoformat()
    return None


def _title(rows: list[str]) -> str:
    """Name of the place: first printed row with real words (e.g. 'COPPOLA BILBAO' -> 'Coppola Bilbao')."""
    for r in rows[:4]:
        t = re.sub(r"[^A-Za-zÁÉÍÓÚÑáéíóúñ0-9 '&.-]", "", r).strip()
        if len(re.findall(r"[A-Za-zÁÉÍÓÚÑáéíóúñ]", t)) >= 4 and not SKIP.search(t):
            return t[:40].title()
    return ""


def _last_money(text: str) -> float | None:
    m = re.findall(MONEY, text)
    return _num(m[-1]) if m else None


def _find_total(rows: list[str], pos: list[tuple[float, float]]) -> float | None:
    # 1) a TOTAL row that carries the amount itself
    total = None
    for r in rows:
        if re.search(r"\bTOTAL\b", r, re.I) and not re.search(r"SUB|BASE", r, re.I):
            v = _last_money(r)
            if v is not None:
                total = v
    if total is not None:
        return total
    # 2) "TOTAL:" and its amount printed in a bigger font end up as two neighbouring rows
    for i, r in enumerate(rows):
        if re.search(r"\bTOTAL\b", r, re.I) and not re.search(r"SUB|BASE", r, re.I):
            for j in (i + 1, i - 1):
                if 0 <= j < len(rows) and abs(pos[j][0] - pos[i][0]) < 0.045:
                    v = _last_money(rows[j])
                    if v is not None:
                        return v
    # 3) tax table: "BASE TIPO CUOTA TOTAL" followed by "8,82 10% 0,88 9,70"
    for i, r in enumerate(rows):
        if re.search(r"\bBASE\b", r, re.I) and re.search(r"\bTOTAL\b", r, re.I) and i + 1 < len(rows):
            v = _last_money(rows[i + 1])
            if v is not None:
                return v
    return None


def parse_rows(raw: list[tuple[str, float, float]]) -> dict:
    rows = [_fix_numbers(TAX_LETTER.sub(r"\1", t.strip())) for t, _, _ in raw]
    pos = [(y, h) for _, y, h in raw]
    total = _find_total(rows, pos)
    lines = []
    single = []                                          # rows "3 AGUA 9,60" whose lone price may be the line total
    for r, (y, hh) in zip(rows, pos):
        if lines and END_ROW.match(r):
            break                                        # BASE / IVA / TOTAL ...: nothing below is an item
        if SKIP.search(r) or DATE_RE.search(r):
            continue
        mm = MULT_ROW.match(r)
        if mm and lines:                                 # "2 X 3,99 EUR": quantity and unit price of the item right above
            qty, raw = float(mm.group(1)), mm.group(2)
            has_sep = bool(re.search(r"[.,]", raw))
            price = _num(raw) if has_sep else int(raw) / 100
            if has_sep or abs(qty * price - lines[-1]["amount"]) < 0.011:
                lines[-1].update(qty=qty, price=price)
                lines[-1]["ok"] = abs(qty * price - lines[-1]["amount"]) < 0.011
            continue
        m = QTY_ROW.match(r)
        if m:
            qty, desc, p1, p2 = _num(m.group(1)), m.group(2).strip(), _num(m.group(3)), m.group(4)
            if p2 is not None:
                price, amount = p1, _num(p2)
            else:           # "2 CAFE 2,50": a single price is the unit price (unless the total says otherwise, below)
                price, amount = p1, round(qty * p1, 2)
                if qty > 1:
                    single.append(len(lines))
        else:
            m = ITEM_ROW.match(r)
            if not m:
                continue
            desc = m.group(1).strip()
            money = [_num(x) for x in re.findall(MONEY, m.group(2))]
            price, amount = money[0], money[-1]
            qty = 1.0
            if len(money) == 2 and price > 0:       # "price amount": the quantity is what links them
                q = amount / price
                if q >= 1 and abs(q - round(q)) < 0.02:
                    qty = float(round(q))
        if not re.search(r"[A-Za-zÁÉÍÓÚÑáéíóúñ]{2}", desc):
            continue
        desc = vocab.fix_name(desc)                  # "PaTmera" -> "Palmera" (see vocab.py)
        ok = abs(qty * price - amount) < 0.011
        lines.append({"qty": qty, "name": desc, "price": price, "amount": amount, "ok": ok, "y": y, "h": hh})
    s = round(sum(l["amount"] for l in lines), 2)
    if single and total is not None and abs(total - s) >= 0.011:
        # Some tickets print "Uds. Producto Importe": the lone price is the whole line, not the unit price.
        alt = round(s - sum(lines[i]["amount"] for i in single) + sum(lines[i]["price"] for i in single), 2)
        if abs(total - alt) < 0.011:
            for i in single:
                l = lines[i]
                l["amount"], l["price"], l["ok"] = l["price"], round(l["price"] / l["qty"], 2), True
            s = alt
    return {"lines": lines, "total": total, "sum": s, "title": _title(rows), "date": _date(rows),
            "matches": total is not None and abs(total - s) < 0.011}


def _read(img: np.ndarray) -> tuple[dict, int]:
    """One OCR pass over an image -> (parsed ticket, number of text pieces the detector found)."""
    res, _ = engine()(img)
    rows = _rows(res or [], img.shape[1], img.shape[0])
    return parse_rows(rows), len(res or [])


def _jpeg(img: np.ndarray) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(img[:, :, ::-1]).save(buf, "JPEG", quality=86, optimize=True)
    return buf.getvalue()


def scan(data: bytes) -> dict:
    img, jpeg = prepare(data)
    out, pieces = _read(img)
    rotation = 0
    if len(out["lines"]) < 2:
        # Sideways or upside-down photo (the phone's sensor orientation, a missing EXIF tag...): the text is read
        # but the rows come out scrambled. Try the other orientations and keep the best one; the stored photo
        # (and the line positions that point into it) are then the corrected picture.
        best, best_img, best_rot = out, None, 0
        for k in (1, 3, 2):                                         # 90 left, 90 right, 180
            rimg = np.ascontiguousarray(np.rot90(img, k))
            cand, _ = _read(rimg)
            if len(cand["lines"]) > len(best["lines"]):
                best, best_img, best_rot = cand, rimg, k * 90
            if len(best["lines"]) >= 2:
                break
        if best_img is not None:
            out, jpeg, rotation = best, _jpeg(best_img), best_rot
    print(f"[scan] image={img.shape[1]}x{img.shape[0]} mean={img.mean():.0f} std={img.std():.0f} pieces={pieces} lines={len(out['lines'])} total={'yes' if out['total'] is not None else 'no'} rotated={rotation}", flush=True)
    out["photo"] = jpeg
    return out
