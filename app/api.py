"""JSON API for the web frontend (CHANGES_TODO F1). Thin wrapper: no business logic lives here.

    uvicorn app.api:app --port 8001        (or: python -m app.api)

Serves web/ as static files from the same process, so the demo is one process and one URL.
Every approve/release endpoint requires the approver's name (the wrapped functions enforce it).
"""
from __future__ import annotations

import csv
import io
import re
import threading
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles

from app import config, payments, pipeline, request, scheduler, vendor_admin
from app.db import connect, init_schema, q, q1, unj

WEB = config.ROOT / "web"
app = FastAPI(title="Payables Desk API")

LIVE_SOURCES = {"crustdata", "ifsc_api", "crustdata_web"}


def db():
    conn = connect()
    init_schema(conn)
    return conn


def _err(msg):
    if msg:
        raise HTTPException(400, msg)


def provenance(source_type: str | None) -> str:
    s = (source_type or "").lower()
    if s.endswith("_mock"):
        return "mock"
    if "replay" in s or "record" in s:
        return "recorded"
    if s in LIVE_SOURCES or s.startswith("crustdata") or s.startswith("ifsc"):
        return "live"
    if s == "invoice":
        return "document"
    return "ours"


def _fact(f: dict) -> dict:
    return {"id": f["id"], "key": f["key"], "value": unj(f["value"], f["value"]), "source_type": f["source_type"],
            "provenance": provenance(f["source_type"]), "source_ref": f["source_ref"], "source_field": f["source_field"],
            "retrieved_at": f["retrieved_at"]}


def facts_by_ids(conn, ids) -> list[dict]:
    ids = [i for i in (ids or []) if i]
    if not ids:
        return []
    return [_fact(f) for f in q(conn, f"SELECT * FROM facts WHERE id IN ({','.join('?' * len(ids))})", ids)]


def mask(acct: str | None) -> str | None:
    return f"A/c ••••{acct[-4:]}" if acct else None


def top_reason(inv) -> dict | None:
    rs = unj(inv["reasons"], [])
    order = {"BLOCK": 0, "HOLD": 1}
    bad = sorted([r for r in rs if r.get("severity") in order], key=lambda r: order[r["severity"]])
    return {"code": bad[0]["code"], "message": bad[0]["message"], "severity": bad[0]["severity"]} if bad else None


# --------------------------------------------------------------------------- config + home
@app.get("/api/config")
def get_config():
    return {"run_date": config.RUN_DATE.isoformat(), "crustdata_mode": config.CRUSTDATA_MODE, "ifsc_mode": config.IFSC_MODE,
            "payment_provider": config.PAYMENT_PROVIDER, "buyer": config.BUYER_NAME, "default_request": request.DEFAULT_REQUEST,
            "horizon_days": config.HORIZON_DAYS, "llm": bool(config.GEMINI_API_KEY)}


@app.post("/api/request")
def post_request(body: dict = Body(...)):
    conn = db()
    try:
        return request.parse_request(conn, body.get("text"))
    finally:
        conn.close()


_RUN = {"thread": None, "error": None, "result": None}


def _run_bg():
    conn = connect()
    try:
        _RUN["result"] = pipeline.run_pipeline(conn, actor="web")
    except Exception as e:  # noqa: BLE001
        _RUN["error"] = f"{type(e).__name__}: {e}"
    finally:
        conn.close()


@app.post("/api/run")
def post_run():
    """Start pipeline.run_pipeline in the background; poll GET /api/run/status for the live checklist."""
    t = _RUN["thread"]
    if t and t.is_alive():
        return {"started": False, "running": True}
    _RUN.update(error=None, result=None)
    _RUN["thread"] = threading.Thread(target=_run_bg, daemon=True)
    _RUN["thread"].start()
    return {"started": True, "running": True}


STAGE_OF = {"RECEIVED": "read", "EXTRACTED": "check", "VALIDATED": "check", "RESOLVED": "check", "MATCHED": "check", "CHECKED": "check"}


@app.get("/api/run/status")
def run_status():
    conn = db()
    try:
        rows = q(conn, "SELECT state, decision, COUNT(*) n FROM invoices GROUP BY state, decision")
        t = _RUN["thread"]
        total = sum(r["n"] for r in rows)
        done = sum(r["n"] for r in rows if r["state"] not in STAGE_OF)
        inbox = len([p for p in config.INBOX.iterdir() if p.is_file()]) if config.INBOX.exists() else 0
        return {"running": bool(t and t.is_alive()), "error": _RUN["error"], "result": _RUN["result"],
                "inbox_files": inbox, "documents": total, "decided": done,
                "by_state": {r["state"]: r["n"] for r in rows}}
    finally:
        conn.close()


@app.post("/api/reset")
def post_reset():
    """Reset the mock database (vendor master, POs, GRNs, receivables). Does not regenerate data files."""
    from scripts import setup_demo
    t = _RUN["thread"]
    if t and t.is_alive():
        raise HTTPException(409, "A run is in progress")
    setup_demo.reset(run=False, regenerate=False)
    _RUN.update(thread=None, error=None, result=None)
    return {"ok": True}


@app.get("/api/summary")
def summary():
    conn = db()
    try:
        invs = q(conn, "SELECT decision, doc_kind, total, reasons, vendor_id FROM invoices")
        count = {k: sum(1 for i in invs if i["decision"] == k) for k in ("PAY_ELIGIBLE", "HOLD", "BLOCK")}
        run = scheduler.current_run(conn) or q1(conn, "SELECT * FROM payment_runs ORDER BY created_at DESC LIMIT 1")
        insts = q(conn, "SELECT amount, status FROM instalments WHERE run_id=?", (run["id"],)) if run else []
        cost = q1(conn, "SELECT COALESCE(SUM(est_cost_inr),0) inr, COALESCE(SUM(credits),0) credits, COUNT(*) calls FROM calls")
        return {"documents": len(invs), "eligible": count["PAY_ELIGIBLE"], "held": count["HOLD"], "blocked": count["BLOCK"],
                "run": ({"id": run["id"], "status": run["status"], "approved_by": run["approved_by"], "spendable": run["spendable"],
                         "summary": unj(run["summary"], {})} if run else None),
                "proposed": sum(x["amount"] for x in insts if x["status"] != "CANCELLED"),
                "paid": sum(x["amount"] for x in insts if x["status"] == "PAID"),
                "paid_count": sum(1 for x in insts if x["status"] == "PAID"),
                "failed_count": sum(1 for x in insts if x["status"] == "FAILED"),
                "instalments": len(insts), "cost": cost}
    finally:
        conn.close()


# --------------------------------------------------------------------------- invoices
@app.get("/api/invoices")
def list_invoices():
    conn = db()
    try:
        rows = q(conn, "SELECT i.*, v.legal_name vname FROM invoices i LEFT JOIN vendors v ON v.id=i.vendor_id ORDER BY i.rowid")
        return [{"id": i["id"], "case_id": i["case_id"], "file": Path(i["source_path"] or "").name, "doc_kind": i["doc_kind"],
                 "vendor_id": i["vendor_id"], "vendor": i["vname"], "vendor_name_raw": i["vendor_name_raw"],
                 "invoice_no": i["invoice_no_raw"], "invoice_date": i["invoice_date"], "po_number": i["po_number_raw"],
                 "total": i["total"], "outstanding": i["outstanding"], "state": i["state"], "decision": i["decision"],
                 "top_reason": top_reason(i)} for i in rows]
    finally:
        conn.close()


def _po_panel(conn, inv) -> dict | None:
    """Invoice lines side by side with PO lines and goods-receipt qty (display only)."""
    po = q1(conn, "SELECT * FROM purchase_orders WHERE id=?", (inv["po_id"],)) if inv["po_id"] else (
        q1(conn, "SELECT * FROM purchase_orders WHERE po_number=?", (inv["po_number_raw"],)) if inv["po_number_raw"] else None)
    lines = q(conn, "SELECT * FROM invoice_lines WHERE invoice_id=? ORDER BY line_no", (inv["id"],))
    if not po:
        return {"po": None, "po_number_raw": inv["po_number_raw"], "rows": [{"invoice": ln, "po": None, "received": None, "flags": []} for ln in lines]} if lines else None
    pls = {p["id"]: p for p in q(conn, "SELECT * FROM po_lines WHERE po_id=? ORDER BY line_no", (po["id"],))}
    grns = q(conn, "SELECT * FROM grns WHERE po_id=?", (po["id"],))
    lmap = (unj(inv["extracted"], {}) or {}).get("_po_line_map", {}) or {}
    flags: dict[int, list] = {}
    for r in unj(inv["reasons"], []):
        if r.get("severity") == "OK" or r.get("stage") != "match":
            continue
        n = r.get("line")
        if n is None:
            m = re.match(r"Line (\d+)", r.get("message", ""))
            n = int(m.group(1)) if m else None
        if n is not None:
            flags.setdefault(int(n), []).append({"code": r["code"], "message": r["message"], "severity": r["severity"]})
    rows, used = [], set()
    for ln in lines:
        pl = pls.get(lmap.get(str(ln["line_no"])) or lmap.get(ln["line_no"]))
        if pl:
            used.add(pl["id"])
        rec = sum(g["qty"] for g in grns if pl and g["line_no"] == pl["line_no"]) if pl else None
        rows.append({"invoice": ln, "po": pl, "received": rec if grns else None, "flags": flags.get(ln["line_no"], [])})
    for pl in pls.values():
        if pl["id"] not in used:
            rec = sum(g["qty"] for g in grns if g["line_no"] == pl["line_no"])
            rows.append({"invoice": None, "po": pl, "received": rec if grns else None, "flags": []})
    v = q1(conn, "SELECT legal_name FROM vendors WHERE id=?", (po["vendor_id"],))
    return {"po": dict(po, vendor=v["legal_name"] if v else None), "grns": grns, "rows": rows, "has_grn": bool(grns)}


@app.get("/api/invoices/{inv_id}")
def get_invoice(inv_id: str):
    conn = db()
    try:
        inv = q1(conn, "SELECT i.*, v.legal_name vname FROM invoices i LEFT JOIN vendors v ON v.id=i.vendor_id WHERE i.id=?", (inv_id,))
        if not inv:
            raise HTTPException(404, "No such invoice")
        reasons = []
        order = {"BLOCK": 0, "HOLD": 1, "OK": 2}
        for r in sorted(unj(inv["reasons"], []), key=lambda r: order.get(r.get("severity"), 3)):
            reasons.append(dict(r, facts=facts_by_ids(conn, r.get("fact_ids"))))
        p = config.ROOT / (inv["source_path"] or "")
        original = p.read_text(encoding="utf-8", errors="replace")[:8000] if inv["source_path"] and p.exists() else None
        ex = unj(inv["extracted"], {}) or {}
        fields = {k: inv[k] for k in ("vendor_name_raw", "gstin_raw", "invoice_no_raw", "invoice_no_norm", "invoice_date", "po_number_raw",
                                      "subtotal", "tax", "total", "ifsc_raw", "beneficiary_raw", "sender_email", "reply_to", "signatory",
                                      "signatory_title", "accepted_date", "due_date", "msme_deadline", "outstanding")}
        fields["bank_account"] = mask(inv["bank_account_raw"])
        vendor = q1(conn, "SELECT id, legal_name, gstin, domain, phone_on_file, msme_category, status FROM vendors WHERE id=?", (inv["vendor_id"],)) if inv["vendor_id"] else None
        return {"id": inv["id"], "case_id": inv["case_id"], "file": Path(inv["source_path"] or "").name, "source_path": inv["source_path"],
                "source_kind": inv["source_kind"], "doc_kind": inv["doc_kind"], "state": inv["state"], "decision": inv["decision"],
                "explanation": inv["explanation"], "vendor": vendor, "vendor_name": inv["vname"], "fields": fields,
                "extraction_method": ex.get("_method"), "sha256": inv["file_sha256"], "reasons": reasons,
                "lines": q(conn, "SELECT line_no, item, qty, unit_price, gst_rate, amount FROM invoice_lines WHERE invoice_id=? ORDER BY line_no", (inv_id,)),
                "po_panel": _po_panel(conn, inv), "original": original,
                "instalments": q(conn, "SELECT id, instalment_no, amount, discount_amount, tier, status, provider_ref, approved_by, paid_at FROM instalments WHERE invoice_id=? ORDER BY instalment_no", (inv_id,)),
                "events": [dict(e, detail=unj(e["detail"], {})) for e in
                           q(conn, "SELECT seq, at, from_state, to_state, actor, detail FROM events WHERE entity_id=? ORDER BY seq", (inv_id,))]}
    finally:
        conn.close()


@app.post("/api/invoices/{inv_id}/recheck")
def post_recheck(inv_id: str, body: dict = Body(default={})):
    conn = db()
    try:
        actor = (body.get("actor") or "web").strip()
        pipeline.recheck(conn, inv_id, actor=actor)
        ctx = pipeline.start_run(conn)
        st = pipeline.process_one(conn, inv_id, ctx, actor=actor)
        return {"state": st}
    finally:
        conn.close()


@app.post("/api/invoices/{inv_id}/release")
def post_release(inv_id: str, body: dict = Body(...)):
    conn = db()
    try:
        _err(pipeline.release_with_note(conn, inv_id, (body.get("approver") or "").strip(), (body.get("note") or "").strip()))
        return {"ok": True}
    finally:
        conn.close()


# --------------------------------------------------------------------------- payment runs
def _run_payload(conn, run):
    if not run:
        return None
    insts = q(conn, "SELECT n.*, i.invoice_no_raw, i.total, i.outstanding inv_out, i.vendor_id, v.legal_name vendor FROM instalments n "
                    "JOIN invoices i ON i.id=n.invoice_id JOIN vendors v ON v.id=i.vendor_id WHERE n.run_id=? ORDER BY n.tier, n.created_at", (run["id"],))
    summ = unj(run["summary"], {})
    out = []
    for x in insts:
        jx = unj(x["justification"], {})
        out.append({k: x[k] for k in ("id", "invoice_id", "vendor_id", "vendor", "invoice_no_raw", "total", "inv_out", "amount", "discount_amount",
                                      "tier", "status", "proposed_amount", "approver_override", "provider_ref", "approved_by", "paid_at",
                                      "instalment_no", "last_error")}
                   | {"justification": jx, "min": min(x["inv_out"] or x["total"], max(int(x["total"] * config.MIN_INSTALMENT_PCT), config.MIN_INSTALMENT_PAISE))})
    return {"id": run["id"], "run_date": run["run_date"], "status": run["status"], "approved_by": run["approved_by"], "approved_at": run["approved_at"],
            "cash": summ.get("cash") or scheduler.cash_position(conn), "alerts": summ.get("alerts", []), "deferred": summ.get("deferred", []),
            "crisis": summ.get("crisis"), "allocated": sum(i["amount"] for i in out if i["status"] != "CANCELLED"),
            "paid": sum(i["amount"] for i in out if i["status"] == "PAID"), "spend_cap": summ.get("spend_cap"),
            "vendor_filter": summ.get("vendor_filter"), "instalments": out}


@app.post("/api/runs/propose")
def post_propose(body: dict = Body(default={})):
    conn = db()
    try:
        kw = {}
        if "spend_cap" in body:
            kw["spend_cap"] = int(body["spend_cap"]) if body["spend_cap"] is not None else None
        if "vendor_filter" in body:
            kw["vendor_filter"] = body["vendor_filter"]
        overrides = {k: int(v) for k, v in (body.get("overrides") or {}).items()}
        try:
            rid = scheduler.propose_run(conn, overrides=overrides or None, actor=(body.get("actor") or "planner"), **kw)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return _run_payload(conn, q1(conn, "SELECT * FROM payment_runs WHERE id=?", (rid,)))
    finally:
        conn.close()


@app.get("/api/runs/current")
def get_current_run():
    conn = db()
    try:
        run = scheduler.current_run(conn) or q1(conn, "SELECT * FROM payment_runs ORDER BY created_at DESC LIMIT 1")
        return _run_payload(conn, run)
    finally:
        conn.close()


@app.post("/api/runs/{run_id}/approve")
def post_approve(run_id: str, body: dict = Body(...)):
    conn = db()
    try:
        if not body.get("reviewed"):
            raise HTTPException(400, "Confirm you have reviewed every line")
        try:
            scheduler.approve_run(conn, run_id, (body.get("approver") or "").strip())
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return _run_payload(conn, q1(conn, "SELECT * FROM payment_runs WHERE id=?", (run_id,)))
    finally:
        conn.close()


@app.post("/api/runs/{run_id}/execute")
def post_execute(run_id: str):
    conn = db()
    try:
        stats = payments.execute_approved(conn, actor="web")
        return {"stats": stats, "run": _run_payload(conn, q1(conn, "SELECT * FROM payment_runs WHERE id=?", (run_id,)))}
    finally:
        conn.close()


@app.get("/api/cash")
def get_cash():
    conn = db()
    try:
        return scheduler.cash_position(conn)
    finally:
        conn.close()


# --------------------------------------------------------------------------- vendors
def _open_dues(conn, vid):
    return q1(conn, "SELECT COALESCE(SUM(outstanding),0) s FROM invoices WHERE vendor_id=? AND state IN ('ELIGIBLE','SCHEDULED','PARTIALLY_PAID')", (vid,))["s"]


@app.get("/api/vendors")
def list_vendors():
    conn = db()
    try:
        pend = {r["vendor_id"] for r in q(conn, "SELECT vendor_id FROM vendor_bank_accounts WHERE status='pending_verification'")}
        out = []
        for v in q(conn, "SELECT * FROM vendors ORDER BY legal_name"):
            held = q1(conn, "SELECT COUNT(*) n FROM invoices WHERE vendor_id=? AND decision IN ('HOLD','BLOCK')", (v["id"],))["n"]
            out.append({k: v[k] for k in ("id", "legal_name", "gstin", "msme_category", "is_trader", "is_critical", "trust_score",
                                          "priority_score", "status", "city", "state")}
                       | {"open_dues": _open_dues(conn, v["id"]), "held_docs": held, "pending_bank_change": v["id"] in pend})
        return out
    finally:
        conn.close()


@app.get("/api/vendors/{vid}")
def get_vendor(vid: str):
    conn = db()
    try:
        v = q1(conn, "SELECT * FROM vendors WHERE id=?", (vid,))
        if not v:
            raise HTTPException(404, "No such vendor")

        def bd(rows):
            return [dict(r, facts=facts_by_ids(conn, r.get("fact_ids"))) for r in (rows or [])]
        accts = [{k: a[k] for k in ("id", "ifsc", "beneficiary_name", "bank_name", "branch", "city", "valid_from", "valid_to", "status",
                                    "proposed_by", "approved_by", "verification_note", "created_at")} | {"account": mask(a["account_no"])}
                 for a in q(conn, "SELECT * FROM vendor_bank_accounts WHERE vendor_id=? ORDER BY created_at", (vid,))]
        facts = [_fact(f) for f in q(conn, "SELECT * FROM facts WHERE entity_type='vendor' AND entity_id=? AND source_type<>'internal' ORDER BY retrieved_at DESC", (vid,))]
        pos = q(conn, "SELECT p.*, (SELECT COALESCE(SUM(qty_ordered*unit_price),0) FROM po_lines l WHERE l.po_id=p.id) value FROM purchase_orders p WHERE vendor_id=? ORDER BY po_date", (vid,))
        invs = q(conn, "SELECT id, invoice_no_raw, doc_kind, invoice_date, total, outstanding, state, decision, reasons FROM invoices WHERE vendor_id=? ORDER BY rowid", (vid,))
        return {"vendor": {k: v[k] for k in v if k not in ("trust_breakdown", "priority_breakdown")} | {"aliases": unj(v["aliases"], [])},
                "trust_breakdown": bd(unj(v["trust_breakdown"], [])), "priority_breakdown": bd(unj(v["priority_breakdown"], [])),
                "trust_gate": config.TRUST_GATE, "bank_accounts": accts, "facts": facts, "pos": pos, "open_dues": _open_dues(conn, vid),
                "invoices": [{k: i[k] for k in i if k != "reasons"} | {"top_reason": top_reason(i)} for i in invs]}
    finally:
        conn.close()


@app.post("/api/bank-accounts/{acct_id}/approve")
def post_bank_approve(acct_id: str, body: dict = Body(...)):
    conn = db()
    try:
        _err(vendor_admin.approve_bank_change(conn, acct_id, body.get("approver"), bool(body.get("called_phone")), body.get("note") or ""))
        return {"ok": True}
    finally:
        conn.close()


@app.post("/api/bank-accounts/{acct_id}/reject")
def post_bank_reject(acct_id: str, body: dict = Body(...)):
    conn = db()
    try:
        _err(vendor_admin.reject_bank_change(conn, acct_id, body.get("approver") or body.get("actor"), body.get("note") or "rejected"))
        return {"ok": True}
    finally:
        conn.close()


@app.post("/api/vendors/{vid}/status")
def post_vendor_status(vid: str, body: dict = Body(...)):
    conn = db()
    try:
        if body.get("status") not in ("active", "on_hold", "blocked"):
            raise HTTPException(400, "status must be active, on_hold or blocked")
        _err(vendor_admin.set_vendor_status(conn, vid, body["status"], body.get("approver") or body.get("actor"), body.get("note")))
        return {"ok": True}
    finally:
        conn.close()


# --------------------------------------------------------------------------- company books
@app.get("/api/books/vendors")
def books_vendors():
    conn = db()
    try:
        return q(conn, "SELECT id, legal_name, gstin, pan, udyam_no, msme_category, is_trader, has_written_agreement, domain, phone_on_file, "
                       "city, state, credit_days, discount_pct, discount_days, is_critical, relationship_since, status FROM vendors ORDER BY legal_name")
    finally:
        conn.close()


@app.get("/api/books/pos")
def books_pos():
    conn = db()
    try:
        out = []
        for p in q(conn, "SELECT p.*, v.legal_name vendor FROM purchase_orders p JOIN vendors v ON v.id=p.vendor_id ORDER BY p.po_number"):
            lines = q(conn, "SELECT * FROM po_lines WHERE po_id=? ORDER BY line_no", (p["id"],))
            grns = q(conn, "SELECT * FROM grns WHERE po_id=? ORDER BY received_date", (p["id"],))
            for ln in lines:
                ln["qty_received_grn"] = sum(g["qty"] for g in grns if g["line_no"] == ln["line_no"])
            invs = q(conn, "SELECT id, invoice_no_raw, decision, state, total FROM invoices WHERE po_id=? OR po_number_raw=?", (p["id"], p["po_number"]))
            out.append(dict(p, lines=lines, grns=grns, invoices=invs))
        return out
    finally:
        conn.close()


@app.get("/api/books/receivables")
def books_receivables():
    conn = db()
    try:
        return q(conn, "SELECT r.id, c.legal_name customer, r.invoice_no, r.amount, r.due_date, r.confidence, r.status, r.confidence_breakdown "
                       "FROM receivables r JOIN customers c ON c.id=r.customer_id ORDER BY r.due_date")
    finally:
        conn.close()


@app.get("/api/books/bank")
def books_bank():
    conn = db()
    try:
        return {"position": scheduler.bank_position(), "cash": scheduler.cash_position(conn)}
    finally:
        conn.close()


# --------------------------------------------------------------------------- audit ledger
@app.get("/api/ledger/events")
def ledger_events(limit: int = 500):
    conn = db()
    try:
        return [dict(e, detail=unj(e["detail"], {})) for e in
                q(conn, "SELECT seq, at, entity_type, entity_id, from_state, to_state, actor, detail FROM events ORDER BY seq DESC LIMIT ?", (limit,))]
    finally:
        conn.close()


@app.get("/api/ledger/calls")
def ledger_calls():
    conn = db()
    try:
        tot = q1(conn, "SELECT COUNT(*) n, COALESCE(SUM(credits),0) credits, COALESCE(SUM(est_cost_inr),0) inr, COALESCE(SUM(input_tokens),0) input_tokens, "
                       "COALESCE(SUM(output_tokens),0) output_tokens, COALESCE(SUM(ok=0),0) fails FROM calls")
        per_doc = q(conn, "SELECT i.id, i.case_id, i.source_path, COUNT(c.id) calls, COALESCE(SUM(c.credits),0) credits, ROUND(COALESCE(SUM(c.est_cost_inr),0),3) est_inr "
                          "FROM invoices i LEFT JOIN calls c ON c.invoice_id=i.id GROUP BY i.id ORDER BY i.rowid")
        rec = q(conn, "SELECT i.id, i.case_id, v.legal_name vendor, i.vendor_id, i.invoice_no_raw, i.total, i.outstanding, "
                      "(SELECT COALESCE(SUM(amount),0) FROM instalments n WHERE n.invoice_id=i.id AND n.status='PAID') paid, "
                      "(SELECT COUNT(*) FROM instalments n WHERE n.invoice_id=i.id AND n.status='PAID') paid_instalments, "
                      "(SELECT COUNT(*) FROM mock_provider_payouts p JOIN instalments n ON n.idem_key=p.idem_key WHERE n.invoice_id=i.id) provider_payouts "
                      "FROM invoices i JOIN vendors v ON v.id=i.vendor_id WHERE i.decision='PAY_ELIGIBLE' ORDER BY i.rowid")
        last = q1(conn, "SELECT * FROM pipeline_runs ORDER BY started_at DESC LIMIT 1")
        return {"totals": tot, "last_run": last, "credit_limit": config.RUN_CREDIT_LIMIT, "per_document": per_doc, "reconciliation": rec,
                "calls": q(conn, "SELECT at, tool, endpoint, mode, credits, input_tokens, output_tokens, est_cost_inr, latency_ms, ok, http_status, error "
                                 "FROM calls ORDER BY id DESC LIMIT 500")}
    finally:
        conn.close()


@app.get("/api/ledger/export")
def ledger_export():
    conn = db()
    try:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["case", "file", "vendor", "invoice_no", "total_inr", "decision", "reasons", "instalments (amount|status|provider_ref)", "sources"])
        for i in q(conn, "SELECT i.*, v.legal_name vname FROM invoices i LEFT JOIN vendors v ON v.id=i.vendor_id ORDER BY i.rowid"):
            rs = unj(i["reasons"], [])
            ins = q(conn, "SELECT amount, status, provider_ref FROM instalments WHERE invoice_id=? ORDER BY instalment_no", (i["id"],))
            srcs = q(conn, "SELECT DISTINCT source_type, source_ref FROM facts WHERE entity_id=? OR entity_id=?", (i["id"], i["vendor_id"] or ""))
            w.writerow([i["case_id"], i["source_path"], i["vname"] or i["vendor_name_raw"], i["invoice_no_raw"], (i["total"] or 0) / 100, i["decision"],
                        "; ".join(f"{r['code']}: {r['message']}" for r in rs if r["severity"] != "OK"),
                        "; ".join(f"{x['amount'] / 100:.0f}|{x['status']}|{x['provider_ref'] or ''}" for x in ins),
                        "; ".join(f"{s['source_type']}:{(s['source_ref'] or '')[:60]}" for s in srcs)])
        return PlainTextResponse(buf.getvalue(), media_type="text/csv",
                                 headers={"Content-Disposition": "attachment; filename=auditor_export.csv"})
    finally:
        conn.close()


# --------------------------------------------------------------------------- static frontend
app.mount("/", StaticFiles(directory=WEB, html=True, check_dir=False), name="web")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.api:app", host="127.0.0.1", port=8001)
