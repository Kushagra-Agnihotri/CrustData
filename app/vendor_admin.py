"""Vendor-master changes under maker-checker. An email can only PROPOSE; a person
(different from the proposer) approves after calling the number already on file."""
from __future__ import annotations

from app.db import now_iso, q1, tx
from app.ledger import add_event


def approve_bank_change(conn, account_id: str, approver: str, called_phone: bool, note: str) -> str | None:
    approver = (approver or "").strip()
    if not approver:
        return "Approver name required"
    if not called_phone:
        return "Confirm you called the phone number on file (not one from the email)"
    with tx(conn):
        a = q1(conn, "SELECT * FROM vendor_bank_accounts WHERE id=?", (account_id,))
        if not a or a["status"] != "pending_verification":
            return "Not a pending change"
        if approver.lower() == (a["proposed_by"] or "").lower():
            return "Maker-checker: the approver must be a different person from the proposer"
        ts = now_iso()
        conn.execute("UPDATE vendor_bank_accounts SET status='retired', valid_to=? WHERE vendor_id=? AND status='current'", (ts, a["vendor_id"]))
        conn.execute("UPDATE vendor_bank_accounts SET status='current', approved_by=?, valid_from=?, verification_note=? WHERE id=?",
                     (approver, ts, f"Phone on file called. {note}", account_id))
        conn.execute("UPDATE vendors SET trust_score=NULL WHERE id=?", (a["vendor_id"],))
        add_event(conn, "vendor_bank_account", account_id, "pending_verification", "current", approver, {"note": note, "called_phone_on_file": True})
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
