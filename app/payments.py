"""Payment providers + exactly-once execution (BUILD_PROMPT §5, §11.1).

PROPOSED → (human) APPROVED → INTENT_WRITTEN → SUBMITTED → PAID | FAILED

On restart, anything in INTENT_WRITTEN/SUBMITTED is reconciled by asking the
provider about the SAME idempotency key first. A key is never regenerated.
"""
from __future__ import annotations

import os
import random
import time
from dataclasses import dataclass
from typing import Protocol

from app import config
from app.db import connect, now_iso, q, q1, tx
from app.ledger import add_event, record_call


@dataclass
class Beneficiary:
    name: str
    account_no: str
    ifsc: str


@dataclass
class ProviderResult:
    found: bool
    provider_ref: str | None = None
    status: str | None = None    # 'processing' | 'processed' | 'failed'
    error: str | None = None


class PaymentProvider(Protocol):
    name: str

    def create_payout(self, idem_key: str, amount_paise: int, beneficiary: Beneficiary, narration: str) -> ProviderResult: ...

    def get_status(self, idem_key: str) -> ProviderResult: ...


class MockProvider:
    """Test-mode provider with true idempotency: same key → the existing payout.
    Persists in its own table, as if it were a separate system."""
    name = "mock"

    def __init__(self, latency_s: float = 0.0, fail_rate: float = 0.0):
        self.latency_s, self.fail_rate = latency_s, fail_rate

    def create_payout(self, idem_key, amount_paise, beneficiary, narration):
        conn = connect()
        try:
            if self.latency_s:
                time.sleep(self.latency_s)
            status = "failed" if random.random() < self.fail_rate else "processed"
            with tx(conn):
                existing = q1(conn, "SELECT * FROM mock_provider_payouts WHERE idem_key=?", (idem_key,))
                if existing:
                    return ProviderResult(True, existing["payout_id"], existing["status"])
                pid = "pout_test_" + idem_key[:14]
                conn.execute("INSERT INTO mock_provider_payouts(idem_key,payout_id,amount,account_no,ifsc,beneficiary,narration,status) VALUES (?,?,?,?,?,?,?,?)",
                             (idem_key, pid, amount_paise, beneficiary.account_no, beneficiary.ifsc, beneficiary.name, narration, status))
            return ProviderResult(True, pid, status)
        finally:
            conn.close()

    def get_status(self, idem_key):
        conn = connect()
        try:
            r = q1(conn, "SELECT * FROM mock_provider_payouts WHERE idem_key=?", (idem_key,))
            return ProviderResult(True, r["payout_id"], r["status"]) if r else ProviderResult(False)
        finally:
            conn.close()


class RazorpayXProvider:
    """RazorpayX Payouts — TEST MODE ONLY. Verify header/endpoint names against current docs."""
    name = "razorpayx"

    def __init__(self):
        kid = os.environ.get("RAZORPAY_KEY_ID", "")
        if kid.startswith("rzp_live_"):
            raise RuntimeError("Refusing to run with live Razorpay keys")
        if not kid.startswith("rzp_test_"):
            raise RuntimeError("RAZORPAY_KEY_ID must be a test key (rzp_test_…)")
        self.auth = (kid, os.environ.get("RAZORPAY_KEY_SECRET", ""))
        self.account = os.environ.get("RAZORPAYX_ACCOUNT_NUMBER", "")

    def create_payout(self, idem_key, amount_paise, beneficiary, narration):
        import httpx
        body = {"account_number": self.account, "amount": amount_paise, "currency": "INR", "mode": "IMPS",
                "purpose": "vendor bill", "reference_id": idem_key[:40], "narration": narration[:30],
                "fund_account": {"account_type": "bank_account",
                                 "bank_account": {"name": beneficiary.name, "ifsc": beneficiary.ifsc, "account_number": beneficiary.account_no},
                                 "contact": {"name": beneficiary.name, "type": "vendor"}}}
        r = httpx.post("https://api.razorpay.com/v1/payouts", auth=self.auth, json=body,
                       headers={"X-Payout-Idempotency": idem_key}, timeout=30)
        if r.status_code >= 400:
            return ProviderResult(False, error=r.text[:300])
        d = r.json()
        return ProviderResult(True, d.get("id"), d.get("status"))

    def get_status(self, idem_key):
        import httpx
        r = httpx.get("https://api.razorpay.com/v1/payouts", auth=self.auth,
                      params={"account_number": self.account, "reference_id": idem_key[:40]}, timeout=30)
        items = r.json().get("items", []) if r.status_code == 200 else []
        if not items:
            return ProviderResult(False)
        return ProviderResult(True, items[0]["id"], items[0]["status"])


def get_provider() -> PaymentProvider:
    if config.PAYMENT_PROVIDER == "razorpayx":
        return RazorpayXProvider()
    return MockProvider(latency_s=float(os.environ.get("MOCK_LATENCY_S", "0")))


def _beneficiary(conn, inst) -> Beneficiary | None:
    """Only ever the vendor's CURRENT, verified account from the vendor master."""
    inv = q1(conn, "SELECT * FROM invoices WHERE id=?", (inst["invoice_id"],))
    acct = q1(conn, "SELECT * FROM vendor_bank_accounts WHERE vendor_id=? AND status='current'", (inv["vendor_id"],))
    v = q1(conn, "SELECT * FROM vendors WHERE id=?", (inv["vendor_id"],))
    if not acct or v["status"] != "active":
        return None
    return Beneficiary(acct["beneficiary_name"] or v["legal_name"], acct["account_no"], acct["ifsc"])


def _mark_paid(conn, inst, pr: ProviderResult, actor):
    with tx(conn):
        cur = q1(conn, "SELECT status FROM instalments WHERE id=?", (inst["id"],))
        if cur["status"] == "PAID":
            return
        conn.execute("UPDATE instalments SET status='PAID', provider_status=?, provider_ref=?, paid_at=? WHERE id=?",
                     (pr.status, pr.provider_ref, now_iso(), inst["id"]))
        inv = q1(conn, "SELECT * FROM invoices WHERE id=?", (inst["invoice_id"],))
        out = inv["outstanding"] - inst["amount"] - inst["discount_amount"]
        st = "PAID" if out <= 0 else "PARTIALLY_PAID"
        conn.execute("UPDATE invoices SET outstanding=?, state=? WHERE id=?", (max(0, out), st, inv["id"]))
        add_event(conn, "instalment", inst["id"], "SUBMITTED", "PAID", actor, {"provider_ref": pr.provider_ref, "amount": inst["amount"]})
        add_event(conn, "invoice", inv["id"], inv["state"], st, actor, {"outstanding": max(0, out)})


def execute_approved(conn, actor="worker", provider: PaymentProvider | None = None) -> dict:
    provider = provider or get_provider()
    crash_after = int(os.environ.get("CRASH_AFTER_PAYOUTS", "0") or 0)
    submitted_now = 0
    stats = {"paid": 0, "failed": 0, "recovered": 0}
    insts = q(conn, "SELECT * FROM instalments WHERE status IN ('APPROVED','INTENT_WRITTEN','SUBMITTED') ORDER BY tier, created_at")
    for run_id in {i["run_id"] for i in insts}:
        with tx(conn):
            conn.execute("UPDATE payment_runs SET status='EXECUTING' WHERE id=? AND status='APPROVED'", (run_id,))
    for inst in insts:
        inst = q1(conn, "SELECT * FROM instalments WHERE id=?", (inst["id"],))
        if inst["status"] == "APPROVED":
            ben = _beneficiary(conn, inst)
            if not ben:
                with tx(conn):
                    conn.execute("UPDATE instalments SET status='FAILED', last_error=? WHERE id=?", ("vendor not active or no verified bank account", inst["id"]))
                    add_event(conn, "instalment", inst["id"], "APPROVED", "FAILED", actor, {"error": "no verified account / vendor on hold"})
                stats["failed"] += 1
                continue
            with tx(conn):  # 1. write intent BEFORE calling the provider
                conn.execute("UPDATE instalments SET status='INTENT_WRITTEN', provider=? WHERE id=?", (provider.name, inst["id"]))
                add_event(conn, "instalment", inst["id"], "APPROVED", "INTENT_WRITTEN", actor, {"idem_key": inst["idem_key"]})
            inst = q1(conn, "SELECT * FROM instalments WHERE id=?", (inst["id"],))
        if inst["status"] == "INTENT_WRITTEN":
            t0 = time.monotonic()
            existing = provider.get_status(inst["idem_key"])  # 2. ask first (crash recovery)
            if existing.found:
                pr = existing
                stats["recovered"] += 1
            else:
                ben = _beneficiary(conn, inst)
                inv = q1(conn, "SELECT * FROM invoices WHERE id=?", (inst["invoice_id"],))
                pr = provider.create_payout(inst["idem_key"], inst["amount"], ben, f"{inv['invoice_no_raw']} #{inst['instalment_no']}")
                submitted_now += 1
                if crash_after and submitted_now >= crash_after:
                    os._exit(137)  # simulated kill -9 in the worst window: provider has it, we don't know yet
            with tx(conn):
                record_call(conn, tool="payments", endpoint=f"{provider.name}.create_payout", invoice_id=inst["invoice_id"],
                            latency_ms=int((time.monotonic() - t0) * 1000), ok=pr.found, error=pr.error, mode=provider.name)
                if not pr.found:
                    conn.execute("UPDATE instalments SET status='FAILED', last_error=? WHERE id=?", (pr.error, inst["id"]))
                    add_event(conn, "instalment", inst["id"], "INTENT_WRITTEN", "FAILED", actor, {"error": pr.error})
                    stats["failed"] += 1
                    continue
                conn.execute("UPDATE instalments SET status='SUBMITTED', provider_ref=?, provider_status=? WHERE id=?",
                             (pr.provider_ref, pr.status, inst["id"]))
                add_event(conn, "instalment", inst["id"], "INTENT_WRITTEN", "SUBMITTED", actor,
                          {"provider_ref": pr.provider_ref, "recovered": existing.found})
            inst = q1(conn, "SELECT * FROM instalments WHERE id=?", (inst["id"],))
        if inst["status"] == "SUBMITTED":
            pr = provider.get_status(inst["idem_key"])
            if pr.found and pr.status == "processed":
                _mark_paid(conn, inst, pr, actor)
                stats["paid"] += 1
            elif pr.found and pr.status in ("failed", "rejected", "reversed"):
                with tx(conn):
                    conn.execute("UPDATE instalments SET status='FAILED', provider_status=? WHERE id=?", (pr.status, inst["id"]))
                    add_event(conn, "instalment", inst["id"], "SUBMITTED", "FAILED", actor, {"provider_status": pr.status})
                stats["failed"] += 1
    with tx(conn):
        for r in q(conn, "SELECT id FROM payment_runs WHERE status='EXECUTING'"):
            left = q1(conn, "SELECT COUNT(*) n FROM instalments WHERE run_id=? AND status IN ('APPROVED','INTENT_WRITTEN','SUBMITTED')", (r["id"],))["n"]
            if not left:
                conn.execute("UPDATE payment_runs SET status='DONE' WHERE id=?", (r["id"],))
                add_event(conn, "payment_run", r["id"], "EXECUTING", "DONE", actor, {})
    return stats
