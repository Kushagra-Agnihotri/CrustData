"""Liquidity-aware payment run (BUILD_PROMPT §10).

spendable = bank balance − safety buffer + confident expected receipts
Tier 0: MSME legal deadline (micro/small, non-trader) within horizon, or critical supplier overdue — paid in full
Tier 1: early-payment discount whose annualised return beats our cost of cash — paid in full (less discount)
Tier 2: everything else due within horizon, in priority order, in instalments

Greedy on purpose: every number must be explainable in one sentence to the approver.
"""
from __future__ import annotations

import datetime as dt
import json

from app import config
from app.db import j, new_id, now_iso, q, q1, tx, unj
from app.ledger import add_event, add_fact
from app.money import inr, round_rupee


def _d(s):
    return dt.date.fromisoformat(s) if s else None


def _fmt_date(d: dt.date) -> str:
    return d.strftime("%d %b %Y").lstrip("0")


def bank_position() -> dict:
    return json.loads((config.DATA / "seed" / "bank_position.json").read_text(encoding="utf-8"))


def cash_position(conn) -> dict:
    bp = bank_position()
    balance = bp["balance"] * 100
    buffer_items = [{"label": k, "amount": v * 100} for k, v in bp["safety_buffer"].items()]
    buffer = sum(i["amount"] for i in buffer_items)
    horizon_end = config.RUN_DATE + dt.timedelta(days=config.HORIZON_DAYS)
    recs = q(conn, "SELECT r.*, c.legal_name FROM receivables r JOIN customers c ON c.id=r.customer_id WHERE r.status IN ('OPEN','OVERDUE') ORDER BY due_date")
    counted, later = [], []
    for r in recs:
        due = _d(r["due_date"])
        row = {"customer": r["legal_name"], "invoice_no": r["invoice_no"], "amount": r["amount"], "due_date": r["due_date"],
               "confidence": r["confidence"], "status": r["status"]}
        if r["status"] == "OPEN" and due <= horizon_end and (r["confidence"] or 0) >= config.RECEIPT_CONFIDENCE_MIN:
            row["counted"] = round_rupee(int(r["amount"] * r["confidence"]))
            counted.append(row)
        else:
            row["counted"] = 0
            row["why_not"] = ("overdue — not counted" if r["status"] == "OVERDUE" else
                              f"confidence {r['confidence']:.0%} < {config.RECEIPT_CONFIDENCE_MIN:.0%}" if (r["confidence"] or 0) < config.RECEIPT_CONFIDENCE_MIN
                              else f"due {r['due_date']}, after this run's horizon")
            later.append(row)
    expected = sum(r["counted"] for r in counted)
    nxt = next((r for r in later if r["status"] == "OPEN" and (r["confidence"] or 0) >= config.RECEIPT_CONFIDENCE_MIN), None)
    return {"bank_balance": balance, "balance_as_of": bp.get("as_of"), "balance_source": bp.get("source"),
            "safety_buffer": buffer, "buffer_items": buffer_items, "expected_receipts": expected,
            "receipts_counted": counted, "receipts_not_counted": later,
            "spendable": balance - buffer + expected, "next_receipt": nxt}


def annualised_discount(d: float, credit_days: int, discount_days: int) -> float:
    return (d / (1 - d)) * (365 / max(1, credit_days - discount_days))


def classify(conn, inv: dict, vendor: dict) -> dict:
    """Return {tier, reasons[], discount_amount, due_date}."""
    rs = []
    horizon_end = config.RUN_DATE + dt.timedelta(days=config.HORIZON_DAYS)
    due = _d(inv["due_date"]) or config.RUN_DATE
    out = {"tier": 2, "reasons": rs, "discount_amount": 0, "due_date": due, "due_in_horizon": due <= horizon_end}
    dl = _d(inv["msme_deadline"])
    if dl and dl <= horizon_end:
        days = (dl - config.RUN_DATE).days
        limit = 45 if vendor["has_written_agreement"] else 15
        when = f"{days} days left" if days >= 0 else f"{-days} days OVERDUE — MSMED interest is accruing"
        rs.append({"code": "MSME_DEADLINE", "text": f"MSME ({vendor['msme_category']}) — legal deadline {_fmt_date(dl)}, {when} "
                   f"(accepted {inv['accepted_date']} + {limit} days; Sec 15 MSMED Act / 43B(h): pay late and the expense isn't deductible this year)"})
        out["tier"] = 0
        return out
    if vendor["msme_category"] in ("micro", "small") and vendor["is_trader"]:
        rs.append({"code": "MSME_TRADER", "text": "MSME trader — not covered by the 45-day rule"})
    if vendor["is_critical"] and due < config.RUN_DATE:
        rs.append({"code": "CRITICAL_OVERDUE", "text": f"Critical/sole supplier, {(config.RUN_DATE - due).days} days overdue — supply stop risk"})
        out["tier"] = 0
        return out
    if vendor["discount_pct"] and inv["invoice_date"]:
        disc_until = _d(inv["invoice_date"]) + dt.timedelta(days=vendor["discount_days"])
        if config.RUN_DATE <= disc_until:
            d = vendor["discount_pct"] / 100
            ann = annualised_discount(d, vendor["credit_days"], vendor["discount_days"])
            if ann > config.COST_OF_CASH:
                out["tier"] = 1
                out["discount_amount"] = round_rupee(int(inv["outstanding"] * d))
                rs.append({"code": "EARLY_DISCOUNT", "text": f"{vendor['discount_pct']:g}/{vendor['discount_days']} net {vendor['credit_days']} — "
                           f"pay by {_fmt_date(disc_until)} and save {inr(out['discount_amount'])}; that's {ann:.0%} annualised vs our {config.COST_OF_CASH:.0%} cost of cash"})
                return out
    return out


def _min_instalment(inv) -> int:
    return min(inv["outstanding"], max(round_rupee(int(inv["total"] * config.MIN_INSTALMENT_PCT)), config.MIN_INSTALMENT_PAISE))


def _context_reasons(inv, vendor) -> list[dict]:
    rs = []
    pb = {r["signal"]: r for r in unj(vendor["priority_breakdown"], [])}
    if vendor["relationship_since"]:
        yrs = (config.RUN_DATE - _d(vendor["relationship_since"])).days / 365.25
        rs.append({"code": "RELATIONSHIP", "text": f"Supplier for {yrs:.0f} years, {vendor['hist_disputes']} disputes in {vendor['hist_invoices']} invoices",
                   "fact_ids": pb.get("relationship_years", {}).get("fact_ids", [])})
    tb = {r["signal"]: r for r in unj(vendor["trust_breakdown"], [])}
    bs = tb.get("bank_stability")
    if bs:
        rs.append({"code": "BANK_STABLE" if bs["value"] >= 1 else "BANK_RECENT", "text": f"Bank details {bs['note']}" + (" ✓" if bs["value"] >= 1 else ""),
                   "fact_ids": bs["fact_ids"]})
    rs.append({"code": "SCORES", "text": f"Priority {vendor['priority_score']:.2f} · Trust {vendor['trust_score']:.2f}"})
    return rs


def plan(conn, overrides: dict | None = None, spend_cap: int | None = None, vendor_filter: dict | None = None) -> dict:
    """Pure computation — returns the proposed allocation without writing.

    spend_cap (paise): from the customer's request; spendable = min(computed spendable, cap). Tier 0 still
    raises the cash-crisis alert if the cap is below what's legally due.
    vendor_filter: {include_ids, exclude_ids, msme_only} from the request — it can only narrow the run.
    """
    overrides = overrides or {}
    vf = vendor_filter or {}
    cash = cash_position(conn)
    cash["computed_spendable"] = cash["spendable"]
    cash["spend_cap"] = spend_cap
    if spend_cap is not None and spend_cap < cash["spendable"]:
        cash["spendable"] = max(0, int(spend_cap))
    filtered_out = []
    invs = q(conn, "SELECT i.*, v.legal_name vendor_name FROM invoices i JOIN vendors v ON v.id=i.vendor_id "
                   "WHERE i.state IN ('ELIGIBLE','SCHEDULED','PARTIALLY_PAID') AND i.outstanding > 0 ORDER BY i.rowid")
    rows = []
    for inv in invs:
        v = q1(conn, "SELECT * FROM vendors WHERE id=?", (inv["vendor_id"],))
        if v["status"] != "active":
            continue
        why_out = ("left out by your request" if v["id"] in (vf.get("exclude_ids") or [])
                   else "not in the vendors your request named" if vf.get("include_ids") and v["id"] not in vf["include_ids"]
                   else "not an MSME vendor (your request: only MSME)" if vf.get("msme_only") and v["msme_category"] == "none" else None)
        if why_out:
            filtered_out.append((inv, v, why_out))
            continue
        # outstanding net of instalments already approved/in flight in other runs
        inflight = q1(conn, "SELECT COALESCE(SUM(amount+discount_amount),0) s, COUNT(*) n FROM instalments WHERE invoice_id=? AND status IN ('APPROVED','INTENT_WRITTEN','SUBMITTED')", (inv["id"],))
        prior = q1(conn, "SELECT COUNT(*) n FROM instalments WHERE invoice_id=? AND status NOT IN ('PROPOSED','CANCELLED','FAILED')", (inv["id"],))["n"]
        inv = dict(inv, outstanding=inv["outstanding"] - inflight["s"])
        if inv["outstanding"] <= 0:
            continue
        c = classify(conn, inv, v)
        rows.append({"inv": inv, "vendor": v, "cls": c, "prior_instalments": prior, "amount": 0, "discount": 0})
    remaining = cash["spendable"]
    alerts = []
    t0 = [r for r in rows if r["cls"]["tier"] == 0]
    t0_total = sum(r["inv"]["outstanding"] for r in t0)
    crisis = t0_total > remaining
    if crisis:
        capped = spend_cap is not None and spend_cap < cash["computed_spendable"]
        alerts.append({"level": "error", "text": f"Cash crisis: {inr(t0_total - remaining)} short for legally-due / critical payments "
                       f"({inr(t0_total)} due vs {inr(remaining)} spendable"
                       + (f", capped by your request at {inr(spend_cap)}; the bank could cover {inr(cash['computed_spendable'])}" if capped else "")
                       + "). Nothing auto-allocated — decide manually."})
    if spend_cap is not None and spend_cap < cash["computed_spendable"]:
        alerts.append({"level": "info", "text": f"Spend capped by your request at {inr(spend_cap)} (cash alone would allow {inr(cash['computed_spendable'])})."})
    order = sorted(rows, key=lambda r: (r["cls"]["tier"], -(r["vendor"]["priority_score"] or 0), r["cls"]["due_date"]))
    for r in order:
        inv, tier, iid = r["inv"], r["cls"]["tier"], r["inv"]["id"]
        if iid in overrides:
            r["amount"] = max(0, min(int(overrides[iid]), inv["outstanding"]))
            r["override"] = True
            if tier == 1 and r["amount"] == inv["outstanding"] - r["cls"]["discount_amount"]:
                r["discount"] = r["cls"]["discount_amount"]
            remaining -= r["amount"]
            continue
        if crisis:
            r["defer"] = "cash crisis — approver decides"
            continue
        if tier == 0:
            r["amount"] = inv["outstanding"]
        elif tier == 1:
            need = inv["outstanding"] - r["cls"]["discount_amount"]
            if remaining >= need:
                r["amount"], r["discount"] = need, r["cls"]["discount_amount"]
            else:
                r["defer"] = "not enough cash to take the discount"
        remaining -= r["amount"]
    # Tier 2: proportional to priority × outstanding, with minimum instalment, in priority order
    t2 = [r for r in order if r["cls"]["tier"] == 2 and r["inv"]["id"] not in overrides and not crisis]
    for r in t2:
        if not r["cls"]["due_in_horizon"]:
            r["defer"] = f"not due until {_fmt_date(r['cls']['due_date'])}"
    pool = [r for r in t2 if "defer" not in r]
    for idx, r in enumerate(pool):
        inv = r["inv"]
        if remaining <= 0:
            r["defer"] = "cash exhausted by higher-priority payments"
            continue
        rest = pool[idx:]
        weight = sum((x["vendor"]["priority_score"] or 0.01) * x["inv"]["outstanding"] for x in rest) or 1
        share = round_rupee(int(remaining * (r["vendor"]["priority_score"] or 0.01) * inv["outstanding"] / weight))
        mn = _min_instalment(inv)
        amt = min(inv["outstanding"], max(share, mn))
        last_allowed = r["prior_instalments"] + 1 >= config.MAX_INSTALMENTS
        if last_allowed and amt < inv["outstanding"]:
            amt = inv["outstanding"]  # 3rd instalment must clear the invoice
        if amt > remaining:
            if remaining >= mn and not last_allowed:
                amt = round_rupee(remaining) if round_rupee(remaining) <= remaining else remaining
            else:
                r["defer"] = f"remaining cash {inr(remaining)} is below the minimum instalment {inr(mn)}"
                continue
        r["amount"] = amt
        remaining -= amt
    # justifications
    nxt = cash["next_receipt"]
    planned = _d(nxt["due_date"]) if nxt else config.RUN_DATE + dt.timedelta(days=7)
    proposals, deferred = [], []
    cash_left = cash["spendable"]
    for r in order:
        inv, v = r["inv"], r["vendor"]
        reasons = r["cls"]["reasons"] + _context_reasons(inv, v)
        if r.get("override"):
            reasons.insert(0, {"code": "APPROVER_OVERRIDE", "text": "Amount set by the approver"})
        amt = r["amount"]
        rem = inv["outstanding"] - amt - r["discount"]
        if amt > 0:
            cash_left -= amt
            j_ = {"tier": r["cls"]["tier"],
                  "headline": f"{inr(amt)} of {inr(inv['outstanding'])} now" if rem > 0 else f"{inr(amt)} — full {'(after discount)' if r['discount'] else 'payment'}",
                  "reasons": reasons, "cash_after": cash_left,
                  "remaining": None if rem <= 0 else {"amount": rem, "planned_date": planned.isoformat(),
                               "because": (f"after {inr(round_rupee(int(nxt['amount'] * nxt['confidence'])))} expected from {nxt['customer']} on {nxt['due_date']}" if nxt else "next weekly run")}}
            if r["cls"]["tier"] == 2 and rem > 0:
                j_["reasons"].append({"code": "INSTALMENT", "text": f"Instalment {r['prior_instalments'] + 1} of max {config.MAX_INSTALMENTS}; "
                                      f"min instalment {inr(_min_instalment(inv))} (25% or ₹10,000)"})
            proposals.append({"invoice_id": inv["id"], "vendor": v["legal_name"], "invoice_no": inv["invoice_no_raw"], "total": inv["total"],
                              "outstanding": inv["outstanding"], "amount": amt, "discount": r["discount"], "tier": r["cls"]["tier"],
                              "min": _min_instalment(inv), "justification": j_, "override": bool(r.get("override")),
                              "instalment_no": r["prior_instalments"] + 1})
        else:
            deferred.append({"invoice_id": inv["id"], "vendor": v["legal_name"], "invoice_no": inv["invoice_no_raw"], "total": inv["total"],
                             "outstanding": inv["outstanding"], "tier": r["cls"]["tier"], "min": _min_instalment(inv),
                             "why": r.get("defer", "no cash left"), "reasons": reasons})
        dl = _d(inv["msme_deadline"])
        if dl and 0 <= (dl - config.RUN_DATE).days <= 3:
            alerts.append({"level": "warning", "text": f"MSME deadline in {(dl - config.RUN_DATE).days} days: {v['legal_name']} {inv['invoice_no_raw']}"})
        if r["cls"]["tier"] == 1:
            alerts.append({"level": "info", "text": f"Discount window open: {v['legal_name']} — {r['cls']['reasons'][-1]['text']}"})
    for inv, v, why in filtered_out:
        dl = _d(inv["msme_deadline"])
        deferred.append({"invoice_id": inv["id"], "vendor": v["legal_name"], "invoice_no": inv["invoice_no_raw"], "total": inv["total"],
                         "outstanding": inv["outstanding"], "tier": None, "min": _min_instalment(inv), "why": why, "reasons": []})
        if dl and dl <= config.RUN_DATE + dt.timedelta(days=config.HORIZON_DAYS):
            alerts.append({"level": "warning", "text": f"{v['legal_name']} {inv['invoice_no_raw']} is left out by your request, but its MSME legal deadline is {_fmt_date(dl)}"})
    return {"cash": cash, "proposals": proposals, "deferred": deferred, "alerts": alerts, "crisis": crisis,
            "allocated": sum(p["amount"] for p in proposals), "unallocated": remaining,
            "spend_cap": spend_cap, "vendor_filter": vf}


def current_run(conn) -> dict | None:
    return q1(conn, "SELECT * FROM payment_runs WHERE run_date=? AND status IN ('DRAFT','PROPOSED','APPROVED','EXECUTING') ORDER BY created_at DESC LIMIT 1",
              (config.RUN_DATE.isoformat(),))


_KEEP = object()


def propose_run(conn, overrides: dict | None = None, actor="planner", spend_cap=_KEEP, vendor_filter=_KEEP) -> str:
    """Persist the plan as PROPOSED instalments (replacing any previous proposal for this run).

    spend_cap / vendor_filter default to whatever the current proposal used (so an approver's
    "recompute with my changes" keeps the request's cap); pass None to clear them.
    """
    prev = unj((current_run(conn) or {}).get("summary"), {}) or {}
    if spend_cap is _KEEP:
        spend_cap = prev.get("spend_cap")
    if vendor_filter is _KEEP:
        vendor_filter = prev.get("vendor_filter")
    p = plan(conn, overrides, spend_cap=spend_cap, vendor_filter=vendor_filter)
    with tx(conn):
        run = current_run(conn)
        if run and run["status"] not in ("DRAFT", "PROPOSED"):
            raise ValueError(f"Run already {run['status']} — can't re-plan")
        rid = run["id"] if run else new_id()
        if not run:
            conn.execute("INSERT INTO payment_runs(id,run_date,status) VALUES (?,?,'DRAFT')", (rid, config.RUN_DATE.isoformat()))
        old = q(conn, "SELECT invoice_id FROM instalments WHERE run_id=? AND status='PROPOSED'", (rid,))
        conn.execute("DELETE FROM instalments WHERE run_id=? AND status='PROPOSED'", (rid,))
        for o in old:
            conn.execute("UPDATE invoices SET state='ELIGIBLE' WHERE id=? AND state='SCHEDULED'", (o["invoice_id"],))
        c = p["cash"]
        bfid = add_fact(conn, "payment_run", rid, "bank_balance", c["bank_balance"], "internal", c.get("balance_source") or "bank_position.json",
                        "balance", c.get("balance_as_of"))
        for pr in p["proposals"]:
            pr["justification"]["cash_fact_id"] = bfid
            conn.execute("INSERT INTO instalments(id,idem_key,invoice_id,run_id,instalment_no,amount,discount_amount,tier,justification,proposed_amount,approver_override,status)"
                         " VALUES (?,?,?,?,?,?,?,?,?,?,?, 'PROPOSED')",
                         (new_id(), new_id().replace("-", ""), pr["invoice_id"], rid, pr["instalment_no"], pr["amount"], pr["discount"], pr["tier"],
                          j(pr["justification"]), pr["amount"], 1 if pr["override"] else 0))
            conn.execute("UPDATE invoices SET state='SCHEDULED' WHERE id=? AND state='ELIGIBLE'", (pr["invoice_id"],))
        summary = {k: p[k] for k in ("deferred", "alerts", "crisis", "allocated", "unallocated", "spend_cap", "vendor_filter")}
        summary["cash"] = c
        conn.execute("UPDATE payment_runs SET status='PROPOSED', bank_balance=?, safety_buffer=?, expected_receipts=?, spendable=?, summary=? WHERE id=?",
                     (c["bank_balance"], c["safety_buffer"], c["expected_receipts"], c["spendable"], j(summary), rid))
        add_event(conn, "payment_run", rid, run["status"] if run else None, "PROPOSED", actor,
                  {"allocated": p["allocated"], "instalments": len(p["proposals"]), "overrides": list((overrides or {}).keys()),
                   "spend_cap": spend_cap, "vendor_filter": vendor_filter})
    return rid


def approve_run(conn, run_id: str, approver: str) -> None:
    if not approver or not approver.strip():
        raise ValueError("Approver name required")
    with tx(conn):
        run = q1(conn, "SELECT * FROM payment_runs WHERE id=?", (run_id,))
        if run["status"] != "PROPOSED":
            raise ValueError(f"Run is {run['status']}, not PROPOSED")
        if unj(run["summary"], {}).get("crisis"):
            ov = q1(conn, "SELECT COUNT(*) n FROM instalments WHERE run_id=? AND approver_override=1", (run_id,))["n"]
            if not ov:
                raise ValueError("Cash crisis: set amounts manually before approving")
        ts = now_iso()
        n = conn.execute("UPDATE instalments SET status='APPROVED', approved_by=?, approved_at=? WHERE run_id=? AND status='PROPOSED'",
                         (approver.strip(), ts, run_id)).rowcount
        conn.execute("UPDATE payment_runs SET status='APPROVED', approved_by=?, approved_at=? WHERE id=?", (approver.strip(), ts, run_id))
        add_event(conn, "payment_run", run_id, "PROPOSED", "APPROVED", approver.strip(), {"instalments": n})
