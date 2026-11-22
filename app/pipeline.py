"""Invoice state machine (crash-safe).

RECEIVED → EXTRACTED → VALIDATED → RESOLVED → MATCHED → CHECKED → (HELD | BLOCKED | ELIGIBLE)

Every transition = ONE transaction: state update + events row + facts.
Each stage is idempotent: it replaces its own reasons (tagged by stage) on re-run.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import sqlite3
import time

from app import config, scoring, sources
from app.db import j, new_id, now_iso, q, q1, tx, unj
from app.extract import extract
from app.ledger import add_event, add_fact, add_src_fact, record_call
from app.money import inr
from app.reasons import BLOCK, HOLD, OK, passed, reason, sort_reasons
from app.validators import (content_hash, domain_core, domain_of, edit_distance, gstin_check_char,
                            gstin_checksum_ok, gstin_format_ok, ifsc_format_ok, norm_invoice_no,
                            normalize_name, pressure_terms, similarity, skeleton)

PIPELINE_FINAL = {"HELD", "BLOCKED", "ELIGIBLE", "SCHEDULED", "PARTIALLY_PAID", "PAID"}
NEXT = {"RECEIVED": "EXTRACTED", "EXTRACTED": "VALIDATED", "VALIDATED": "RESOLVED",
        "RESOLVED": "MATCHED", "MATCHED": "CHECKED", "CHECKED": "DECIDED"}


def _d(s):
    return dt.date.fromisoformat(s) if s else None


def _mask(acct):
    return f"****{acct[-4:]}" if acct else "—"


def _src_reason(e: sources.SourceUnavailable, stage, what: str):
    if isinstance(e, sources.CeilingReached):
        return reason("RUN_CEILING_REACHED", f"Run ceiling reached before {what}: {e.detail}. Held for a person.", stage=stage)
    return reason("SOURCE_UNAVAILABLE", f"I can't verify {what}: {e.source} {e.detail}. "
                  "A timeout is not an answer, so this is held.", stage=stage, source=e.source)


# --------------------------------------------------------------------------- ingest
def ingest(conn, inbox=None, actor="ingest") -> list[str]:
    inbox = inbox or config.INBOX
    ids = []
    for path in sorted(inbox.iterdir()):
        if path.suffix.lower() not in (".txt", ".eml", ".pdf"):
            continue
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        rel = str(path.relative_to(config.ROOT)).replace("\\", "/") if path.is_relative_to(config.ROOT) else str(path)
        case_id = path.name.split("_")[0] if path.name.startswith("case") else None
        with tx(conn):
            iid = new_id()
            cur = conn.execute(
                "INSERT OR IGNORE INTO invoices(id,file_sha256,source_path,source_kind,case_id,state) VALUES (?,?,?,?,?,?)",
                (iid, sha, rel, path.suffix.lower().lstrip("."), case_id, "RECEIVED"))
            if cur.rowcount:
                add_event(conn, "invoice", iid, None, "RECEIVED", actor, {"file": rel, "sha256": sha})
                ids.append(iid)
    return ids


# --------------------------------------------------------------------------- stages
def s_extract(conn, inv, ctx):
    stage = "extract"
    path = config.ROOT / inv["source_path"]
    key = f"extract:{inv['file_sha256']}"
    hit = q1(conn, "SELECT value FROM cache WHERE key=?", (key,))
    if hit:
        res = unj(hit["value"])
        record_call(conn, tool="extract", endpoint="cache", run_id=ctx.run_id, invoice_id=inv["id"], mode="cache")
    else:
        res, err = None, None
        for attempt in range(2):  # re-ask once, then HOLD
            t0 = time.monotonic()
            try:
                res = extract(path)
                u = res.get("usage") or {}
                it, ot = u.get("promptTokenCount"), u.get("candidatesTokenCount")
                cost = ((it or 0) / 1000 * config.LLM_INR_PER_1K_INPUT + (ot or 0) / 1000 * config.LLM_INR_PER_1K_OUTPUT)
                record_call(conn, tool="llm" if res["method"].startswith("llm") else "extract",
                            endpoint=res["method"], run_id=ctx.run_id, invoice_id=inv["id"], input_tokens=it,
                            output_tokens=ot, est_cost_inr=round(cost, 4),
                            latency_ms=int((time.monotonic() - t0) * 1000), mode=res["method"])
                break
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
                record_call(conn, tool="extract", endpoint="extract", run_id=ctx.run_id, invoice_id=inv["id"],
                            ok=False, error=err)
        if res is None:
            return {"_state": "HELD", "decision": "HOLD"}, [reason("EXTRACTION_FAILED", f"Could not read this document: {err}", stage=stage)]
        conn.execute("INSERT OR REPLACE INTO cache(key,value,retrieved_at) VALUES (?,?,?)", (key, j(res), now_iso()))
    d = res["data"]
    ref = f"{inv['source_path']}#sha256={inv['file_sha256'][:12]}"
    at = inv["created_at"]
    fids = {}
    for k in ("vendor_name", "gstin", "invoice_no", "invoice_date", "po_number", "subtotal", "tax", "total",
              "sender_email", "reply_to", "signatory", "account_no", "ifsc", "beneficiary_name"):
        v = d.get(k)
        if v is None and isinstance(d.get("bank"), dict):
            v = d["bank"].get(k)
        if v is not None:
            fids[k] = add_fact(conn, "invoice", inv["id"], k, v, "invoice", ref, k, at)
    bank = d.get("bank") or {}
    lines = d.get("lines") or []
    conn.execute("DELETE FROM invoice_lines WHERE invoice_id=?", (inv["id"],))
    for i, ln in enumerate(lines, 1):
        conn.execute("INSERT INTO invoice_lines VALUES (?,?,?,?,?,?,?)",
                     (inv["id"], i, ln["item"], ln["qty"], ln["unit_price"], ln["gst_rate"], ln["amount"]))
    if lines:
        fids["lines"] = add_fact(conn, "invoice", inv["id"], "lines", lines, "invoice", ref, "lines[]", at)
    upd = {
        "doc_kind": res["doc_kind"], "vendor_name_raw": d.get("vendor_name"), "gstin_raw": d.get("gstin"),
        "invoice_no_raw": d.get("invoice_no"), "invoice_no_norm": norm_invoice_no(d.get("invoice_no")),
        "invoice_date": d.get("invoice_date"), "po_number_raw": d.get("po_number"),
        "subtotal": d.get("subtotal"), "tax": d.get("tax"), "total": d.get("total"),
        "bank_account_raw": bank.get("account_no") or d.get("account_no"),
        "ifsc_raw": bank.get("ifsc") or d.get("ifsc"),
        "beneficiary_raw": bank.get("beneficiary_name") or d.get("beneficiary_name"),
        "sender_email": d.get("sender_email"), "reply_to": d.get("reply_to"),
        "signatory": d.get("signatory"), "signatory_title": d.get("signatory_title"),
        "content_hash": content_hash(lines) if lines else None,
        "extracted": j({**d, "_facts": fids, "_method": res["method"]}),
    }
    return upd, [passed("EXTRACTED", f"Read {res['kind'].upper()} via {res['method']} "
                        f"({'bank-change request' if res['doc_kind'] == 'bank_change' else f'{len(lines)} line(s)'})",
                        list(fids.values())[:3], stage)]


def _facts(inv):
    return (unj(inv["extracted"], {}) or {}).get("_facts", {})


def s_validate(conn, inv, ctx):
    stage, out = "validate", []
    f = _facts(inv)
    if inv["doc_kind"] != "invoice":
        return {}, out
    lines = q(conn, "SELECT * FROM invoice_lines WHERE invoice_id=? ORDER BY line_no", (inv["id"],))
    s = sum(l["amount"] for l in lines)
    arith = [f.get("lines"), f.get("subtotal"), f.get("tax"), f.get("total")]
    bad = []
    if inv["subtotal"] is not None and abs(s - inv["subtotal"]) > 100:
        bad.append(f"line items add up to {inr(s)} but sub-total says {inr(inv['subtotal'])}")
    if inv["subtotal"] is not None and inv["tax"] is not None and abs(inv["subtotal"] + inv["tax"] - inv["total"]) > 100:
        bad.append(f"sub-total {inr(inv['subtotal'])} + tax {inr(inv['tax'])} ≠ total {inr(inv['total'])}")
    out.append(reason("ARITHMETIC_MISMATCH", "; ".join(bad), arith, stage) if bad
               else passed("ARITHMETIC_OK", f"Lines, sub-total, tax and total reconcile ({inr(inv['total'])})", arith, stage))
    g = inv["gstin_raw"]
    if g:
        if not gstin_format_ok(g):
            out.append(reason("GSTIN_FORMAT", f"GSTIN '{g}' is not in the GSTIN format", [f.get("gstin")], stage))
        elif not gstin_checksum_ok(g):
            out.append(reason("INVALID_GSTIN", f"GSTIN {g} fails its checksum: check character should be "
                              f"'{gstin_check_char(g[:14])}', invoice shows '{g[14]}'", [f.get("gstin")], stage))
        else:
            out.append(passed("GSTIN_CHECKSUM_OK", f"GSTIN {g} format and checksum valid. Registration status: "
                              "I can't verify live (GST portal is not machine-accessible)", [f.get("gstin")], stage))
    idate = _d(inv["invoice_date"])
    if idate:
        if idate > config.RUN_DATE:
            out.append(reason("INVOICE_DATE_FUTURE", f"Invoice dated {idate} is after today ({config.RUN_DATE})", [f.get("invoice_date")], stage))
        elif (config.RUN_DATE - idate).days > config.MAX_INVOICE_AGE_DAYS:
            out.append(reason("INVOICE_TOO_OLD", f"Invoice dated {idate} is over {config.MAX_INVOICE_AGE_DAYS} days old", [f.get("invoice_date")], stage))
    if inv["ifsc_raw"] and not ifsc_format_ok(inv["ifsc_raw"]):
        out.append(reason("IFSC_FORMAT", f"IFSC '{inv['ifsc_raw']}' is not a valid IFSC format", [f.get("ifsc")], stage))
    return {}, out


def _match_vendor_by_name(vendors, raw_name):
    n = normalize_name(raw_name)
    sk = skeleton(raw_name)
    exact, look = None, None
    best = 0.0
    for v in vendors:
        names = [v["name_normalized"]] + [normalize_name(a) for a in unj(v["aliases"], [])]
        if n and n in names:
            exact = v
            break
        if sk and sk == skeleton(v["legal_name"]):
            look, best = v, 1.0
            continue
        sim = max(similarity(n, x) for x in names) if n else 0
        if sim > best:
            best, look = sim, v
    if exact:
        return exact, None, 1.0
    return None, (look if best >= 0.85 else None), best


def s_resolve(conn, inv, ctx):
    stage, out, upd = "resolve", [], {}
    f = _facts(inv)
    vendors = q(conn, "SELECT * FROM vendors")
    g = inv["gstin_raw"]
    vendor = None
    if inv["doc_kind"] == "bank_change":
        vendor, look, _ = _match_vendor_by_name(vendors, inv["vendor_name_raw"])
        if not vendor:
            out.append(reason("UNVERIFIABLE_VENDOR", f"Bank-change request names '{inv['vendor_name_raw']}', which is not in our vendor master", [f.get("vendor_name")], stage))
            return upd, out
        out.append(passed("CLAIMED_VENDOR", f"Request claims to come from {vendor['legal_name']} (vendor master)", [f.get("vendor_name")], stage))
    else:
        by_g = next((v for v in vendors if g and v["gstin"] == g), None)
        if by_g:
            vendor = by_g
            names = [by_g["name_normalized"]] + [normalize_name(a) for a in unj(by_g["aliases"], [])]
            if normalize_name(inv["vendor_name_raw"]) not in names and inv["vendor_name_raw"]:
                al = unj(by_g["aliases"], []) + [inv["vendor_name_raw"]]
                conn.execute("UPDATE vendors SET aliases=? WHERE id=?", (j(al), by_g["id"]))
                add_event(conn, "vendor", by_g["id"], None, None, "worker", {"alias_added": inv["vendor_name_raw"], "via_gstin": g})
            out.append(passed("VENDOR_BY_GSTIN", f"GSTIN {g} → {by_g['legal_name']} (vendor master). "
                              f"Invoice name '{inv['vendor_name_raw']}' kept as an alias of the same record",
                              [f.get("gstin"), f.get("vendor_name")], stage))
        else:
            exact, look, score = _match_vendor_by_name(vendors, inv["vendor_name_raw"])
            fids = [f.get("vendor_name"), f.get("gstin")]
            if exact and g:
                out.append(reason("GSTIN_MISMATCH", f"Name matches {exact['legal_name']} but GSTIN {g} ≠ vendor master {exact['gstin']}", fids, stage))
            elif exact and not g:
                out.append(reason("VENDOR_NOT_APPROVED", f"Invoice names {exact['legal_name']} but carries no GSTIN, so I can't confirm it is the same company", fids, stage))
            elif look:
                homo = skeleton(inv["vendor_name_raw"]) == skeleton(look["legal_name"])
                out.append(reason("VENDOR_NOT_APPROVED",
                                  f"'{inv['vendor_name_raw']}' is not an approved vendor — possible lookalike of {look['legal_name']}"
                                  + (" (homoglyph: characters swapped to look identical, e.g. capital I for lowercase l)" if homo else f" (name similarity {score:.0%})")
                                  + f"; GSTIN {g or 'missing'} ≠ {look['gstin']}", fids, stage, lookalike_of=look["id"]))
            else:
                missing, fid_reg = [], None
                if not g:
                    missing.append("GSTIN")
                if not inv["po_number_raw"]:
                    missing.append("PO")
                try:
                    res = sources.company_identify(conn, ctx, name=inv["vendor_name_raw"])
                    fid_reg = add_src_fact(conn, "invoice", inv["id"], "registry_lookup", res.data and res.data["company_data"],
                                           "crustdata", res, "[0].matches[0].company_data")
                    if not res.data:
                        missing.append("registry record")
                    else:
                        missing.append("anything tying this invoice to the same-named company Crustdata found (a name-only match is not proof)")
                except sources.SourceUnavailable as e:
                    out.append(_src_reason(e, stage, "the company registry"))
                    missing.append("registry record (source unavailable)")
                if not g and not inv["po_number_raw"]:
                    out.append(reason("UNVERIFIABLE_VENDOR", f"I can't verify '{inv['vendor_name_raw']}'. Missing: {', '.join(missing)}. "
                                      "No record settles who this is, so nothing will be paid.", fids + [fid_reg], stage, missing=missing))
                else:
                    out.append(reason("VENDOR_NOT_APPROVED", f"'{inv['vendor_name_raw']}' is not in the vendor master", fids + [fid_reg], stage))
    if vendor:
        upd["vendor_id"] = vendor["id"]
        if vendor["status"] == "on_hold":
            out.append(reason("VENDOR_ON_HOLD", f"{vendor['legal_name']} is on hold (see its bank-change review)", stage=stage))
        elif vendor["status"] == "blocked":
            out.append(reason("VENDOR_BLOCKED", f"{vendor['legal_name']} is blocked", stage=stage))
        if g and vendor["pan"] and g[2:12] != vendor["pan"]:
            out.append(reason("PAN_MISMATCH", f"PAN in GSTIN ({g[2:12]}) ≠ vendor PAN {vendor['pan']}", [f.get("gstin")], stage))
        if inv["doc_kind"] == "invoice":
            v2 = scoring.ensure_scores(conn, ctx, vendor["id"])
            tb = unj(v2["trust_breakdown"], [])
            ident = next((r for r in tb if r["signal"] == "identity"), None)
            if ident:
                out.append(passed("LIVE_IDENTITY", f"Crustdata company identify: {ident['note']}", ident["fact_ids"], stage))
            # Live-data rule: dated insolvency news about this vendor holds the invoice on its own,
            # even when every internal check is clean (no_adverse_news is only 15% of trust).
            news = next((r for r in tb if r["signal"] == "no_adverse_news"), None)
            arts = (news or {}).get("articles") or []
            if arts:
                a = next((x for x in arts if x.get("published")), arts[0])
                when = f"published {a['published'][:10]}" if a.get("published") else "publish date: can't verify"
                out.append(reason("ADVERSE_NEWS", f"Live news search: \"{a['title']}\" ({when}; matched '{a.get('matched_term')}'). "
                                  f"{vendor['legal_name']} appears to be in insolvency proceedings — held so a person reviews the "
                                  "counterparty risk before any payment.", [x["fact_id"] for x in arts], stage, url=a.get("url")))
            elif news and news.get("verified"):
                out.append(passed("NO_ADVERSE_NEWS", f"Live news search: {news['note']}", news["fact_ids"], stage))
    return upd, out


def s_match(conn, inv, ctx):
    stage, out, upd = "match", [], {}
    if inv["doc_kind"] != "invoice" or not inv["vendor_id"]:
        return upd, out
    f = _facts(inv)
    vendor = q1(conn, "SELECT * FROM vendors WHERE id=?", (inv["vendor_id"],))
    idate = _d(inv["invoice_date"]) or config.RUN_DATE
    upd["due_date"] = (idate + dt.timedelta(days=vendor["credit_days"])).isoformat()
    upd["accepted_date"] = idate.isoformat()
    if not inv["po_number_raw"]:
        out.append(reason("NO_PO", "Invoice cites no purchase order", [f.get("po_number")], stage))
        return upd, out
    po = q1(conn, "SELECT * FROM purchase_orders WHERE po_number=?", (inv["po_number_raw"],))
    if not po:
        out.append(reason("NO_PO", f"{inv['po_number_raw']} is not in our purchase orders", [f.get("po_number")], stage))
        return upd, out
    upd["po_id"] = po["id"]
    fpo = add_fact(conn, "invoice", inv["id"], "po", po["po_number"], "internal", "purchase_orders", f"po_number={po['po_number']}")
    if po["vendor_id"] != inv["vendor_id"]:
        out.append(reason("PO_VENDOR_MISMATCH", f"{po['po_number']} was raised on a different vendor", [fpo], stage))
        return upd, out
    pls = q(conn, "SELECT * FROM po_lines WHERE po_id=?", (po["id"],))
    grns = q(conn, "SELECT * FROM grns WHERE po_id=?", (po["id"],))
    lines = q(conn, "SELECT * FROM invoice_lines WHERE invoice_id=? ORDER BY line_no", (inv["id"],))
    line_map, problems, accepted = {}, 0, []
    for ln in lines:
        best = max(pls, key=lambda p: similarity(normalize_name(p["item"]), normalize_name(ln["item"])), default=None)
        if not best or similarity(normalize_name(best["item"]), normalize_name(ln["item"])) < 0.6:
            out.append(reason("UNMATCHED_LINE", f"Line {ln['line_no']} '{ln['item']}' is not on {po['po_number']}", [fpo, f.get("lines")], stage))
            problems += 1
            continue
        line_map[ln["line_no"]] = best["id"]
        fl = add_fact(conn, "po_line", best["id"], "po_line", {k: best[k] for k in ("item", "qty_ordered", "qty_invoiced", "unit_price")},
                      "internal", "po_lines", f"{po['po_number']}/line {best['line_no']}")
        remaining = best["qty_ordered"] - best["qty_invoiced"]
        if ln["qty"] > remaining + 1e-9:
            out.append(reason("OVER_BILLED", f"Line {ln['line_no']} ({ln['item']}): billed {ln['qty']:g} units, {po['po_number']} ordered "
                              f"{best['qty_ordered']:g}" + (f" and {best['qty_invoiced']:g} already invoiced" if best["qty_invoiced"] else ""),
                              [fl, f.get("lines")], stage, line=ln["line_no"]))
            problems += 1
        g_lines = [g for g in grns if g["line_no"] == best["line_no"]]
        if grns:
            received = sum(g["qty"] for g in g_lines)
            if ln["qty"] > received - best["qty_invoiced"] + 1e-9 and ln["qty"] <= remaining + 1e-9:
                out.append(reason("NOT_RECEIVED", f"Line {ln['line_no']} ({ln['item']}): billed {ln['qty']:g}, goods receipts show only {received:g} received", [fl], stage))
                problems += 1
            accepted += [g["received_date"] for g in g_lines]
        var = abs(ln["unit_price"] - best["unit_price"]) / best["unit_price"]
        if var > config.PRICE_TOLERANCE:
            out.append(reason("PRICE_VARIANCE", f"Line {ln['line_no']} ({ln['item']}): billed {inr(ln['unit_price'], True)}/unit vs PO {inr(best['unit_price'], True)} ({var:+.1%})", [fl, f.get("lines")], stage))
            problems += 1
    if accepted:
        upd["accepted_date"] = max(accepted)
    if not problems:
        out.append(passed("PO_MATCHED", f"Matched {po['po_number']}: {len(lines)} line(s) within quantity and ±2% price"
                          + (f"; {'3-way with goods receipt' if grns else '2-way (no GRN yet)'}"), [fpo], stage))
    ex = unj(inv["extracted"], {})
    ex["_po_line_map"] = line_map
    upd["extracted"] = j(ex)
    return upd, out


def _bank_checks(conn, inv, ctx, vendor, stage, acct, ifsc, ben, f) -> list[dict]:
    out = []
    if not ifsc:
        return out
    try:
        res = sources.ifsc_lookup(conn, ctx, ifsc)
        if res.data is None:
            out.append(reason("IFSC_NOT_FOUND", f"IFSC {ifsc} does not exist (live IFSC lookup returned 404)", [f.get("ifsc")], stage))
        else:
            b = res.data
            fid = add_src_fact(conn, "invoice", inv["id"], "ifsc_branch", {k: b.get(k) for k in ("BANK", "BRANCH", "CITY", "STATE")},
                               "ifsc_api", res, "BANK,BRANCH,CITY,STATE")
            bstate, vstate = (b.get("STATE") or "").upper(), ((vendor or {}).get("state") or "").upper()
            legacy = bool(vstate) and bstate != vstate and bstate in STATE_EQUIV.get(vstate, set())
            out.append(passed("IFSC_OK", f"IFSC {ifsc} = {b.get('BANK')}, {b.get('BRANCH')}, {b.get('CITY')} ({b.get('STATE')})"
                              + (f" — the IFSC dataset still lists this branch under {b.get('STATE')}, the pre-2014 state for "
                                 f"{vstate.title()}" if legacy else ""), [fid], stage))
            if vstate and bstate != vstate and not legacy:
                out.append(reason("IFSC_LOCATION_MISMATCH", f"Bank branch is in {b.get('CITY')}, {b.get('STATE')}; "
                                  f"{vendor['legal_name']} is based in {vendor['city']}, {vendor['state']}", [fid], stage))
    except sources.SourceUnavailable as e:
        out.append(_src_reason(e, stage, f"bank branch for IFSC {ifsc}"))
    if vendor and ben and normalize_name(ben) != vendor["name_normalized"] and \
            normalize_name(ben) not in [normalize_name(a) for a in unj(vendor["aliases"], [])]:
        out.append(reason("BENEFICIARY_NAME_MISMATCH", f"Beneficiary '{ben}' ≠ vendor legal name '{vendor['legal_name']}'", [f.get("beneficiary_name")], stage))
    return out


# Telangana was carved out of Andhra Pradesh in 2014; the Razorpay IFSC dataset still lists
# Hyderabad branches under ANDHRA PRADESH (seen live on 22 Nov 2026, see NOTES.md).
STATE_EQUIV = {"TELANGANA": {"ANDHRA PRADESH"}}


def _sender_checks(conn, inv, ctx, vendor, stage, f) -> tuple[list[dict], bool]:
    out, domain_bad = [], False
    sd = domain_of(inv["sender_email"])
    if not (sd and vendor):
        return out, False
    official = [vendor["domain"].lower()] if vendor["domain"] else []
    fid_off = add_fact(conn, "vendor", vendor["id"], "domain_on_file", vendor["domain"], "internal", "vendors (vendor master)", "domain")
    fids = [f.get("sender_email"), fid_off]
    if sd not in official:
        domain_bad = True
        try:
            res = sources.company_identify(conn, ctx, domain=sd)
            fids.append(add_src_fact(conn, "invoice", inv["id"], "sender_domain_identify", res.data and res.data["company_data"],
                                     "crustdata", res, "[0].matches[0].company_data"))
            who = "no company in Crustdata" if not res.data else f"belongs to {res.data['company_data']['basic_info']['name']}"
        except sources.SourceUnavailable:
            who = "Crustdata unavailable"
        near = [o for o in official if edit_distance(domain_core(sd), domain_core(o)) <= 2]
        out.append(reason("SENDER_DOMAIN_MISMATCH", f"Sent from {sd}; {vendor['legal_name']}'s domain is {', '.join(official)}"
                          + (" — a lookalike domain" if near else "") + f" ({sd}: {who})", fids, stage))
    else:
        out.append(passed("SENDER_DOMAIN_OK", f"Sender domain {sd} matches vendor master", fids, stage))
    rt = inv["reply_to"]
    if rt and rt != inv["sender_email"]:
        out.append(reason("REPLY_TO_MISMATCH", f"Replies would go to {rt}, not the sender {inv['sender_email']}", [f.get("reply_to"), f.get("sender_email")], stage))
    return out, domain_bad


def s_check(conn, inv, ctx):
    stage, out = "check", []
    f = _facts(inv)
    vendor = q1(conn, "SELECT * FROM vendors WHERE id=?", (inv["vendor_id"],)) if inv["vendor_id"] else None
    if inv["doc_kind"] == "bank_change":
        return _check_bank_change(conn, inv, ctx, vendor, f)
    # --- duplicates (three levels)
    if vendor:
        earlier = q(conn, "SELECT * FROM invoices WHERE vendor_id=? AND id<>? AND doc_kind='invoice' AND created_at<=? AND rowid<?",
                    (vendor["id"], inv["id"], inv["created_at"], q1(conn, "SELECT rowid r FROM invoices WHERE id=?", (inv["id"],))["r"]))
        ident = [e for e in earlier if e["invoice_no_norm"] and e["invoice_no_norm"] == inv["invoice_no_norm"]]
        if ident:
            e = ident[0]
            out.append(reason("DUPLICATE_INVOICE", f"Same invoice as {e['source_path']}: '{inv['invoice_no_raw']}' and '{e['invoice_no_raw']}' "
                              f"both normalise to {inv['invoice_no_norm']}", [f.get("invoice_no")], stage, duplicate_of=e["id"]))
        else:
            for e in earlier:
                why = []
                if e["total"] == inv["total"] and e["invoice_date"] and inv["invoice_date"] and \
                        abs((_d(e["invoice_date"]) - _d(inv["invoice_date"])).days) <= 7:
                    why.append(f"same total {inr(inv['total'])} within 7 days")
                if e["content_hash"] and e["content_hash"] == inv["content_hash"]:
                    why.append("same line items")
                if why:
                    out.append(reason("POSSIBLE_DUPLICATE", f"Possible duplicate of {e['invoice_no_raw']} ({e['source_path']}): " + " and ".join(why)
                                      + f", different number '{inv['invoice_no_raw']}'", [f.get("total"), f.get("lines")], stage, duplicate_of=e["id"]))
                    break
        if not any(r["code"] in ("DUPLICATE_INVOICE", "POSSIBLE_DUPLICATE") for r in out):
            out.append(passed("NO_DUPLICATE", "No earlier invoice with the same number, amount+date, or line items", [f.get("invoice_no")], stage))
    # --- bank details vs vendor master
    cur = q1(conn, "SELECT * FROM vendor_bank_accounts WHERE vendor_id=? AND status='current'", (vendor["id"],)) if vendor else None
    if vendor and inv["bank_account_raw"] and cur and (inv["bank_account_raw"] != cur["account_no"] or (inv["ifsc_raw"] and inv["ifsc_raw"] != cur["ifsc"])):
        out.append(reason("BANK_DETAILS_CHANGED", f"Invoice asks for payment to A/c {_mask(inv['bank_account_raw'])} ({inv['ifsc_raw']}); vendor master has "
                          f"{_mask(cur['account_no'])} ({cur['ifsc']}). We never pay to details from an invoice.", [f.get("account_no"), f.get("ifsc")], stage))
    elif vendor and cur:
        out.append(passed("BANK_MATCHES_MASTER", f"Bank details match vendor master A/c {_mask(cur['account_no'])}", [f.get("account_no")], stage))
    ifsc = inv["ifsc_raw"] or (cur["ifsc"] if cur else None)
    out += _bank_checks(conn, inv, ctx, vendor, stage, inv["bank_account_raw"], ifsc, inv["beneficiary_raw"], f)
    s_out, _ = _sender_checks(conn, inv, ctx, vendor, stage, f)
    out += s_out
    return {}, out


def _check_bank_change(conn, inv, ctx, vendor, f):
    stage, out = "check", []
    if not vendor:
        return {}, out
    out.append(reason("BANK_CHANGE_REQUEST", f"Email asks to change {vendor['legal_name']}'s bank account to {_mask(inv['bank_account_raw'])} "
                      f"({inv['ifsc_raw']}). An email can never update the vendor master: a pending change was created — "
                      f"call {vendor['phone_on_file']} (number already on file, not from the email), then a second person approves.",
                      [f.get("account_no"), f.get("ifsc")], stage))
    s_out, domain_bad = _sender_checks(conn, inv, ctx, vendor, stage, f)
    out += s_out
    # sender identity check — Crustdata People Search
    if inv["signatory"]:
        v = scoring.ensure_scores(conn, ctx, vendor["id"])
        cid = v["crustdata_company_id"]
        if not cid:
            out.append(reason("SENDER_NOT_VERIFIED_AT_VENDOR", f"I can't verify '{inv['signatory']}': {vendor['legal_name']} not found in Crustdata", [f.get("signatory")], stage))
        else:
            try:
                res = sources.person_search(conn, ctx, cid, inv["signatory"])
                hits = (res.data or {}).get("matches", [])
                fid = add_src_fact(conn, "invoice", inv["id"], "signatory_search",
                                   {"count": len(hits), "titles": [h.get("title") for h in hits]}, "crustdata", res,
                                   "profiles[].basic_profile.current_title")
                if not hits:
                    out.append(reason("SENDER_NOT_VERIFIED_AT_VENDOR", f"I can't verify that '{inv['signatory']}, {inv['signatory_title']}' works at "
                                      f"{vendor['legal_name']} (Crustdata people search: 0 current employees by that name)", [fid, f.get("signatory")], stage))
                else:
                    out.append(passed("SENDER_FOUND_AT_VENDOR", f"{inv['signatory']} found at {vendor['legal_name']} ({hits[0].get('title')}). "
                                      "This does not make the change safe — mailboxes get compromised", [fid], stage))
            except sources.SourceUnavailable as e:
                out.append(_src_reason(e, stage, f"whether {inv['signatory']} works at {vendor['legal_name']}"))
    terms = pressure_terms((unj(inv["extracted"], {}) or {}).get("body"))
    if terms:
        out.append(reason("PRESSURE_LANGUAGE", f"Pressure language: {', '.join(repr(t) for t in terms)}", [f.get("sender_email")], stage))
    out += _bank_checks(conn, inv, ctx, vendor, stage, inv["bank_account_raw"], inv["ifsc_raw"], inv["beneficiary_raw"], f)
    if domain_bad:
        out.append(reason("BANK_CHANGE_FRAUD", f"Bank change requested from a domain {vendor['legal_name']} does not use. "
                          f"Blocked, and {vendor['legal_name']} put on hold until verified by phone.", stage=stage))
    # pending bank-account row (idempotent per source document)
    if inv["bank_account_raw"] and inv["ifsc_raw"] and not q1(conn, "SELECT 1 FROM vendor_bank_accounts WHERE source_doc_id=?", (inv["id"],)):
        conn.execute("INSERT INTO vendor_bank_accounts(id,vendor_id,account_no,ifsc,beneficiary_name,status,proposed_by,verification_note,source_doc_id,valid_from)"
                     " VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (new_id(), vendor["id"], inv["bank_account_raw"], inv["ifsc_raw"], inv["beneficiary_raw"], "pending_verification",
                      f"email:{inv['sender_email']}", f"Call {vendor['phone_on_file']} (on file) to confirm", inv["id"], now_iso()))
        conn.execute("UPDATE vendors SET trust_score=NULL WHERE id=?", (vendor["id"],))  # re-score: bank no longer stable
    return {}, out


def _explain(state, reasons):
    holds = [r for r in reasons if r["severity"] in (HOLD, BLOCK)]
    oks = [r for r in reasons if r["severity"] == OK]
    if state == "ELIGIBLE":
        top = "; ".join(r["message"] for r in oks if r["code"] in ("PO_MATCHED", "VENDOR_BY_GSTIN", "IFSC_OK", "NO_DUPLICATE"))
        return f"Eligible for this week's payment run. {len(oks)} checks passed — {top}. Payment goes only to the bank account on file."
    blocks = [r for r in holds if r["severity"] == BLOCK]
    lead = "Blocked" if blocks else "Held for your review"
    msgs = [r["message"] for r in (blocks + [h for h in holds if h["severity"] == HOLD])][:3]
    return f"{lead}: " + " | ".join(m.rstrip(". ") for m in msgs) + ". Nothing is paid until a person reviews it."


def _msme_deadline(vendor, accepted):
    if vendor["msme_category"] in ("micro", "small") and not vendor["is_trader"] and accepted:
        return (accepted + dt.timedelta(days=45 if vendor["has_written_agreement"] else 15)).isoformat()
    return None


def make_eligible(conn, inv, actor) -> str | None:
    """Claim invoice identity + consume PO quantities. Returns error text or None."""
    try:
        conn.execute("INSERT INTO invoice_identities(vendor_id,invoice_no_norm,invoice_id) VALUES (?,?,?)",
                     (inv["vendor_id"], inv["invoice_no_norm"], inv["id"]))
    except sqlite3.IntegrityError:
        return "DUPLICATE_INVOICE"
    lmap = (unj(inv["extracted"], {}) or {}).get("_po_line_map", {})
    for ln in q(conn, "SELECT * FROM invoice_lines WHERE invoice_id=?", (inv["id"],)):
        pid = lmap.get(str(ln["line_no"])) or lmap.get(ln["line_no"])
        if pid:
            try:
                conn.execute("UPDATE po_lines SET qty_invoiced = qty_invoiced + ? WHERE id=?", (ln["qty"], pid))
            except sqlite3.IntegrityError:
                raise ValueError("PO quantity would be exceeded")
    if inv["po_id"]:
        pls = q(conn, "SELECT * FROM po_lines WHERE po_id=?", (inv["po_id"],))
        st = "CLOSED" if all(p["qty_invoiced"] >= p["qty_ordered"] for p in pls) else "PARTIALLY_INVOICED"
        conn.execute("UPDATE purchase_orders SET status=? WHERE id=?", (st, inv["po_id"]))
    return None


def s_decide(conn, inv, ctx):
    stage = "decide"
    reasons = unj(inv["reasons"], [])
    blocks = [r for r in reasons if r["severity"] == BLOCK]
    holds = [r for r in reasons if r["severity"] == HOLD]
    new = []
    upd = {}
    if blocks:
        state, decision = "BLOCKED", "BLOCK"
    elif holds or inv["doc_kind"] != "invoice":
        state, decision = "HELD", "HOLD"
    else:
        vendor = q1(conn, "SELECT * FROM vendors WHERE id=?", (inv["vendor_id"],))
        if (vendor["trust_score"] or 0) < config.TRUST_GATE:
            tb = unj(vendor["trust_breakdown"], [])
            new.append(reason("LOW_TRUST", f"Trust score {vendor['trust_score']:.2f} is below the {config.TRUST_GATE} gate: "
                              + "; ".join(f"{r['signal']} {r['note']}" for r in tb if r["value"] < 0.5), stage=stage))
            state, decision = "HELD", "HOLD"
        else:
            err = make_eligible(conn, inv, ctx)
            if err:
                new.append(reason("DUPLICATE_INVOICE", "Another invoice with this vendor + number is already payable", stage=stage))
                state, decision = "HELD", "HOLD"
            else:
                state, decision = "ELIGIBLE", "PAY_ELIGIBLE"
                upd["outstanding"] = inv["total"]
                upd["msme_deadline"] = _msme_deadline(vendor, _d(inv["accepted_date"]))
    if inv["doc_kind"] == "bank_change" and blocks and inv["vendor_id"]:
        v = q1(conn, "SELECT status FROM vendors WHERE id=?", (inv["vendor_id"],))
        if v["status"] == "active":
            conn.execute("UPDATE vendors SET status='on_hold' WHERE id=?", (inv["vendor_id"],))
            add_event(conn, "vendor", inv["vendor_id"], "active", "on_hold", "worker", {"because": "bank change from lookalike domain", "doc": inv["id"]})
    upd["decision"] = decision
    upd["_state"] = state
    upd["_explain"] = True
    return upd, new


STAGES = {"RECEIVED": s_extract, "EXTRACTED": s_validate, "VALIDATED": s_resolve,
          "RESOLVED": s_match, "MATCHED": s_check, "CHECKED": s_decide}

STAGE_TAG = {"RECEIVED": "extract", "EXTRACTED": "validate", "VALIDATED": "resolve",
             "RESOLVED": "match", "MATCHED": "check", "CHECKED": "decide"}


def step(conn, inv_id, ctx, actor="worker") -> str:
    inv = q1(conn, "SELECT * FROM invoices WHERE id=?", (inv_id,))
    state = inv["state"]
    if state in PIPELINE_FINAL:
        return state
    fn = STAGES[state]
    ctx.invoice_id = inv_id
    try:
        with tx(conn):
            inv = q1(conn, "SELECT * FROM invoices WHERE id=?", (inv_id,))
            upd, new_reasons = fn(conn, inv, ctx)
            tag = STAGE_TAG[state]
            reasons = sort_reasons([r for r in unj(inv["reasons"], []) if r.get("stage") != tag] + new_reasons)
            nxt = upd.pop("_state", NEXT[state])
            explain = upd.pop("_explain", False)
            if explain:
                upd["explanation"] = _explain(nxt, reasons)
            if nxt == "HELD" and "decision" not in upd:
                upd["decision"] = "HOLD"
                upd["explanation"] = _explain("HELD", reasons)
            upd.update({"reasons": j(reasons), "state": nxt, "locked_until": None, "last_error": None})
            cols = ", ".join(f"{k}=?" for k in upd)
            conn.execute(f"UPDATE invoices SET {cols} WHERE id=?", (*upd.values(), inv_id))
            add_event(conn, "invoice", inv_id, state, nxt, actor,
                      {"reasons": [r["code"] for r in new_reasons if r["severity"] != OK]})
        return nxt
    except Exception as e:  # unexpected bug/crash inside a stage: whole transaction rolled back
        with tx(conn):
            inv = q1(conn, "SELECT * FROM invoices WHERE id=?", (inv_id,))
            att = inv["attempts"] + 1
            if att >= 3:
                rs = unj(inv["reasons"], []) + [reason("PIPELINE_ERROR", f"Stage {STAGE_TAG[state]} failed 3 times: {e}", stage="error")]
                conn.execute("UPDATE invoices SET attempts=?, last_error=?, state='HELD', decision='HOLD', reasons=?, explanation=?, locked_until=NULL WHERE id=?",
                             (att, str(e), j(rs), _explain("HELD", rs), inv_id))
                add_event(conn, "invoice", inv_id, state, "HELD", actor, {"error": str(e)})
                return "HELD"
            conn.execute("UPDATE invoices SET attempts=?, last_error=?, locked_until=NULL WHERE id=?", (att, str(e), inv_id))
        return state


def claim_next(conn) -> str | None:
    """Pick the oldest unfinished, unlocked invoice and lock it for 60 s
    (SQLite equivalent of SELECT … FOR UPDATE SKIP LOCKED)."""
    with tx(conn):
        row = q1(conn, "SELECT id FROM invoices WHERE state NOT IN ('HELD','BLOCKED','ELIGIBLE','SCHEDULED','PARTIALLY_PAID','PAID') "
                       "AND (locked_until IS NULL OR locked_until < ?) ORDER BY rowid LIMIT 1", (now_iso(),))
        if not row:
            return None
        until = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=60)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        conn.execute("UPDATE invoices SET locked_until=? WHERE id=?", (until, row["id"]))
        return row["id"]


def start_run(conn) -> sources.RunCtx:
    rid = new_id()
    with tx(conn):
        conn.execute("INSERT INTO pipeline_runs(id,started_at,time_limit_s,credit_limit,status) VALUES (?,?,?,?,?)",
                     (rid, now_iso(), config.RUN_TIME_LIMIT_S, config.RUN_CREDIT_LIMIT, "RUNNING"))
    return sources.RunCtx(run_id=rid)


def process_one(conn, inv_id, ctx, actor="worker") -> str:
    for _ in range(12):
        st = step(conn, inv_id, ctx, actor)
        if st in PIPELINE_FINAL:
            return st
    return st


def pending_count(conn) -> int:
    return q1(conn, "SELECT COUNT(*) n FROM invoices WHERE state NOT IN ('HELD','BLOCKED','ELIGIBLE','SCHEDULED','PARTIALLY_PAID','PAID')")["n"]


def iter_pipeline(conn, actor="worker", ctx=None):
    """Same work as run_pipeline, but yields an event after every stage so a UI can show it live.

    Events: invoice_start{invoice, index, total} · stage{invoice_id, stage, state, reasons} ·
    invoice_done{invoice} · run_done{run_id, processed}
    """
    ctx = ctx or start_run(conn)
    ingest(conn)
    total = pending_count(conn)
    done = 0
    while True:
        iid = claim_next(conn)
        if not iid:
            break
        yield {"type": "invoice_start", "invoice": q1(conn, "SELECT * FROM invoices WHERE id=?", (iid,)), "index": done + 1, "total": total}
        st = None
        for _ in range(12):
            prev = q1(conn, "SELECT state FROM invoices WHERE id=?", (iid,))["state"]
            if prev in PIPELINE_FINAL:
                break
            st = step(conn, iid, ctx, actor)
            tag = STAGE_TAG[prev]
            row = q1(conn, "SELECT * FROM invoices WHERE id=?", (iid,))
            yield {"type": "stage", "invoice_id": iid, "stage": tag, "state": st,
                   "reasons": [r for r in unj(row["reasons"], []) if r.get("stage") == tag]}
            if st in PIPELINE_FINAL:
                break
        yield {"type": "invoice_done", "invoice": q1(conn, "SELECT * FROM invoices WHERE id=?", (iid,))}
        done += 1
    with tx(conn):
        conn.execute("UPDATE pipeline_runs SET finished_at=?, status='DONE' WHERE id=?", (now_iso(), ctx.run_id))
    yield {"type": "run_done", "run_id": ctx.run_id, "processed": done}


def run_pipeline(conn, actor="worker", ctx=None) -> dict:
    out = {}
    for ev in iter_pipeline(conn, actor, ctx):
        if ev["type"] == "run_done":
            out = {"run_id": ev["run_id"], "processed": ev["processed"]}
    return out


# --------------------------------------------------------------------------- human actions
def release_with_note(conn, inv_id, actor, note) -> str | None:
    """Human override of a HOLD. Logged with actor. BLOCK can't be released."""
    if not actor or not note:
        return "Approver name and note are required"
    with tx(conn):
        inv = q1(conn, "SELECT * FROM invoices WHERE id=?", (inv_id,))
        if inv["state"] != "HELD":
            return f"Only HELD invoices can be released (this one is {inv['state']})"
        if not inv["vendor_id"] or inv["doc_kind"] != "invoice":
            return "Can't release: no approved vendor on this document"
        try:
            err = make_eligible(conn, inv, actor)
        except ValueError as e:
            return f"Can't release: {e}"
        if err:
            return "Can't release: this vendor + invoice number is already payable (duplicate)"
        v = q1(conn, "SELECT * FROM vendors WHERE id=?", (inv["vendor_id"],))
        rs = unj(inv["reasons"], []) + [passed("RELEASED_BY_HUMAN", f"Released by {actor}: {note}", stage="human")]
        conn.execute("UPDATE invoices SET state='ELIGIBLE', decision='PAY_ELIGIBLE', outstanding=?, msme_deadline=?, reasons=?, explanation=? WHERE id=?",
                     (inv["total"], _msme_deadline(v, _d(inv["accepted_date"])), j(rs), f"Released by {actor} with note: {note}", inv_id))
        add_event(conn, "invoice", inv_id, "HELD", "ELIGIBLE", actor, {"note": note, "override": True})
    return None


def recheck(conn, inv_id, actor="user") -> None:
    with tx(conn):
        inv = q1(conn, "SELECT * FROM invoices WHERE id=?", (inv_id,))
        if inv["state"] not in ("HELD", "BLOCKED"):
            return
        rs = [r for r in unj(inv["reasons"], []) if r.get("stage") == "extract"]
        conn.execute("UPDATE invoices SET state='EXTRACTED', decision=NULL, reasons=?, attempts=0 WHERE id=?", (j(rs), inv_id))
        add_event(conn, "invoice", inv_id, inv["state"], "EXTRACTED", actor, {"action": "re-check"})
