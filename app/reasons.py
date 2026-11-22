"""Reason codes: the single place that maps a code to its severity.

decide.py only reads severities from here, so the same facts always give the
same decision regardless of how an invoice was worded.
"""
from __future__ import annotations

BLOCK, HOLD, OK = "BLOCK", "HOLD", "OK"

SEVERITY: dict[str, str] = {
    # BLOCK
    "INVALID_GSTIN": BLOCK,
    "BANK_CHANGE_FRAUD": BLOCK,
    "VENDOR_BLOCKED": BLOCK,
    # HOLD
    "EXTRACTION_FAILED": HOLD,
    "ARITHMETIC_MISMATCH": HOLD,
    "GSTIN_FORMAT": HOLD,
    "INVOICE_DATE_FUTURE": HOLD,
    "INVOICE_TOO_OLD": HOLD,
    "IFSC_FORMAT": HOLD,
    "IFSC_NOT_FOUND": HOLD,
    "PAN_MISMATCH": HOLD,
    "GSTIN_MISMATCH": HOLD,
    "VENDOR_NOT_APPROVED": HOLD,
    "UNVERIFIABLE_VENDOR": HOLD,
    "VENDOR_ON_HOLD": HOLD,
    "NO_PO": HOLD,
    "PO_VENDOR_MISMATCH": HOLD,
    "OVER_BILLED": HOLD,
    "NOT_RECEIVED": HOLD,
    "PRICE_VARIANCE": HOLD,
    "UNMATCHED_LINE": HOLD,
    "DUPLICATE_INVOICE": HOLD,
    "POSSIBLE_DUPLICATE": HOLD,
    "BANK_DETAILS_CHANGED": HOLD,
    "BANK_CHANGE_REQUEST": HOLD,
    "SENDER_DOMAIN_MISMATCH": HOLD,
    "REPLY_TO_MISMATCH": HOLD,
    "SENDER_NOT_VERIFIED_AT_VENDOR": HOLD,
    "PRESSURE_LANGUAGE": HOLD,
    "IFSC_LOCATION_MISMATCH": HOLD,
    "BENEFICIARY_NAME_MISMATCH": HOLD,
    "SOURCE_UNAVAILABLE": HOLD,
    "RUN_CEILING_REACHED": HOLD,
    "LOW_TRUST": HOLD,
    "ADVERSE_NEWS": HOLD,
    "PIPELINE_ERROR": HOLD,
}


# Display/decision order: the most serious thing a reviewer must see comes first.
PRIORITY = [
    "BANK_CHANGE_FRAUD", "INVALID_GSTIN", "VENDOR_BLOCKED",
    "DUPLICATE_INVOICE", "BANK_CHANGE_REQUEST", "SENDER_DOMAIN_MISMATCH", "REPLY_TO_MISMATCH", "BANK_DETAILS_CHANGED",
    "VENDOR_NOT_APPROVED", "UNVERIFIABLE_VENDOR", "GSTIN_MISMATCH", "PAN_MISMATCH", "VENDOR_ON_HOLD", "ADVERSE_NEWS",
    "SENDER_NOT_VERIFIED_AT_VENDOR", "PRESSURE_LANGUAGE", "POSSIBLE_DUPLICATE", "OVER_BILLED", "NOT_RECEIVED",
    "PRICE_VARIANCE", "UNMATCHED_LINE", "NO_PO", "PO_VENDOR_MISMATCH", "ARITHMETIC_MISMATCH", "BENEFICIARY_NAME_MISMATCH",
    "IFSC_NOT_FOUND", "IFSC_LOCATION_MISMATCH", "IFSC_FORMAT", "GSTIN_FORMAT", "INVOICE_DATE_FUTURE", "INVOICE_TOO_OLD",
    "LOW_TRUST", "SOURCE_UNAVAILABLE", "RUN_CEILING_REACHED", "EXTRACTION_FAILED", "PIPELINE_ERROR",
]
_SEV_RANK = {BLOCK: 0, HOLD: 1, OK: 2}


def sort_reasons(reasons: list[dict]) -> list[dict]:
    """BLOCK first, then HOLD by PRIORITY, then passed checks (stable within each group)."""
    pr = {c: i for i, c in enumerate(PRIORITY)}

    def key(x):
        i, r = x
        sev = r.get("severity")
        return (_SEV_RANK.get(sev, 3), pr.get(r["code"], len(pr)) if sev != OK else 0, i)
    return [r for _, r in sorted(enumerate(reasons), key=key)]


def reason(code: str, message: str, fact_ids=None, stage: str = "", **extra) -> dict:
    sev = SEVERITY.get(code, OK)
    r = {"code": code, "severity": sev, "message": message, "fact_ids": list(fact_ids or []), "stage": stage}
    r.update(extra)
    return r


def passed(check: str, message: str, fact_ids=None, stage: str = "") -> dict:
    """A check that passed — kept so even ELIGIBLE invoices show their evidence."""
    return {"code": check, "severity": OK, "message": message, "fact_ids": list(fact_ids or []), "stage": stage}
