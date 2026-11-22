import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common import DECISION_BADGE, db, facts_by_ids, inr, page, q, render_fact, st, unj  # noqa: E402

from app import vendor_admin  # noqa: E402

page("Vendor 360", "🏢")
conn = db()
st.title("🏢 Vendor 360")
vendors = q(conn, "SELECT * FROM vendors ORDER BY legal_name")
if not vendors:
    st.stop()
pending_ids = {r["vendor_id"] for r in q(conn, "SELECT vendor_id FROM vendor_bank_accounts WHERE status='pending_verification'")}
names = [f"{'⚠️ ' if v['id'] in pending_ids or v['status'] != 'active' else ''}{v['legal_name']}" for v in vendors]
i = st.selectbox("Vendor", range(len(vendors)), format_func=lambda k: names[k])
v = vendors[i]

st.subheader(f"{v['legal_name']}  ·  status **{v['status']}**")
a, b, c = st.columns(3)
a.markdown(f"**GSTIN** {v['gstin']}  \n**PAN** {v['pan']}  \n**Udyam** {v['udyam_no'] or '—'}")
b.markdown(f"**MSME** {v['msme_category']}{' (trader — 45-day rule n/a)' if v['is_trader'] else ''}  \n"
           f"**Written agreement** {'yes (45 days)' if v['has_written_agreement'] else 'no (15 days)'}  \n**Critical** {'yes' if v['is_critical'] else 'no'}")
c.markdown(f"**Domain** {v['domain']}  \n**Phone on file** {v['phone_on_file']}  \n**Location** {v['city']}, {v['state']}  \n"
           f"**Terms** {v['credit_days']} days" + (f", {v['discount_pct']:g}/{v['discount_days']}" if v["discount_pct"] else ""))


def breakdown(title, score, rows):
    st.markdown(f"#### {title}: **{score:.2f}**" if score is not None else f"#### {title}: not scored yet")
    for r in rows or []:
        cols = st.columns([2, 3, 1])
        cols[0].markdown(f"{'✅' if r['verified'] else '❔'} {r['signal']} <span class='small'>(w {r['weight']})</span>", unsafe_allow_html=True)
        cols[1].progress(float(r["value"]), text=r["note"][:90])
        cols[2].markdown(f"+{r['contribution']:.3f}")
        fs = facts_by_ids(conn, r.get("fact_ids"))
        if fs:
            with st.expander(f"evidence · {r['signal']}"):
                for f in fs:
                    render_fact(f)


l, r = st.columns(2)
with l:
    breakdown("Trust score (gate < 0.4 → HOLD)", v["trust_score"], unj(v["trust_breakdown"], []))
with r:
    breakdown("Priority score (order of payment)", v["priority_score"], unj(v["priority_breakdown"], []))
st.caption("Trust = is it safe to pay at all? Priority = who first? Kept separate on purpose. Missing signals score 0 and are marked ❔ (can't verify).")

st.markdown("#### Bank accounts")
accts = q(conn, "SELECT * FROM vendor_bank_accounts WHERE vendor_id=? ORDER BY created_at", (v["id"],))
st.dataframe([{"Status": x["status"], "A/c": f"****{x['account_no'][-4:]}", "IFSC": x["ifsc"], "Beneficiary": x["beneficiary_name"],
               "Valid from": (x["valid_from"] or "")[:10], "Valid to": (x["valid_to"] or "")[:10], "Proposed by": x["proposed_by"],
               "Approved by": x["approved_by"], "Note": x["verification_note"]} for x in accts], hide_index=True, use_container_width=True)
for x in accts:
    if x["status"] != "pending_verification":
        continue
    with st.container(border=True):
        st.warning(f"Pending bank change → A/c ****{x['account_no'][-4:]} / {x['ifsc']}, proposed by **{x['proposed_by']}**. "
                   "Emails can only propose. Maker-checker: a different person approves after verifying out of band.")
        st.caption("A request is never proof: a real vendor and a fraudster send the same polite email. "
                   "Every step below uses a channel the sender doesn't control, and is written to the Audit Ledger.")
        k = x["id"]
        who = st.text_input("Your name", key=f"who_{k}")
        note = st.text_input("Note", key=f"note_{k}")

        def done(e):
            st.error(e) if e else st.rerun()

        for row in vendor_admin.checklist(conn, k):
            mark = "✅" if row["done"] else ("➖" if row["done"] is None else "☐")
            st.markdown(f"{mark} **{row['step']}** <span class='small'>{row['detail'] or ''}</span>", unsafe_allow_html=True)
        c1, c2, c3 = st.columns(3)
        if c1.button("📄 Documents received", key=f"docs_{k}", disabled=bool(x["docs_received"])):
            done(vendor_admin.mark_docs_received(conn, k, who, note))
        if c2.button(f"☎️ I called {v['phone_on_file']}", key=f"cb_{k}", disabled=bool(x["callback_done"]),
                     help="Only the number already on file — never one from the email"):
            done(vendor_admin.record_callback(conn, k, who, note))
        if c3.button("🏦 Run penny drop (mock)", key=f"pd_{k}", disabled=x["penny_drop_status"] == "name_match",
                     help="penny_drop_mock: Razorpay Account Validation is not available in test mode"):
            err = vendor_admin.run_penny_drop(conn, k, who or "system")
            st.error(err) if err else st.rerun()
        with st.expander("👥 Second contact (Crustdata suggestion)"):
            sug = vendor_admin.suggest_second_contact(conn, k)
            if sug.get("ok"):
                for p in sug["contacts"]:
                    st.markdown(f"- **{p['name'] or '(name not in recording)'}**, {p['title']} · found on Crustdata ({sug['source']}), retrieved {sug['retrieved_at'][:16]}")
                st.caption("Someone other than the sender. Call them on the number on file, not a number from the email.")
            else:
                st.info(sug.get("why"))
            if st.button("Record second-contact confirmation", key=f"sc_{k}"):
                done(vendor_admin.record_second_contact(conn, k, who, note))
        with st.expander("✉️ Notify the old contact (draft → send)"):
            st.code(vendor_admin.draft_old_contact_notice(conn, k) or "", language=None)
            if st.button("Send (mock: logs an event, no real email)", key=f"send_{k}", disabled=bool(x["old_contact_notified_at"])):
                done(vendor_admin.send_old_contact_notice(conn, k, who))
        blockers = vendor_admin.approval_blockers(x, who)
        if not x["old_contact_notified_at"]:
            st.caption("⚠️ Old contact not notified yet (allowed, but flagged on approval).")
        ca, cb = st.columns(2)
        if ca.button("✅ Approve change (different person)", key=f"ap_{k}", disabled=bool(blockers) or not who.strip(),
                     help="; ".join(blockers) or None):
            done(vendor_admin.approve_bank_change(conn, k, who, True, note))
        if blockers:
            ca.caption("Approve disabled: " + "; ".join(blockers))
        if cb.button("❌ Reject change", key=f"rj_{k}"):
            done(vendor_admin.reject_bank_change(conn, k, who, note or "rejected"))
if v["status"] != "active":
    with st.form("lift"):
        st.markdown(f"Vendor is **{v['status']}**. Lifting the hold lets its eligible invoices into payment runs again.")
        who = st.text_input("Your name")
        note = st.text_input("Why")
        if st.form_submit_button("Lift hold"):
            err = vendor_admin.set_vendor_status(conn, v["id"], "active", who, note)
            st.error(err) if err else st.rerun()

st.markdown("#### Invoices & payments")
invs = q(conn, "SELECT * FROM invoices WHERE vendor_id=? ORDER BY rowid", (v["id"],))
st.dataframe([{"Invoice": i["invoice_no_raw"] or i["doc_kind"], "Date": i["invoice_date"], "Total": inr(i["total"]), "Outstanding": inr(i["outstanding"]),
               "State": i["state"], "Decision": DECISION_BADGE.get(i["decision"])} for i in invs], hide_index=True, use_container_width=True)
