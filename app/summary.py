"""The week's gist: what was evaluated, what is planned to be paid, what was stopped and why.

Pure read over the invoices / instalments / payment_runs tables.
"""
from __future__ import annotations

from collections import Counter

from app import scheduler
from app.db import q, q1, unj
from app.reasons import BLOCK, HOLD

# Codes that mean "someone may be trying to get paid who shouldn't be" (as opposed to clerical holds).
FRAUD_CODES = {"BANK_CHANGE_FRAUD", "SENDER_DOMAIN_MISMATCH", "BANK_DETAILS_CHANGED", "REPLY_TO_MISMATCH",
               "INVALID_GSTIN", "GSTIN_MISMATCH", "PAN_MISMATCH", "UNVERIFIABLE_VENDOR", "BENEFICIARY_NAME_MISMATCH"}
DUP_CODES = {"DUPLICATE_INVOICE", "POSSIBLE_DUPLICATE"}
TIER_LABEL = {0: "Tier 0 · must pay", 1: "Tier 1 · discount", 2: "Tier 2 · priority"}


def _codes(inv) -> list[dict]:
    return unj(inv["reasons"], []) or []


def is_fraud(inv) -> bool:
    for r in _codes(inv):
        if r["code"] in FRAUD_CODES:
            return True
        if r["code"] == "VENDOR_NOT_APPROVED" and r.get("lookalike_of"):
            return True
    return False


def latest_run(conn) -> dict | None:
    return scheduler.current_run(conn) or q1(conn, "SELECT * FROM payment_runs ORDER BY created_at DESC LIMIT 1")


def week_summary(conn) -> dict:
    invs = q(conn, "SELECT * FROM invoices ORDER BY rowid")
    run = latest_run(conn)
    insts = q(conn, "SELECT * FROM instalments WHERE run_id=? AND status NOT IN ('CANCELLED','FAILED')", (run["id"],)) if run else []
    by_inv = {}
    for x in insts:
        by_inv.setdefault(x["invoice_id"], []).append(x)
    run_summary = unj(run["summary"], {}) if run else {}

    real = [i for i in invs if i["doc_kind"] == "invoice"]
    dec = {k: [i for i in invs if i["decision"] == k] for k in ("PAY_ELIGIBLE", "HOLD", "BLOCK")}
    fraud = [i for i in invs if is_fraud(i)]
    dups = [i for i in invs if any(r["code"] in DUP_CODES for r in _codes(i))]
    fraud_vendors = {i["vendor_id"] for i in invs if i["vendor_id"] and any(r["code"] == "BANK_CHANGE_FRAUD" for r in _codes(i))}
    protected = sum(i["total"] or 0 for i in real if i["vendor_id"] in fraud_vendors) + \
        sum(i["total"] or 0 for i in fraud if i["doc_kind"] == "invoice" and i["vendor_id"] not in fraud_vendors)

    tiers = {t: {"count": 0, "amount": 0, "discount": 0} for t in (0, 1, 2)}
    for x in insts:
        tiers[x["tier"]]["count"] += 1
        tiers[x["tier"]]["amount"] += x["amount"]
        tiers[x["tier"]]["discount"] += x["discount_amount"]
    full = sum(1 for iid, xs in by_inv.items()
               if (inv := next((i for i in invs if i["id"] == iid), None)) and sum(x["amount"] + x["discount_amount"] for x in xs) >= (inv["outstanding"] or inv["total"] or 0))

    reasons = Counter(r["code"] for i in invs for r in _codes(i) if r["severity"] in (BLOCK, HOLD))
    msme = sum(x["amount"] for x in insts if x["tier"] == 0 and "MSME_DEADLINE" in (x["justification"] or ""))
    cost = q1(conn, "SELECT COALESCE(SUM(est_cost_inr),0) c, COALESCE(SUM(credits),0) cr FROM calls")
    deferred = {d["invoice_id"]: d for d in run_summary.get("deferred", [])}
    dup_ids = {i["id"] for i in dups}

    rows = []
    for i in invs:
        xs = by_inv.get(i["id"], [])
        top = next((r["message"] for r in sorted(_codes(i), key=lambda r: r["severity"] != BLOCK) if r["severity"] in (BLOCK, HOLD)), None)
        if xs:
            outcome = "Pay " + ("in full" if sum(x["amount"] + x["discount_amount"] for x in xs) >= (i["outstanding"] or i["total"] or 0) else "part now")
        elif i["id"] in deferred:
            outcome = "Deferred — " + deferred[i["id"]]["why"]
        elif i["decision"] == "PAY_ELIGIBLE":
            outcome = "Eligible — not planned yet"
        else:
            outcome = {"HOLD": "Held for review", "BLOCK": "Blocked"}.get(i["decision"], "—")
        rows.append({"invoice": i, "planned": sum(x["amount"] for x in xs),
                     "tier": xs[0]["tier"] if xs else (deferred[i["id"]]["tier"] if i["id"] in deferred else None),
                     "fraud": is_fraud(i), "duplicate": i["id"] in dup_ids, "outcome": outcome, "top_reason": top or "all checks passed"})

    return {
        "docs": len(invs), "invoices": len(real), "bank_change": len(invs) - len(real),
        "value": sum(i["total"] or 0 for i in real),
        "eligible": len(dec["PAY_ELIGIBLE"]), "held": len(dec["HOLD"]), "blocked": len(dec["BLOCK"]),
        "eligible_value": sum(i["total"] or 0 for i in dec["PAY_ELIGIBLE"]),
        "held_value": sum(i["total"] or 0 for i in dec["HOLD"] if i["doc_kind"] == "invoice"),
        "blocked_value": sum(i["total"] or 0 for i in dec["BLOCK"] if i["doc_kind"] == "invoice"),
        "fraud": len(fraud), "fraud_protected": protected,
        "duplicates": len(dups), "duplicate_value": sum(i["total"] or 0 for i in dups),
        "run": run, "run_status": run["status"] if run else None,
        "payments": len(insts), "planned": sum(x["amount"] for x in insts), "full_payments": full,
        "part_payments": len(by_inv) - full, "deferred": len(deferred),
        "spendable": (run_summary.get("cash") or {}).get("spendable") if run else None,
        "tiers": tiers, "discounts": sum(t["discount"] for t in tiers.values()), "msme_on_time": msme,
        "reasons": reasons.most_common(), "cost_inr": cost["c"], "credits": cost["cr"],
        "cost_per_invoice": cost["c"] / (len(real) or 1), "rows": rows,
    }
