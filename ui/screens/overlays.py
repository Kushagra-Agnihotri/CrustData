"""Overlays (st.dialog). Streamlit allows one open dialog at a time, so the bank-change
flow lives inside the invoice overlay as a tab, never as a dialog of its own."""
from __future__ import annotations

import hashlib

from common import DECISION_BADGE, config, facts_by_ids, inr, ist, mask, q, render_fact, render_reasons, st, unj

from app import bank_verify, pipeline, scheduler, vendor_admin
from app.db import q1

INBOX_EXT = (".txt", ".eml", ".pdf")


def inbox_files():
    return sorted(p for p in config.INBOX.iterdir() if p.suffix.lower() in INBOX_EXT) if config.INBOX.exists() else []


# --------------------------------------------------------------------------- database
def render_database(conn):
    seen = {r["source_path"].split("/")[-1]: r for r in q(conn, "SELECT source_path, state, decision FROM invoices")}
    files = inbox_files()
    vendors = {v["id"]: v["legal_name"] for v in q(conn, "SELECT id, legal_name FROM vendors")}
    t = st.tabs([f"📥 Inbox ({len(files)})", "🏢 Vendors", "📝 Purchase orders", "📦 Goods receipts", "🏦 Bank accounts", "💰 Receivables"])
    with t[0]:
        st.dataframe([{"File": p.name, "Type": p.suffix.lstrip(".").upper(), "Size": f"{p.stat().st_size / 1024:.1f} KB",
                       "Status": (DECISION_BADGE.get(seen[p.name]["decision"]) if p.name in seen else "⏳ not evaluated yet")}
                      for p in files], hide_index=True, use_container_width=True)
        pick = st.selectbox("Preview a document", [p.name for p in files], index=None, placeholder="Choose a file to read it")
        if pick:
            p = config.INBOX / pick
            if p.suffix.lower() == ".pdf":
                st.caption("PDF — text is extracted when evaluated.")
            else:
                st.code(p.read_text(encoding="utf-8", errors="replace"), language=None)
    with t[1]:
        st.dataframe([{"Vendor": v["legal_name"], "GSTIN": v["gstin"], "MSME": v["msme_category"], "Critical": bool(v["is_critical"]),
                       "Domain": v["domain"], "City": v["city"], "State": v["state"], "Credit days": v["credit_days"],
                       "Discount": f"{v['discount_pct']:g}%/{v['discount_days']}d" if v["discount_pct"] else "—",
                       "Since": v["relationship_since"], "Status": v["status"]}
                      for v in q(conn, "SELECT * FROM vendors ORDER BY legal_name")], hide_index=True, use_container_width=True)
    with t[2]:
        st.dataframe([{"PO": r["po_number"], "Vendor": vendors.get(r["vendor_id"]), "Line": r["line_no"], "Item": r["item"],
                       "Ordered": r["qty_ordered"], "Received": r["qty_received"], "Invoiced": r["qty_invoiced"],
                       "Unit price": inr(r["unit_price"], True), "GST": f"{r['gst_rate']:g}%", "PO date": r["po_date"], "Status": r["status"]}
                      for r in q(conn, "SELECT p.*, l.line_no, l.item, l.qty_ordered, l.qty_received, l.qty_invoiced, l.unit_price, l.gst_rate "
                                       "FROM purchase_orders p JOIN po_lines l ON l.po_id=p.id ORDER BY p.po_number, l.line_no")],
                     hide_index=True, use_container_width=True)
    with t[3]:
        st.dataframe([{"GRN": r["grn_number"], "PO": r["po_number"], "Vendor": vendors.get(r["vendor_id"]), "PO line": r["line_no"],
                       "Qty received": r["qty"], "Received on": r["received_date"]}
                      for r in q(conn, "SELECT g.*, p.po_number, p.vendor_id FROM grns g JOIN purchase_orders p ON p.id=g.po_id ORDER BY g.received_date")],
                     hide_index=True, use_container_width=True)
    with t[4]:
        st.dataframe([{"Vendor": vendors.get(r["vendor_id"]), "Account": mask(r["account_no"]), "IFSC": r["ifsc"], "Beneficiary": r["beneficiary_name"],
                       "Valid from": (r["valid_from"] or "")[:10], "Status": r["status"], "Proposed by": r["proposed_by"]}
                      for r in q(conn, "SELECT * FROM vendor_bank_accounts ORDER BY vendor_id, status")], hide_index=True, use_container_width=True)
    with t[5]:
        st.dataframe([{"Customer": r["legal_name"], "Invoice": r["invoice_no"], "Amount": inr(r["amount"]), "Due": r["due_date"],
                       "Confidence": f"{r['confidence']:.0%}" if r["confidence"] is not None else "—", "Status": r["status"]}
                      for r in q(conn, "SELECT r.*, c.legal_name FROM receivables r LEFT JOIN customers c ON c.id=r.customer_id ORDER BY r.due_date")],
                     hide_index=True, use_container_width=True)


@st.dialog("Invoice database", width="large")
def database_dialog(conn):
    st.caption("The inbox and the records every check runs against. Read-only.")
    render_database(conn)


@st.dialog("Add invoices", width="large")
def add_invoices_dialog(conn):
    st.caption("Drop invoice files (.txt, .eml, .pdf). They land in the inbox and are evaluated on your next prompt.")
    ups = st.file_uploader("Invoice files", type=["txt", "eml", "pdf"], accept_multiple_files=True, label_visibility="collapsed")
    if ups and st.button(f"Add {len(ups)} file(s) to the inbox", type="primary"):
        known = {hashlib.sha256(p.read_bytes()).hexdigest() for p in inbox_files()}
        added, dup = 0, 0
        config.INBOX.mkdir(parents=True, exist_ok=True)
        for u in ups:
            data = u.getvalue()
            if hashlib.sha256(data).hexdigest() in known:
                dup += 1
                continue
            (config.INBOX / u.name).write_bytes(data)
            added += 1
        st.session_state.flash = f"Added {added} file(s) to the inbox" + (f" · {dup} identical file(s) skipped" if dup else "") + "."
        st.rerun()


# --------------------------------------------------------------------------- invoice overlay
def overlay_order(conn) -> list[str]:
    rank = {"HOLD": 0, "BLOCK": 1, "PAY_ELIGIBLE": 2}
    rows = q(conn, "SELECT id, decision FROM invoices WHERE decision IS NOT NULL ORDER BY rowid")
    return [r["id"] for r in sorted(rows, key=lambda r: rank.get(r["decision"], 3))]


def open_invoice(inv_id: str | None = None):
    st.session_state.inv_focus = inv_id
    st.session_state.open_overlay = "invoice"


def _replan(conn):
    run = scheduler.current_run(conn)
    if not run or run["status"] in ("DRAFT", "PROPOSED"):
        try:
            scheduler.propose_run(conn)
        except ValueError:
            pass


@st.dialog("Explore each invoice", width="large")
def invoice_dialog(conn):
    ss = st.session_state
    ids = overlay_order(conn)
    if not ids:
        st.info("Nothing evaluated yet.")
        return
    if ss.get("inv_focus") in ids:
        ss.inv_idx = ids.index(ss.inv_focus)
        ss.inv_focus = None
    ss.inv_idx = min(ss.get("inv_idx", 0), len(ids) - 1)
    n1, n2, n3 = st.columns([1, 3, 1])
    if n1.button("‹ Prev", disabled=ss.inv_idx == 0, use_container_width=True):
        ss.inv_idx -= 1
        st.rerun(scope="fragment")
    n2.markdown(f"<div style='text-align:center;padding-top:.4rem'>{ss.inv_idx + 1} of {len(ids)}"
                "<span class='small'> · held first, then blocked, then payable</span></div>", unsafe_allow_html=True)
    if n3.button("Next ›", disabled=ss.inv_idx >= len(ids) - 1, use_container_width=True):
        ss.inv_idx += 1
        st.rerun(scope="fragment")

    inv = q1(conn, "SELECT * FROM invoices WHERE id=?", (ids[ss.inv_idx],))
    v = q1(conn, "SELECT * FROM vendors WHERE id=?", (inv["vendor_id"],)) if inv["vendor_id"] else None
    title = inv["invoice_no_raw"] or ("Bank change" if inv["doc_kind"] == "bank_change" else inv["source_path"].split("/")[-1][:18])
    st.markdown(f"### {DECISION_BADGE.get(inv['decision'])} · {v['legal_name'] if v else (inv['vendor_name_raw'] or 'Unknown sender')}")
    facts = [("Document", title), ("Amount", inr(inv["total"]) if inv["total"] else None), ("Due", inv["due_date"]),
             ("Vendor trust", f"{v['trust_score']:.2f}" if v and v["trust_score"] is not None else None)]
    st.markdown("  ·  ".join(f"<span class='small'>{k}</span> **{x}**" for k, x in facts if x), unsafe_allow_html=True)
    if inv["explanation"]:
        st.markdown(f"> {inv['explanation']}")

    tabs = st.tabs(["Why", "Details", "Vendor", "🏦 Bank details"])
    with tabs[0]:
        render_reasons(conn, unj(inv["reasons"], []), inv["id"])
        _actions(conn, inv)
    with tabs[1]:
        _details(conn, inv, v)
    with tabs[2]:
        if v:
            _vendor(conn, v)
        else:
            st.info("This document isn't tied to a vendor in our master.")
    with tabs[3]:
        if v:
            bank_tab(conn, v)
        else:
            st.info("No vendor on file, so there are no bank details to check.")


def _actions(conn, inv):
    if inv["state"] not in ("HELD", "BLOCKED"):
        return
    st.markdown("##### What you can do")
    a, b = st.columns(2)
    if a.button("🔁 Re-check", key=f"rc_{inv['id']}", help="Run every check again (e.g. after fixing the vendor master)"):
        pipeline.recheck(conn, inv["id"], actor="ui")
        pipeline.process_one(conn, inv["id"], pipeline.start_run(conn), actor="ui")
        _replan(conn)
        st.rerun(scope="fragment")
    if inv["state"] == "HELD" and inv["doc_kind"] == "invoice":
        with b.form(f"rel_{inv['id']}"):
            who = st.text_input("Your name (approver)")
            note = st.text_input("Why is it OK to pay? (logged)")
            if st.form_submit_button("✅ Release for payment"):
                err = pipeline.release_with_note(conn, inv["id"], who, note)
                if err:
                    st.error(err)
                else:
                    _replan(conn)
                    st.session_state.flash = "Released — it's now in the payment run."
                    st.rerun()
    elif inv["state"] == "BLOCKED":
        b.caption("Blocked documents can't be released. Fix the root cause (for example the vendor master) and re-check.")


def _details(conn, inv, v):
    fields = {"Vendor (as written)": inv["vendor_name_raw"], "GSTIN": inv["gstin_raw"], "Invoice no": inv["invoice_no_raw"],
              "Invoice date": inv["invoice_date"], "PO": inv["po_number_raw"], "Sub-total": inr(inv["subtotal"]) if inv["subtotal"] else None,
              "Tax": inr(inv["tax"]) if inv["tax"] else None, "Total": inr(inv["total"]) if inv["total"] else None,
              "Bank a/c on invoice": mask(inv["bank_account_raw"]) if inv["bank_account_raw"] else None, "IFSC": inv["ifsc_raw"],
              "Sender": inv["sender_email"], "Reply-To": inv["reply_to"],
              "Signatory": f"{inv['signatory']}, {inv['signatory_title']}" if inv["signatory"] else None,
              "Accepted (MSME clock)": inv["accepted_date"], "Due": inv["due_date"], "MSME deadline": inv["msme_deadline"]}
    st.table({k: str(x) for k, x in fields.items() if x not in (None, "None")})
    if v and inv["doc_kind"] == "invoice" and inv["decision"] == "PAY_ELIGIBLE":
        try:
            c = scheduler.classify(conn, inv, v)
            st.caption("Payment priority: " + ", ".join(f"{k} = {x}" for k, x in c.items() if not isinstance(x, (dict, list)))[:400])
        except Exception:
            pass
    lines = q(conn, "SELECT line_no, item, qty, unit_price, gst_rate, amount FROM invoice_lines WHERE invoice_id=?", (inv["id"],))
    if lines:
        st.dataframe([{"#": x["line_no"], "Item": x["item"], "Qty": x["qty"], "Rate": inr(x["unit_price"], True), "GST %": x["gst_rate"],
                       "Amount": inr(x["amount"], True)} for x in lines], hide_index=True, use_container_width=True)
    with st.expander("Original document"):
        p = config.ROOT / inv["source_path"]
        st.code(p.read_text(encoding="utf-8", errors="replace")[:4000] if p.exists() else "file missing", language="text")


def _vendor(conn, v):
    a, b = st.columns(2)
    a.markdown(f"**{v['legal_name']}** · status **{v['status']}**  \nGSTIN {v['gstin']} · PAN {v['pan']}  \n"
               f"MSME {v['msme_category']} · critical {'yes' if v['is_critical'] else 'no'}")
    b.markdown(f"Domain **{v['domain']}**  \nPhone on file **{v['phone_on_file']}**  \n{v['city']}, {v['state']} · {v['credit_days']}-day terms")
    st.markdown(f"##### Trust score: **{v['trust_score']:.2f}**" if v["trust_score"] is not None else "##### Trust score: not scored yet")
    for r in unj(v["trust_breakdown"], []):
        c = st.columns([2, 3, 1])
        c[0].markdown(f"{'✅' if r['verified'] else '❔'} {r['signal'].replace('_', ' ')}")
        c[1].progress(float(r["value"]), text=r["note"][:90])
        c[2].markdown(f"+{r['contribution']:.3f}")
        fs = facts_by_ids(conn, r.get("fact_ids"))
        if fs:
            with st.expander(f"evidence · {r['signal'].replace('_', ' ')}"):
                for f in fs:
                    render_fact(f)
    st.caption("Trust = is it safe to pay at all (below 0.4 → hold). Missing signals score 0 and are marked ❔.")
    if v["status"] != "active":
        with st.form(f"lift_{v['id']}"):
            st.markdown(f"Vendor is **{v['status']}**. Lifting the hold lets its eligible invoices into payment runs again.")
            who = st.text_input("Your name")
            note = st.text_input("Why")
            if st.form_submit_button("Lift hold"):
                err = vendor_admin.set_vendor_status(conn, v["id"], "active", who, note)
                st.error(err) if err else st.rerun(scope="fragment")


# --------------------------------------------------------------------------- bank details tab
VERDICT_ICON = {bank_verify.GENUINE: "🟢", bank_verify.UNVERIFIED: "🟡", bank_verify.FRAUD: "🔴"}
STATUS_ICON = {"ok": "✅", "warn": "🟠", "bad": "🔴", "unknown": "❔"}


def bank_tab(conn, v):
    ss = st.session_state
    accts = q(conn, "SELECT * FROM vendor_bank_accounts WHERE vendor_id=? ORDER BY created_at", (v["id"],))
    cur = next((x for x in accts if x["status"] == "current"), None)
    st.markdown(f"**On file:** A/c {mask(cur['account_no'])} · {cur['ifsc']} · {cur['beneficiary_name'] or v['legal_name']}" if cur
                else "**On file:** no verified account")
    st.caption("We only ever pay the account on file. A request to change it — however polite — is never proof on its own.")

    pending = [x for x in accts if x["status"] == "pending_verification"]
    st.markdown("##### Verify a change request")
    k = v["id"]
    em = st.text_area("Paste the email (include the From / Reply-To lines if you can)", key=f"bv_em_{k}", height=180,
                      placeholder="From: \"Name\" <name@vendor.com>\nSubject: Change of bank details\n\nDear Accounts Team, …")
    sms = st.text_area("Paste the SMS / WhatsApp message (optional)", key=f"bv_sms_{k}", height=80)
    if st.button("🔎 Verify request", key=f"bv_go_{k}", disabled=not (em.strip() or sms.strip()), type="primary"):
        with st.spinner("Checking the sender, the person, the bank branch and the vendor's record…"):
            ss[f"bv_res_{k}"] = bank_verify.verify(conn, k, em, sms)
    res = ss.get(f"bv_res_{k}")
    if res:
        st.markdown(f"<div class='verdict {res['verdict']}'>{VERDICT_ICON[res['verdict']]} {res['headline']}</div>", unsafe_allow_html=True)
        f = res["fields"]
        st.caption(f"Read from what you pasted ({f['method']}): new A/c {mask(f['account_no'])} · IFSC {f['ifsc'] or '—'} · "
                   f"beneficiary {f['beneficiary_name'] or '—'} · requester {f['signatory'] or '—'}"
                   + (f", {f['signatory_title']}" if f['signatory_title'] else "") + f" · sender {f['sender'] or '—'}")
        for c in sorted(res["checks"], key=lambda c: ["bad", "warn", "unknown", "ok"].index(c["status"])):
            st.markdown(f"{STATUS_ICON[c['status']]} {c['message']}  \n<span class='small'>{'🌐 live · ' if c['live'] else ''}{c['source']}</span>",
                        unsafe_allow_html=True)
        st.info(f"Whatever the verdict: call **{res['phone_on_file']}** (the number on file, never one from the message), "
                "then a second person approves. Nothing is paid to the new account until then.")
        if st.button("📝 Record as a pending change and start the checklist", key=f"bv_rec_{k}"):
            err = bank_verify.record_request(conn, k, res, actor="ui")
            if err:
                st.warning(err)
            else:
                ss.pop(f"bv_res_{k}", None)
                st.rerun(scope="fragment")

    if pending:
        st.markdown("##### Pending change — finish the checklist")
        for x in pending:
            checklist(conn, v, x)


def _done(ok):
    return "✅" if ok else "☐"


def checklist(conn, v, x):
    k = x["id"]
    with st.container(border=True):
        st.warning(f"Pending change → A/c {mask(x['account_no'])} / {x['ifsc']}, proposed by **{x['proposed_by']}**. "
                   "Each step uses a channel the requester doesn't control, and every step is logged.")
        who = st.text_input("Your name", key=f"who_{k}")
        st.markdown(f"**{_done(x['docs_received'])} 1 · Documents** — cancelled cheque or bank letter on the vendor's letterhead")
        if not x["docs_received"] and st.button("Mark documents received", key=f"docs_{k}"):
            err = vendor_admin.mark_docs_received(conn, k, who)
            st.error(err) if err else st.rerun(scope="fragment")
        phone = vendor_admin.callback_number(conn, k)
        st.markdown(f"**{_done(x['callback_done'])} 2 · Call back on the number on file: {phone}** — never a number from the message"
                    + (f"  \nDone by **{x['callback_by']}**: {x['callback_note']}" if x["callback_done"] else ""))
        if not x["callback_done"]:
            cb = st.text_input("What the vendor confirmed on the call", key=f"cbn_{k}")
            if st.button(f"I called {phone}", key=f"cb_{k}"):
                err = vendor_admin.record_callback(conn, k, who, cb)
                st.error(err) if err else st.rerun(scope="fragment")
        st.markdown(f"**{_done(x['second_contact_note'])} 3 · Second contact (recommended)** — someone *else* at the vendor")
        if st.button("Suggest a contact from Crustdata", key=f"sc_{k}"):
            sug = vendor_admin.suggest_second_contact(conn, k)
            for m in sug.get("suggestions", []):
                st.markdown(f"- **{m.get('title') or 'title n/a'}**{' · ' + m['name'] if m.get('name') else ''} "
                            f"(Crustdata, retrieved {ist(sug['retrieved_at'])})")
            st.caption(sug.get("note", ""))
        if not x["second_contact_note"]:
            sc = st.text_input("Who else confirmed (name, role, how)", key=f"scn_{k}")
            if st.button("Record second confirmation", key=f"scb_{k}"):
                err = vendor_admin.record_second_contact(conn, k, who, sc)
                st.error(err) if err else st.rerun(scope="fragment")
        pd = {"not_run": "not run", "name_match": f"bank name **{x['penny_drop_name']}** matches", "failed": "validation failed",
              "name_mismatch": f"⚠️ bank says the account belongs to **{x['penny_drop_name']}**"}
        st.markdown(f"**{_done(x['penny_drop_status'] == 'name_match')} 4 · Penny drop (mock)**: {pd[x['penny_drop_status']]}")
        if x["penny_drop_status"] == "not_run" and st.button("Run penny drop", key=f"pd_{k}"):
            err = vendor_admin.run_penny_drop(conn, k, who or "system")
            st.error(err) if err else st.rerun(scope="fragment")
        st.markdown(f"**{_done(x['old_contact_notified_at'])} 5 · Tell the old contact on file**"
                    + (f" — sent {ist(x['old_contact_notified_at'])} (mock)" if x["old_contact_notified_at"] else ""))
        if not x["old_contact_notified_at"]:
            d = vendor_admin.draft_old_contact_notice(conn, k)
            st.text_area(f"Draft to {d['to']}", d["text"], height=110, key=f"nt_{k}", disabled=True)
            if st.button("Send notice (mock)", key=f"ns_{k}"):
                err = vendor_admin.send_old_contact_notice(conn, k, who)
                st.error(err) if err else st.rerun(scope="fragment")
        blockers = vendor_admin.approval_blockers(x, who or None)
        st.markdown("**6 · Approve** — a different person from the proposer and the caller")
        note = st.text_input("Approval / rejection note", key=f"an_{k}")
        ca, cb2 = st.columns(2)
        if blockers:
            st.caption("Approve is disabled: " + "; ".join(blockers))
        if ca.button("Approve change", key=f"ap_{k}", disabled=bool(blockers) or not who):
            err = vendor_admin.approve_bank_change(conn, k, who, note)
            st.error(err) if err else st.rerun(scope="fragment")
        if cb2.button("Reject change", key=f"rj_{k}"):
            err = vendor_admin.reject_bank_change(conn, k, who, note or "rejected")
            st.error(err) if err else st.rerun(scope="fragment")


# --------------------------------------------------------------------------- audit trail
@st.dialog("Audit trail", width="large")
def audit_dialog(conn, run_id: str | None):
    st.caption("Append-only: database triggers refuse UPDATE/DELETE on events and facts.")
    t1, t2, t3 = st.tabs(["Payments", "Events", "Calls & cost"])
    with t1:
        rows = q(conn, "SELECT v.legal_name vendor, i.invoice_no_raw invoice, n.amount, n.status, n.provider_ref, n.approved_by, n.paid_at "
                       "FROM instalments n JOIN invoices i ON i.id=n.invoice_id JOIN vendors v ON v.id=i.vendor_id "
                       "WHERE n.run_id=? ORDER BY n.tier", (run_id,)) if run_id else []
        st.dataframe([dict(r, amount=inr(r["amount"])) for r in rows], hide_index=True, use_container_width=True)
        st.caption("Every PAID instalment maps to exactly one provider payout (same idempotency key).")
    with t2:
        st.dataframe(q(conn, "SELECT seq, at, entity_type, from_state, to_state, actor, detail FROM events ORDER BY seq DESC LIMIT 300"),
                     hide_index=True, use_container_width=True)
    with t3:
        tot = q1(conn, "SELECT COUNT(*) n, COALESCE(SUM(credits),0) cr, COALESCE(SUM(est_cost_inr),0) c FROM calls")
        st.markdown(f"**{tot['n']}** external calls · **{tot['cr']:.1f}** Crustdata credits · est. **₹{tot['c']:.2f}**")
        st.dataframe(q(conn, "SELECT at, tool, endpoint, mode, credits, est_cost_inr, latency_ms, ok, error FROM calls ORDER BY id DESC LIMIT 300"),
                     hide_index=True, use_container_width=True)
