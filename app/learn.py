"""What the local reader learns from the AI reader. When the AI had to step in and its answer is the one that was kept,
every line whose name differs from what the local reader produced (and that can be paired with certainty, see pairs())
is remembered as "local text -> AI text"; the next ticket that reads the same text gets the corrected name straight
away, without asking the AI. The pairs are listed (and can be deleted) in the admin panel. The whole local + AI readings
are also kept per bill (`readings`), next to the stored photo: real examples to measure or retrain a reader with later."""
from collections import Counter
from difflib import SequenceMatcher

import store
import vocab

MIN_SIMILARITY = 0.5      # a name that looks nothing like the one it replaces is a different item, not a misreading


def load() -> None:
    vocab.LEARNED.clear()
    vocab.LEARNED.update({r["src"]: r["dst"] for r in store.learned_all()})


def _unique(lines: list[dict]) -> dict:
    """Lines whose (quantity, amount) no other line shares: the only ones that can be paired without guessing."""
    keys = [(l["qty"], round(l["amount"], 2)) for l in lines]
    count = Counter(keys)
    return {k: l for k, l in zip(keys, lines) if count[k] == 1}


def pairs(local_lines: list[dict], ai_lines: list[dict]) -> list[tuple[str, str]]:
    mine, theirs = _unique(local_lines), _unique(ai_lines)
    out = []
    for k, l in mine.items():
        m = theirs.get(k)
        if not m:
            continue
        src, dst = " ".join((l["name"] or "").split()), " ".join((m["name"] or "").split())
        if not src or not dst or len(dst) > 60 or vocab.key(src) == vocab.key(dst):
            continue
        if SequenceMatcher(None, vocab.key(src), vocab.key(dst)).ratio() < MIN_SIMILARITY:
            continue
        if src.isupper():                        # tickets print in capitals: keep the ticket's look
            dst = dst.upper()
        out.append((vocab.key(src), dst))
    return out


def from_ai(slug: str, local: dict, ai: dict) -> None:
    """Best effort, runs after the bill exists: never lets a failure reach the person who scanned."""
    try:
        store.save_reading(slug, local, ai)
        p = pairs(local["lines"], ai["lines"])
        if p:
            store.learn_pairs(p)
            load()
            print(f"[learn] {len(p)} name(s) learned from {slug}", flush=True)
    except Exception as e:
        print(f"[learn] failed: {e!r}", flush=True)
