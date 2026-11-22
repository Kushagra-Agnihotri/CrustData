"""Vendor-master changes under maker-checker.

A request is never proof: a real vendor and a fraudster send the same polite email. So a
bank change only becomes `current` after evidence from channels the requester doesn't control:

  1. documents received (cancelled cheque / bank letter on letterhead)
  2. call-back on the phone number ALREADY ON FILE (never one from the email)
  3. (recommended) a second contact at the vendor confirms — Crustdata suggests who
  4. penny drop: the bank says who owns the new account → must match the vendor's legal name
     (MOCK provider: Razorpay Account Validation is not available in test mode)
  5. the OLD contact on file is told about the change (draft → a human clicks send)
  6. approval by a different person from the proposer AND from whoever did the call-back

Every step writes an `events` row with the actor; steps with evidence also write a `facts` row.
"""
from __future__ import annotations

import hashlib
import json

from app import config, sources
from app.db import now_iso, q1, tx
from app.ledger import add_event, add_fact, add_src_fact
from app.validators import normalize_name

PENNY_DROP_FILE = config.DATA / "penny_drop_mock.json"


def _acct(conn, account_id):
    a = q1(conn, "SELECT * FROM vendor_bank_accounts WHERE id=?", (account_id,))
    v = q1(conn, "SELECT * FROM vendors WHERE id=?", (a["vendor_id"],)) if a else None
    return a, v


def _mask(n):
    return f"XXXX{(n or '')[-4:]}"


def _pending(a):
    return None if a and a["status"] == "pending_verification" else "Not a pending bank change"


# --------------------------------------------------------------------------- 1. documents
def mark_docs_received(conn, account_id: str, actor: str, note: str = "") -> str | None:
    actor = (actor or "").strip()
    if not actor:
        return "Your name is required"
    with tx(conn):
        a, v = _acct(conn, account_id)
        if err := _pending(a):
            return err
        conn.execute("UPDATE vendor_bank_accounts SET docs_received=1 WHERE id=?", (account_id,))
        fid = add_fact(conn, "vendor_bank_account", account_id, "documents_received",
                       {"by": actor, "note": note or "cancelled cheque / bank letter on letterhead"}, "human", f"checked by {actor}", "docs_received")
        add_event(conn, "vendor_bank_account", account_id, None, None, actor, {"step": "docs_received", "note": note, "fact_id": fid})
    return None


# --------------------------------------------------------------------------- 2. call-back
def callback_number(conn, account_id: str) -> str | None:
    """The ONLY number a call-back may use: the vendor's phone on file (never one from the email)."""
    _, v = _acct(conn, account_id)
    return v["phone_on_file"] if v else None


def record_callback(conn, account_id: str, actor: str, note: str) -> str | None:
    actor = (actor or "").strip()
    if not actor or not (note or "").strip():
        return "Your name and what the vendor confirmed are required"
    with tx(conn):
        a, v = _acct(conn, account_id)
        if err := _pending(a):
            return err
        number = v["phone_on_file"]
        conn.execute("UPDATE vendor_bank_accounts SET callback_done=1, callback_by=?, callback_note=? WHERE id=?",
                     (actor, note, account_id))
        fid = add_fact(conn, "vendor_bank_account", account_id, "callback",
                       {"number_called": number, "by": actor, "note": note}, "human", "vendors.phone_on_file", "phone_on_file")
        add_event(conn, "vendor_bank_account", account_id, None, None, actor,
                  {"step": "callback", "number_called": number, "number_source": "vendors.phone_on_file", "note": note, "fact_id": fid})
    return None


# --------------------------------------------------------------------------- 3. second contact (Crustdata)
def suggest_second_contact(conn, account_id: str, ctx=None) -> dict:
    """Crustdata people search for finance/accounts titles at the vendor: someone ELSE to confirm with.
    Returns {suggestions:[{title, name?}], retrieved_at, source, recorded, note}. Names appear only in a
    live run (UI); the committed recordings hold titles only."""
    ctx = ctx or sources.RunCtx()
    a, v = _acct(conn, account_id)
    if not v or not v["crustdata_company_id"]:
        return {"suggestions": [], "note": "can't suggest: vendor not resolved on Crustdata"}
    requester = None
    if a["source_doc_id"]:
        inv = q1(conn, "SELECT signatory FROM invoices WHERE id=?", (a["source_doc_id"],))
        requester = (inv or {}).get("signatory")
    try:
        res = sources.finance_contacts(conn, ctx, v["crustdata_company_id"])
    except sources.SourceUnavailable as e:
        return {"suggestions": [], "note": f"can't suggest: {e}"}
    rows = [m for m in res.data["matches"] if not (requester and m.get("name") and m["name"].lower() == requester.lower())]
    with tx(conn):
        fid = add_src_fact(conn, "vendor_bank_account", account_id, "second_contact_suggestions",
                           {"titles": [m.get("title") for m in rows]}, "crustdata", res,
                           "profiles[].basic_profile.current_title")
        add_event(conn, "vendor_bank_account", account_id, None, None, "system",
                  {"step": "second_contact_suggested", "count": len(rows), "fact_id": fid})
    return {"suggestions": rows, "retrieved_at": res.retrieved_at, "source": sources.source_type("crustdata", res),
            "recorded": res.recorded, "fact_id": fid,
            "note": "Found on Crustdata — confirm with this person, not with whoever sent the email."}


def record_second_contact(conn, account_id: str, actor: str, note: str) -> str | None:
    if not (actor or "").strip() or not (note or "").strip():
        return "Your name and who confirmed are required"
    with tx(conn):
        a, _ = _acct(conn, account_id)
        if err := _pending(a):
            return err
        conn.execute("UPDATE vendor_bank_accounts SET second_contact_note=? WHERE id=?", (note, account_id))
        add_event(conn, "vendor_bank_account", account_id, None, None, actor.strip(), {"step": "second_contact", "note": note})
    return None


# --------------------------------------------------------------------------- 4. penny drop (MOCK)
def _penny_provider(account_no: str) -> dict:
    book = json.loads(PENNY_DROP_FILE.read_text(encoding="utf-8")).get("accounts", {}) if PENNY_DROP_FILE.exists() else {}
    return book.get(account_no) or {"status": "failed", "error": "account could not be validated"}


def run_penny_drop(conn, account_id: str, actor: str = "system") -> str | None:
    """Mock penny drop. Compares the bank-registered name with the vendor's legal name using the
    same normalisation as vendor resolution. Returns an error string, or None once a result is stored."""
    with tx(conn):
        a, v = _acct(conn, account_id)
        if err := _pending(a):
            return err
        r = _penny_provider(a["account_no"])
        ref = "pdm_" + hashlib.sha256(f"{a['account_no']}{a['ifsc']}".encode()).hexdigest()[:12]
        if r.get("status") != "success":
            status, name = "failed", None
        else:
            name = r.get("registered_name")
            status = "name_match" if normalize_name(name) == v["name_normalized"] else "name_mismatch"
        conn.execute("UPDATE vendor_bank_accounts SET penny_drop_status=?, penny_drop_name=?, penny_drop_ref=? WHERE id=?",
                     (status, name, ref, account_id))
        fid = add_fact(conn, "vendor_bank_account", account_id, "penny_drop",
                       {"status": status, "registered_name": name, "vendor_legal_name": v["legal_name"], "ref": ref},
                       "penny_drop_mock", f"mock penny drop A/c {_mask(a['account_no'])} / {a['ifsc']}", "registered_name")
        add_event(conn, "vendor_bank_account", account_id, None, None, actor or "system",
                  {"step": "penny_drop", "provider": "penny_drop_mock", "status": status, "ref": ref, "fact_id": fid})
    return None


# --------------------------------------------------------------------------- 5. tell the old contact
def draft_old_contact_notice(conn, account_id: str) -> dict:
    a, v = _acct(conn, account_id)
    when = (a["valid_from"] or now_iso())[:10]
    return {"to": f"accounts@{v['domain']} / {v['phone_on_file']} (contact already on file)",
            "text": (f"Dear {v['legal_name']},\n\nYour bank details with {config.BUYER_NAME} are being changed to "
                     f"A/c {_mask(a['account_no'])} ({a['ifsc']}), requested on {when}. If this wasn't you, call us on "
                     f"{config.BUYER_PHONE} before any payment is released.\n\nAccounts Payable, {config.BUYER_NAME}")}


def send_old_contact_notice(conn, account_id: str, actor: str) -> str | None:
    """Needs a human click. Demo: logs the send (mock) — no real email leaves the system."""
    if not (actor or "").strip():
        return "Your name is required to send"
    with tx(conn):
        a, _ = _acct(conn, account_id)
        if err := _pending(a):
            return err
        d = draft_old_contact_notice(conn, account_id)
        ts = now_iso()
        conn.execute("UPDATE vendor_bank_accounts SET old_contact_notified_at=? WHERE id=?", (ts, account_id))
        add_event(conn, "vendor_bank_account", account_id, None, None, actor.strip(),
                  {"step": "old_contact_notified", "channel": "email (mock — not sent)", "to": d["to"], "text": d["text"]})
    return None


# --------------------------------------------------------------------------- 6. approve / reject
def approval_blockers(a: dict, approver: str | None = None) -> list[str]:
    out = []
    if not a["docs_received"]:
        out.append("documents not received (cancelled cheque / bank letter)")
    if not a["callback_done"]:
        out.append("no call-back on the number on file yet")
    ps = a["penny_drop_status"]
    if ps == "not_run":
        out.append("penny drop not run")
    elif ps == "name_mismatch":
        out.append(f"name_mismatch: the bank says the account belongs to '{a['penny_drop_name']}'")
    elif ps == "failed":
        out.append("can't verify: penny drop failed")
    if approver:
        ap = approver.strip().lower()
        if ap == (a["proposed_by"] or "").lower():
            out.append("maker-checker: approver must differ from the proposer")
        if ap == (a["callback_by"] or "").lower():
            out.append("maker-checker: approver must differ from whoever did the call-back")
    return out


def approve_bank_change(conn, account_id: str, approver: str, note: str = "") -> str | None:
    approver = (approver or "").strip()
    if not approver:
        return "Approver name required"
    with tx(conn):
        a, v = _acct(conn, account_id)
        if err := _pending(a):
            return err
        blockers = approval_blockers(a, approver)
        if blockers:
            add_event(conn, "vendor_bank_account", account_id, None, None, approver, {"step": "approval_refused", "why": blockers})
            return "Can't approve: " + "; ".join(blockers)
        ts = now_iso()
        warn = None if a["old_contact_notified_at"] else "old contact on file was NOT notified of the change"
        conn.execute("UPDATE vendor_bank_accounts SET status='retired', valid_to=? WHERE vendor_id=? AND status='current'", (ts, a["vendor_id"]))
        conn.execute("UPDATE vendor_bank_accounts SET status='current', approved_by=?, valid_from=?, verification_note=? WHERE id=?",
                     (approver, ts, f"Docs ✓, call-back on {v['phone_on_file']} by {a['callback_by']} ✓, penny drop name match ✓ "
                                    f"({a['penny_drop_name']}). {note}".strip(), account_id))
        conn.execute("UPDATE vendors SET trust_score=NULL WHERE id=?", (a["vendor_id"],))
        add_event(conn, "vendor_bank_account", account_id, "pending_verification", "current", approver,
                  {"note": note, "callback_by": a["callback_by"], "penny_drop": a["penny_drop_status"], "warning": warn})
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
