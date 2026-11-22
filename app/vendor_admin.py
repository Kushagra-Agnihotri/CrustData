"""Vendor-master changes under maker-checker. An email can only PROPOSE; a person
(different from the proposer) approves after calling the number already on file.

Genuine bank-change flow (Part B). A request is never proof: a real vendor and a fraudster
send the same polite email. The difference must come from channels the fraudster does not
control: a call-back on the number already on file, and the bank confirming who owns the
account (penny drop). Each step writes an `events` row (with the actor) and, where there is
evidence, a `facts` row.
"""
from __future__ import annotations

import json

from app import config
from app.db import now_iso, q1, tx
from app.ledger import add_event, add_fact
from app.validators import normalize_name

PENNY_DROP_FIXTURE = config.DATA / "penny_drop_mock.json"
FINANCE_TITLE_WORDS = ("cfo", "finance", "accounts", "accountant", "controller", "treasury")
OUR_PHONE = "+91 22 4000 1234"  # synthetic: Orbit Manufacturing AP desk


def mask(acct: str | None) -> str:
    return f"XXXX{acct[-4:]}" if acct else "?"


def _pending(conn, account_id):
    a = q1(conn, "SELECT * FROM vendor_bank_accounts WHERE id=?", (account_id,))
    if not a or a["status"] != "pending_verification":
        return None, None
    return a, q1(conn, "SELECT * FROM vendors WHERE id=?", (a["vendor_id"],))


def _step_event(conn, account_id, actor, detail):
    add_event(conn, "vendor_bank_account", account_id, "pending_verification", "pending_verification", actor, detail)


# --------------------------------------------------------------------------- B2 steps
def mark_docs_received(conn, account_id: str, actor: str, note: str = "") -> str | None:
    """Cancelled cheque / bank letter on the vendor's letterhead received."""
    actor = (actor or "").strip()
    if not actor:
        return "Name required"
    with tx(conn):
        a, _ = _pending(conn, account_id)
        if not a:
            return "Not a pending change"
        conn.execute("UPDATE vendor_bank_accounts SET docs_received=1 WHERE id=?", (account_id,))
        add_fact(conn, "vendor_bank_account", account_id, "docs_received", {"note": note}, "manual", f"user:{actor}", "docs_received")
        _step_event(conn, account_id, actor, {"step": "docs_received", "note": note})
    return None


def callback_number(conn, account_id: str) -> str | None:
    """The ONLY number a call-back may use: vendors.phone_on_file. Never a number from the email."""
    a = q1(conn, "SELECT vendor_id FROM vendor_bank_accounts WHERE id=?", (account_id,))
    v = q1(conn, "SELECT phone_on_file FROM vendors WHERE id=?", (a["vendor_id"],)) if a else None
    return v["phone_on_file"] if v else None


def record_callback(conn, account_id: str, actor: str, note: str = "") -> str | None:
    actor = (actor or "").strip()
    if not actor:
        return "Name required"
    with tx(conn):
        a, v = _pending(conn, account_id)
        if not a:
            return "Not a pending change"
        if actor.lower() == (a["proposed_by"] or "").lower():
            return "The call-back must be made by someone other than the requester"
        phone = v["phone_on_file"]
        if not phone:
            return "No phone number on file for this vendor: I can't do a call-back"
        conn.execute("UPDATE vendor_bank_accounts SET callback_done=1, callback_by=?, callback_note=? WHERE id=?",
                     (actor, note, account_id))
        add_fact(conn, "vendor_bank_account", account_id, "callback", {"phone_used": phone, "note": note},
                 "manual", f"user:{actor}", "vendors.phone_on_file")
        _step_event(conn, account_id, actor, {"step": "callback", "phone_on_file": phone, "note": note})
    return None


def suggest_second_contact(conn, account_id: str) -> dict:
    """Someone ELSE at the vendor (finance titles) to confirm with, found on Crustdata.
    Never the person who sent the request. Names are shown in the UI only, never committed."""
    a = q1(conn, "SELECT * FROM vendor_bank_accounts WHERE id=?", (account_id,))
    if not a:
        return {"ok": False, "why": "unknown account"}
    v = q1(conn, "SELECT * FROM vendors WHERE id=?", (a["vendor_id"],))
    cid = v.get("crustdata_company_id")
    if not cid:
        return {"ok": False, "why": f"I can't suggest a second contact: {v['legal_name']} not matched on Crustdata yet"}
    sender = None
    if a["source_doc_id"]:
        inv = q1(conn, "SELECT signatory FROM invoices WHERE id=?", (a["source_doc_id"],))
        sender = (inv or {}).get("signatory")
    from app import sources
    try:  # People Search by finance titles at the vendor's crustdata_company_id
        res = sources.person_search(conn, sources.RunCtx(), cid, title="|".join(FINANCE_TITLE_WORDS), limit=10)
    except sources.SourceUnavailable as e:
        return {"ok": False, "why": f"I can't suggest a second contact: {e}"}
    src, retrieved = sources.source_type("crustdata", res), res.retrieved_at
    people = []
    for p in (res.data or {}).get("profiles", []) or []:  # live / mock: full profile (name shown in the UI only)
        bp = p.get("basic_profile") or {}
        people.append({"name": bp.get("name"), "title": bp.get("current_title")})
    if not people:  # replay: the committed recording keeps titles only (no names)
        people = [{"name": None, "title": m.get("title")} for m in sources.person_titles(res.data)]
    picks = [p for p in people
             if any(w in (p.get("title") or "").lower() for w in FINANCE_TITLE_WORDS)
             and (not sender or (p.get("name") or "").lower() != sender.lower())]
    return {"ok": bool(picks), "source": src, "retrieved_at": retrieved or now_iso(),
            "contacts": [{"name": p.get("name"), "title": p.get("title")} for p in picks],
            "why": None if picks else "No other finance contact found on Crustdata"}


def record_second_contact(conn, account_id: str, actor: str, note: str) -> str | None:
    actor = (actor or "").strip()
    if not actor or not note:
        return "Name and note required"
    with tx(conn):
        a, _ = _pending(conn, account_id)
        if not a:
            return "Not a pending change"
        conn.execute("UPDATE vendor_bank_accounts SET second_contact_note=? WHERE id=?", (note, account_id))
        _step_event(conn, account_id, actor, {"step": "second_contact", "note": note})
    return None


def _penny_drop_provider(account_no: str, ifsc: str) -> dict:
    """MOCK provider (`penny_drop_mock`). Razorpay Account Validation is not available in test
    mode; in production this is the live call. Returns the bank's registered holder name."""
    try:
        fx = json.loads(PENNY_DROP_FIXTURE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"ok": False, "error": "penny-drop fixture missing"}
    hit = fx.get("accounts", {}).get(account_no)
    if not hit:
        return {"ok": False, "error": "the bank returned no name for this account"}
    return {"ok": True, "registered_name": hit["registered_name"], "ref": f"pdmock_{account_no[-6:]}_{ifsc}"}


def run_penny_drop(conn, account_id: str, actor: str = "system") -> str | None:
    """Compare the bank's registered name with the vendor's legal name, using the same
    normalisation as vendor resolution. Returns None on name_match, else the reason."""
    with tx(conn):
        a, v = _pending(conn, account_id)
        if not a:
            return "Not a pending change"
        r = _penny_drop_provider(a["account_no"], a["ifsc"])
        if not r["ok"]:
            status, name, ref = "failed", None, None
        else:
            name, ref = r["registered_name"], r["ref"]
            status = "name_match" if normalize_name(name) == v["name_normalized"] else "name_mismatch"
        conn.execute("UPDATE vendor_bank_accounts SET penny_drop_status=?, penny_drop_name=?, penny_drop_ref=? WHERE id=?",
                     (status, name, ref, account_id))
        add_fact(conn, "vendor_bank_account", account_id, "penny_drop",
                 {"status": status, "registered_name": name, "vendor_legal_name": v["legal_name"], "error": r.get("error")},
                 "penny_drop_mock", ref or "penny_drop_mock", "registered_name")
        _step_event(conn, account_id, actor, {"step": "penny_drop", "status": status})
    if status == "name_match":
        return None
    if status == "name_mismatch":
        return f"name_mismatch: the bank says this account belongs to '{name}', not {v['legal_name']}"
    return f"I can't verify the account holder: {r.get('error')}"


def draft_old_contact_notice(conn, account_id: str) -> str | None:
    """Draft to the EXISTING contact on file. Sending is a separate, human-clicked step."""
    a = q1(conn, "SELECT * FROM vendor_bank_accounts WHERE id=?", (account_id,))
    if not a:
        return None
    v = q1(conn, "SELECT * FROM vendors WHERE id=?", (a["vendor_id"],))
    cur = q1(conn, "SELECT * FROM vendor_bank_accounts WHERE vendor_id=? AND status='current' AND id<>?",
             (a["vendor_id"], account_id))
    return (f"To: {v['legal_name']} (contact on file: {v['phone_on_file']})\n"
            f"Your bank details with {config.BUYER_NAME} were changed from A/c {mask(cur['account_no']) if cur else '(none)'} "
            f"to A/c {mask(a['account_no'])} ({a['ifsc']}) on {now_iso()[:10]}. "
            f"If this wasn't you, call us on {OUR_PHONE} before any payment is made.")


def send_old_contact_notice(conn, account_id: str, actor: str) -> str | None:
    """Mock send for the demo: logs an event, sends no real email."""
    actor = (actor or "").strip()
    if not actor:
        return "Name required"
    text = draft_old_contact_notice(conn, account_id)
    if not text:
        return "Unknown account"
    with tx(conn):
        conn.execute("UPDATE vendor_bank_accounts SET old_contact_notified_at=? WHERE id=?", (now_iso(), account_id))
        add_event(conn, "vendor_bank_account", account_id, None, None, actor,
                  {"step": "old_contact_notified", "channel": "mock", "text": text})
    return None


def approval_blockers(a: dict, approver: str) -> list[str]:
    """B3: everything that must be true before a bank change can be approved."""
    approver = (approver or "").strip().lower()
    out = []
    if not a["docs_received"]:
        out.append("Documents not received (cancelled cheque / bank letter)")
    if not a["callback_done"]:
        out.append("No call-back on the number on file yet")
    pd = a["penny_drop_status"]
    if pd == "name_mismatch":
        out.append(f"name_mismatch: the bank says the account belongs to '{a['penny_drop_name']}'")
    elif pd == "failed":
        out.append("I can't verify the account holder (penny drop failed)")
    elif pd != "name_match":
        out.append("Penny drop not run")
    if approver and approver == (a["proposed_by"] or "").lower():
        out.append("Maker-checker: the approver must be a different person from the proposer")
    if approver and approver == (a["callback_by"] or "").lower():
        out.append("The approver must be a different person from whoever did the call-back")
    return out


def checklist(conn, account_id: str) -> list[dict]:
    """Status per step for the UI. done: True / False, or None for an optional step not done."""
    a = q1(conn, "SELECT * FROM vendor_bank_accounts WHERE id=?", (account_id,))
    return [
        {"step": "Documents (cancelled cheque / bank letter)", "done": bool(a["docs_received"]), "detail": ""},
        {"step": "Call-back on number on file", "done": bool(a["callback_done"]),
         "detail": f"by {a['callback_by']}: {a['callback_note'] or ''}" if a["callback_done"] else f"call {callback_number(conn, account_id)}"},
        {"step": "Second contact (Crustdata suggestion, optional)", "done": True if a["second_contact_note"] else None,
         "detail": a["second_contact_note"] or ""},
        {"step": "Penny drop (mock)", "done": a["penny_drop_status"] == "name_match",
         "detail": a["penny_drop_status"] + (f": {a['penny_drop_name']}" if a["penny_drop_name"] else "")},
        {"step": "Notify old contact (draft → send)", "done": bool(a["old_contact_notified_at"]), "detail": a["old_contact_notified_at"] or ""},
        {"step": "Approve (different person)", "done": a["status"] == "current", "detail": a["approved_by"] or ""},
    ]


# --------------------------------------------------------------------------- B3 approval
def approve_bank_change(conn, account_id: str, approver: str, called_phone: bool = True, note: str = "") -> str | None:
    """Requires docs received, a recorded call-back on the number on file, penny drop
    name_match, and an approver who is neither the proposer nor the person who called back.
    `called_phone` is kept for old callers; the recorded call-back is what counts."""
    approver = (approver or "").strip()
    if not approver:
        return "Approver name required"
    with tx(conn):
        a = q1(conn, "SELECT * FROM vendor_bank_accounts WHERE id=?", (account_id,))
        if not a or a["status"] != "pending_verification":
            return "Not a pending change"
        blockers = approval_blockers(a, approver)
        if blockers:
            return "; ".join(blockers)
        ts = now_iso()
        conn.execute("UPDATE vendor_bank_accounts SET status='retired', valid_to=? WHERE vendor_id=? AND status='current'", (ts, a["vendor_id"]))
        conn.execute("UPDATE vendor_bank_accounts SET status='current', approved_by=?, valid_from=?, verification_note=? WHERE id=?",
                     (approver, ts, f"Docs received; phone on file called by {a['callback_by']}; penny drop name_match "
                                    f"({a['penny_drop_name']}). {note}".strip(), account_id))
        conn.execute("UPDATE vendors SET trust_score=NULL WHERE id=?", (a["vendor_id"],))
        warn = None if a["old_contact_notified_at"] else "old contact not notified yet"
        add_event(conn, "vendor_bank_account", account_id, "pending_verification", "current", approver,
                  {"note": note, "callback_by": a["callback_by"], "penny_drop": a["penny_drop_status"],
                   "penny_drop_ref": a["penny_drop_ref"], "warning": warn})
    return None


def reject_bank_change(conn, account_id: str, actor: str, note: str) -> str | None:
    if not (actor or "").strip():
        return "Name required"
    with tx(conn):
        a = q1(conn, "SELECT * FROM vendor_bank_accounts WHERE id=?", (account_id,))
        if not a or a["status"] != "pending_verification":
            return "Not a pending change"
        conn.execute("UPDATE vendor_bank_accounts SET status='rejected', verification_note=? WHERE id=?", (note, account_id))
        add_event(conn, "vendor_bank_account", account_id, "pending_verification", "rejected", actor.strip(), {"note": note})
    return None


def set_vendor_status(conn, vendor_id: str, status: str, actor: str, note: str) -> str | None:
    if not (actor or "").strip() or not note:
        return "Name and note required"
    with tx(conn):
        v = q1(conn, "SELECT * FROM vendors WHERE id=?", (vendor_id,))
        conn.execute("UPDATE vendors SET status=? WHERE id=?", (status, vendor_id))
        add_event(conn, "vendor", vendor_id, v["status"], status, actor.strip(), {"note": note})
    return None
