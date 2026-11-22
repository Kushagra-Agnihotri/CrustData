"""Money helpers. All money is stored as integer paise — never floats."""
from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation


def to_paise(value) -> int | None:
    """Parse '1,20,000.00', 'Rs. 1,20,000/-', '₹45,000', 120000 -> paise."""
    if value is None:
        return None
    if isinstance(value, int):
        return value * 100
    s = str(value)
    s = re.sub(r"(?i)(rs\.?|inr|₹|/-)", "", s).replace(",", "").strip()
    if not s:
        return None
    try:
        d = Decimal(s)
    except InvalidOperation:
        return None
    return int((d * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def round_rupee(paise: int) -> int:
    """Round paise to whole rupees."""
    return int((Decimal(paise) / 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)) * 100


def _group_indian(n: int) -> str:
    s = str(n)
    if len(s) <= 3:
        return s
    last3, rest = s[-3:], s[:-3]
    parts = []
    while len(rest) > 2:
        parts.insert(0, rest[-2:])
        rest = rest[:-2]
    if rest:
        parts.insert(0, rest)
    return ",".join(parts) + "," + last3


def inr(paise: int | None, decimals: bool = False) -> str:
    """Format paise as ₹1,20,000 (Indian grouping)."""
    if paise is None:
        return "—"
    neg = paise < 0
    paise = abs(int(paise))
    rupees, p = divmod(paise, 100)
    out = "₹" + _group_indian(rupees)
    if decimals:
        out += f".{p:02d}"
    return ("-" if neg else "") + out
