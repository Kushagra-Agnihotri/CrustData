"""Golden test set: python scripts/run_test_set.py

Runs every case from a CLEAN database and prints a pass/fail table.
- case 12: fault injected on Crustdata + IFSC (checked before the cache)
- case 16: run in isolation (fresh DB) and compared with case 1's decision
- case 17: crash demo (kill mid-payment, restart, reconcile)
- case 18: must be held ONLY by a live (recorded) Crustdata news fact
- case 20: run the bank-change checklist on case 19's pending account, then a new invoice pays the new account
- case 20b: run in isolation; penny drop returns another name → approval refused
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.gettempdir()) / "payables_desk_tests"
TMP.mkdir(exist_ok=True)
os.environ["DB_PATH"] = str(TMP / "golden.db")
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from app import config, pipeline, scheduler, sources, vendor_admin  # noqa: E402
from app.db import connect, q, q1, unj  # noqa: E402
from scripts import setup_demo  # noqa: E402

EXP = None


def codes(inv):
    return [r["code"] for r in unj(inv["reasons"], []) if r["severity"] != "OK"]


def run_main_set():
    setup_demo.reset(Path(os.environ["DB_PATH"]), run=False, regenerate=True)
    conn = connect()
    ctx = pipeline.start_run(conn)
    pipeline.ingest(conn)
    for inv in q(conn, "SELECT id, case_id FROM invoices ORDER BY rowid"):
        faults = set(EXP.get(inv["case_id"], {}).get("inject_fault", []))
        sources.FAULTS.clear()
        sources.FAULTS.update(faults)
        pipeline.process_one(conn, inv["id"], ctx, actor="test")
        sources.FAULTS.clear()
    return conn


def check(conn):
    results = []
    p = scheduler.plan(conn)
    tiers = {x["invoice_id"]: x for x in p["proposals"]}
    tiers.update({x["invoice_id"]: dict(x, amount=0) for x in p["deferred"]})
    invs = {i["case_id"]: i for i in q(conn, "SELECT * FROM invoices")}
    for case, e in EXP.items():
        if e.get("isolated") or e.get("crash_test") or e.get("followup"):
            continue
        inv = invs.get(case)
        if not inv:
            results.append((case, e["label"], "missing", "FAIL"))
            continue
        got = inv["decision"]
        problems = []
        if got != e["decision"]:
            problems.append(f"decision {got} ≠ {e['decision']}")
        for c in e.get("must_include", []):
            if c not in codes(inv):
                problems.append(f"missing {c}")
        if e.get("only") and set(codes(inv)) != set(e.get("must_include", [])):
            problems.append(f"expected only {e['must_include']}, got {codes(inv)}")
        for c in e.get("must_exclude", []):
            if c in codes(inv):
                problems.append(f"unexpected {c}")
        if e.get("first_reason") and (codes(inv) or [None])[0] != e["first_reason"]:
            problems.append(f"first reason {(codes(inv) or [None])[0]} ≠ {e['first_reason']}")
        if e.get("pending_account") and not q1(conn, "SELECT 1 FROM vendor_bank_accounts WHERE source_doc_id=? AND status='pending_verification'", (inv["id"],)):
            problems.append("no pending bank-account row")
        if e.get("live_source") and config.CRUSTDATA_MODE != "mock":
            r = next((r for r in unj(inv["reasons"], []) if r["code"] == "ADVERSE_NEWS"), None)
            fs = [q1(conn, "SELECT * FROM facts WHERE id=?", (f,)) for f in (r or {}).get("fact_ids", [])]
            if not fs or any(f["source_type"].endswith("_mock") for f in fs) or not any("published" in (f["value"] or "") for f in fs):
                problems.append("ADVERSE_NEWS not backed by a live, dated source")
        alloc = tiers.get(inv["id"])
        if "tier" in e:
            if not alloc:
                problems.append("not in payment plan")
            elif alloc["tier"] != e["tier"]:
                problems.append(f"tier {alloc['tier']} ≠ {e['tier']}")
        if e.get("paid_in_full") and alloc and alloc["amount"] + alloc.get("discount", 0) != alloc["outstanding"]:
            problems.append("not paid in full")
        if e.get("split") and alloc:
            if not (alloc.get("min", 0) <= alloc["amount"] < alloc["outstanding"]):
                problems.append(f"not split (amount {alloc['amount']}, outstanding {alloc['outstanding']})")
        detail = f"{got}" + (f" T{alloc['tier']}" if alloc else "") + (f" [{', '.join(codes(inv)[:3])}]" if codes(inv) else "")
        if e.get("split") and alloc:
            detail += f" ₹{alloc['amount'] // 100:,} of ₹{alloc['outstanding'] // 100:,}"
        results.append((case, e["label"], "; ".join(problems) or detail, "FAIL" if problems else "PASS"))
    return results, invs


def bank_checklist(conn, acct_id, refuse_expected=False):
    """Run every B2 step (docs, call-back by asha, penny drop, notify old contact), then approve as vikram."""
    for err in (vendor_admin.mark_docs_received(conn, acct_id, "asha", "cancelled cheque + bank letter"),
                vendor_admin.record_callback(conn, acct_id, "asha", "Spoke to accounts head on the number on file; confirmed the new A/c"),
                vendor_admin.run_penny_drop(conn, acct_id, "asha"),
                vendor_admin.send_old_contact_notice(conn, acct_id, "asha")):
        if err:
            return err
    vendor_admin.suggest_second_contact(conn, acct_id)
    return vendor_admin.approve_bank_change(conn, acct_id, "vikram", "checklist complete")


def run_case20(conn, invs):
    e = EXP["case20"]
    acct = q1(conn, "SELECT id FROM vendor_bank_accounts WHERE source_doc_id=?", (invs["case19"]["id"],))
    if not acct:
        return ("case20", e["label"], "no pending account from case19", "FAIL")
    err = bank_checklist(conn, acct["id"])
    if err:
        return ("case20", e["label"], f"approval failed: {err}", "FAIL")
    ctx = pipeline.start_run(conn)
    pipeline.ingest(conn, inbox=config.DATA / "inbox_followup")
    inv = q1(conn, "SELECT * FROM invoices WHERE case_id='case20'")
    pipeline.process_one(conn, inv["id"], ctx, actor="test")
    inv = q1(conn, "SELECT * FROM invoices WHERE id=?", (inv["id"],))
    ok_codes = [r["code"] for r in unj(inv["reasons"], []) if r["severity"] == "OK"]
    problems = [f"decision {inv['decision']} ≠ {e['decision']} {codes(inv)}"] if inv["decision"] != e["decision"] else []
    problems += [f"missing {c}" for c in e["must_pass"] if c not in ok_codes]
    return ("case20", e["label"], "; ".join(problems) or f"{inv['decision']} [BANK_MATCHES_MASTER, new A/c ****{inv['bank_account_raw'][-4:]}]",
            "FAIL" if problems else "PASS")


def run_case20b():
    e = EXP["case20b"]
    db = TMP / "case20b.db"
    os.environ["DB_PATH"] = str(db)
    config.DB_PATH = db
    setup_demo.reset(db, run=False, regenerate=False)
    conn = connect(db)
    ctx = pipeline.start_run(conn)
    pipeline.ingest(conn, inbox=config.DATA / "inbox_followup" / "case20b")
    inv = q1(conn, "SELECT * FROM invoices WHERE case_id='case20b'")
    pipeline.process_one(conn, inv["id"], ctx, actor="test")
    acct = q1(conn, "SELECT * FROM vendor_bank_accounts WHERE source_doc_id=?", (inv["id"],))
    if not acct:
        return ("case20b", e["label"], "no pending account", "FAIL")
    err = bank_checklist(conn, acct["id"])
    acct = q1(conn, "SELECT * FROM vendor_bank_accounts WHERE id=?", (acct["id"],))
    ok = bool(err) and e["approval_refused"] in err and acct["status"] == "pending_verification"
    return ("case20b", e["label"], f"refused: {err}" if ok else f"not refused ({err}); status {acct['status']}", "PASS" if ok else "FAIL")


def run_isolated(case, base_decision):
    db = TMP / f"{case}.db"
    os.environ["DB_PATH"] = str(db)
    config.DB_PATH = db
    setup_demo.reset(db, run=False, regenerate=False)
    conn = connect(db)
    ctx = pipeline.start_run(conn)
    pipeline.ingest(conn, inbox=config.DATA / "inbox_variants")
    inv = q1(conn, "SELECT * FROM invoices WHERE case_id=?", (case,))
    pipeline.process_one(conn, inv["id"], ctx, actor="test")
    inv = q1(conn, "SELECT * FROM invoices WHERE id=?", (inv["id"],))
    ok = inv["decision"] == base_decision
    return (case, EXP[case]["label"], f"{inv['decision']} (case01: {base_decision}), {len(unj(inv['reasons'], []))} checks", "PASS" if ok else "FAIL")


def main():
    global EXP
    import scripts.generate_mock_data as gen
    gen.main()
    EXP = json.loads((config.DATA / "golden" / "expected.json").read_text(encoding="utf-8"))
    config.DB_PATH = Path(os.environ["DB_PATH"])
    conn = run_main_set()
    results, invs = check(conn)
    results.append(run_case20(conn, invs))
    conn.close()
    results.append(run_case20b())
    results.append(run_isolated("case16", invs["case01"]["decision"]))
    from scripts import crash_demo
    ok, summary = crash_demo.run(quiet=True)
    results.append(("case17", EXP["case17"]["label"], summary, "PASS" if ok else "FAIL"))
    results.sort()
    print(f"\nmode: Crustdata {config.CRUSTDATA_MODE} · IFSC {config.IFSC_MODE}")
    w = max(len(r[1]) for r in results)
    print(f"\n{'case':7} {'scenario':{w}}  result  detail")
    print("-" * (w + 60))
    for case, label, detail, res in results:
        print(f"{case:7} {label:{w}}  {'✅' if res == 'PASS' else '❌'} {res:4}  {detail}")
    n = sum(r[3] == "PASS" for r in results)
    print(f"\n{n}/{len(results)} passed")
    sys.exit(0 if n == len(results) else 1)


if __name__ == "__main__":
    main()
