"""Deterministic checks — no LLM anywhere in here."""
from __future__ import annotations

import difflib
import hashlib
import re

CH = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
IFSC_RE = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")


def gstin_check_char(g14: str) -> str:
    total = 0
    for i, c in enumerate(g14):
        p = CH.index(c) * (1 if i % 2 == 0 else 2)
        total += p // 36 + p % 36
    return CH[(36 - total % 36) % 36]


def make_gstin(state_code: str, pan: str, entity: str = "1") -> str:
    g14 = f"{state_code}{pan}{entity}Z"
    return g14 + gstin_check_char(g14)


def gstin_format_ok(g: str | None) -> bool:
    return bool(g) and bool(GSTIN_RE.match(g))


def gstin_checksum_ok(g: str) -> bool:
    return gstin_check_char(g[:14]) == g[14]


def ifsc_format_ok(code: str | None) -> bool:
    return bool(code) and bool(IFSC_RE.match(code))


def norm_invoice_no(s: str | None) -> str | None:
    """INV-0042, INV/42, inv 42 -> INV42."""
    if not s:
        return None
    s = re.sub(r"[^A-Z0-9]", "", s.upper())
    s = re.sub(r"(?<![0-9])0+(?=[0-9])", "", s)
    return s or None


_SUFFIX = re.compile(r"\b(pvt|private|ltd|limited|llp|inc|co|company|corp|corporation)\b")


def normalize_name(name: str | None) -> str:
    if not name:
        return ""
    s = name.lower().replace("&", " and ")
    s = re.sub(r"[.,()\-/]", " ", s)
    s = _SUFFIX.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


def skeleton(name: str | None) -> str:
    """Confusable-folded form: l/I/1/| collapse, 0->o, rn->m, vv->w.
    Applied before lowercasing so 'I' (capital i) folds to 'l'."""
    if not name:
        return ""
    s = name.replace("I", "l").replace("|", "l").replace("1", "l")
    s = normalize_name(s)
    s = s.replace("0", "o").replace("rn", "m").replace("vv", "w")
    return s.replace(" ", "")


def similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


def domain_of(email: str | None) -> str | None:
    if not email or "@" not in email:
        return None
    return email.rsplit("@", 1)[1].strip().strip(">").lower()


def edit_distance(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def domain_core(d: str) -> str:
    """acme-supplies.co -> acmesupplies ; acmesupplies.com -> acmesupplies."""
    first = d.split(".")[0]
    return re.sub(r"[^a-z0-9]", "", first)


def content_hash(lines: list[dict]) -> str:
    norm = sorted((normalize_name(l["item"]), float(l["qty"]), int(l["unit_price"])) for l in lines)
    return hashlib.sha256(repr(norm).encode()).hexdigest()


PRESSURE_WORDS = ["urgent", "urgently", "immediately", "today", "account frozen", "frozen",
                  "confidential", "asap", "do not call", "within 24 hours", "at the earliest"]


def pressure_terms(text: str | None) -> list[str]:
    t = (text or "").lower()
    return [w for w in PRESSURE_WORDS if re.search(r"\b" + re.escape(w) + r"\b", t)]
