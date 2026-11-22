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
    "PIPELINE_ERROR": HOLD,
}


def reason(code: str, message: str, fact_ids=None, stage: str = "", **extra) -> dict:
    sev = SEVERITY.get(code, OK)
    r = {"code": code, "severity": sev, "message": message, "fact_ids": list(fact_ids or []), "stage": stage}
    r.update(extra)
    return r


def passed(check: str, message: str, fact_ids=None, stage: str = "") -> dict:
    """A check that passed — kept so even ELIGIBLE invoices show their evidence."""
    return {"code": check, "severity": OK, "message": message, "fact_ids": list(fact_ids or []), "stage": stage}
