"""Crash demo (Windows-friendly replacement for crash_demo.sh):

1. fresh DB, run pipeline, propose + approve the payment run
2. start the worker with CRASH_AFTER_PAYOUTS=2 → it hard-exits (os._exit, like kill -9)
   right after the provider accepted a payout but BEFORE we recorded it
3. restart the worker normally → it asks the provider about the same idempotency key first
4. print per-invoice total / paid / outstanding / #provider payouts and assert no rupee paid twice
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _worker(env):
    return subprocess.run([sys.executable, "-m", "app.worker", "--once"], cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)


def run(quiet=False) -> tuple[bool, str]:
    db = Path(tempfile.gettempdir()) / "payables_desk_tests" / "crash_demo.db"
    db.parent.mkdir(exist_ok=True)
    env = dict(os.environ, DB_PATH=str(db), PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    os.environ["DB_PATH"] = str(db)
    from app import config, scheduler
    from app.db import connect, q
    from app.money import inr
    from scripts import setup_demo
    config.DB_PATH = db
    setup_demo.reset(db, run=True, regenerate=False)
    conn = connect(db)
    run_ = scheduler.current_run(conn)
    scheduler.approve_run(conn, run_["id"], "Kushagra (approver)")
    n_inst = q(conn, "SELECT COUNT(*) n FROM instalments WHERE run_id=?", (run_["id"],))[0]["n"]
    conn.close()

    p1 = _worker(dict(env, CRASH_AFTER_PAYOUTS="2"))
    conn = connect(db)
    mid = {r["status"]: r["n"] for r in q(conn, "SELECT status, COUNT(*) n FROM instalments GROUP BY status")}
    conn.close()
    p2 = _worker(env)
    conn = connect(db)
    rows = q(conn, "SELECT i.case_id, v.legal_name vendor, i.total, i.outstanding, "
                   "(SELECT COALESCE(SUM(amount),0) FROM instalments n WHERE n.invoice_id=i.id AND n.status='PAID') paid, "
                   "(SELECT COALESCE(SUM(discount_amount),0) FROM instalments n WHERE n.invoice_id=i.id AND n.status='PAID') disc, "
                   "(SELECT COUNT(*) FROM instalments n WHERE n.invoice_id=i.id AND n.status='PAID') n_paid, "
                   "(SELECT COUNT(*) FROM mock_provider_payouts p JOIN instalments n ON n.idem_key=p.idem_key WHERE n.invoice_id=i.id) n_payouts "
                   "FROM invoices i JOIN vendors v ON v.id=i.vendor_id WHERE i.decision='PAY_ELIGIBLE' ORDER BY i.rowid")
    total_payouts = q(conn, "SELECT COUNT(*) n FROM mock_provider_payouts")[0]["n"]
    recovered = q(conn, "SELECT COUNT(*) n FROM events WHERE entity_type='instalment' AND detail LIKE '%\"recovered\": true%'")[0]["n"]
    ok = all(r["n_paid"] == r["n_payouts"] and r["paid"] + r["disc"] + r["outstanding"] == r["total"] for r in rows) \
        and total_payouts == n_inst and p1.returncode != 0
    if not quiet:
        print(f"approved instalments: {n_inst}")
        print(f"worker #1 exit code {p1.returncode} (simulated kill -9 after 2 payouts) — instalment states at crash: {mid}")
        print(f"worker #2 exit code {p2.returncode} — {p2.stdout.strip()[-200:]}")
        print(f"\n{'case':7} {'vendor':32} {'total':>12} {'paid':>12} {'outstanding':>12}  instalments  provider payouts")
        for r in rows:
            print(f"{r['case_id']:7} {r['vendor'][:32]:32} {inr(r['total']):>12} {inr(r['paid']):>12} {inr(r['outstanding']):>12}  "
                  f"{r['n_paid']:>11}  {r['n_payouts']:>16}  {'✅' if r['n_paid'] == r['n_payouts'] else '❌'}")
        print(f"\nprovider payouts {total_payouts} == approved instalments {n_inst}; recovered-after-crash {recovered}")
        print("✅ NO RUPEE PAID TWICE" if ok else "❌ RECONCILIATION FAILED")
    return ok, f"crash exit {p1.returncode}; {total_payouts} payouts for {n_inst} instalments; {recovered} recovered via idem key"


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    ok, _ = run()
    sys.exit(0 if ok else 1)
