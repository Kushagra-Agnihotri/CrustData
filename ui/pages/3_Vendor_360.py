import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common import DECISION_BADGE, db, facts_by_ids, inr, ist, page, q, render_fact, st, unj  # noqa: E402

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
def _done(ok):
    return "✅" if ok else "☐"


for x in accts:
    if x["status"] != "pending_verification":
        continue
    k = x["id"]
    with st.container(border=True):
        st.warning(f"Pending bank change → A/c ****{x['account_no'][-4:]} / {x['ifsc']}, proposed by **{x['proposed_by']}**. "
                   "A request is never proof: a real vendor and a fraudster send the same polite email. Each step below "
                   "uses a channel the requester doesn't control, and every step is written to the Audit Ledger.")
        who = st.text_input("Your name", key=f"who_{k}")

        st.markdown(f"**{_done(x['docs_received'])} 1 · Documents** — cancelled cheque / bank letter on the vendor's letterhead")
        if not x["docs_received"] and st.button("Mark documents received", key=f"docs_{k}"):
            err = vendor_admin.mark_docs_received(conn, k, who)
            st.error(err) if err else st.rerun()

        phone = vendor_admin.callback_number(conn, k)
        st.markdown(f"**{_done(x['callback_done'])} 2 · Call-back on the number on file: {phone}** "
                    "(never a number from the email) — ask for a known contact, read back the new account number."
                    + (f"  \nDone by **{x['callback_by']}**: {x['callback_note']}" if x["callback_done"] else ""))
        if not x["callback_done"]:
            cb_note = st.text_input("What the vendor confirmed on the call", key=f"cbn_{k}")
            if st.button(f"I called {phone}", key=f"cb_{k}"):
                err = vendor_admin.record_callback(conn, k, who, cb_note)
                st.error(err) if err else st.rerun()

        st.markdown(f"**{_done(x['second_contact_note'])} 3 · Second contact (recommended)** — someone *else* at the vendor, "
                    "not the person who sent the email")
        if st.button("Suggest a contact from Crustdata", key=f"sc_{k}"):
            sug = vendor_admin.suggest_second_contact(conn, k)
            if sug["suggestions"]:
                when = ist(sug["retrieved_at"])
                for m in sug["suggestions"]:
                    st.markdown(f"- **{m.get('title') or 'title n/a'}**{' · ' + m['name'] if m.get('name') else ''} — found on Crustdata "
                                f"({'live, recorded ' if sug['recorded'] else ''}retrieved {when})")
                st.caption(sug["note"])
            else:
                st.info(sug["note"] if "note" in sug else "No finance/accounts contact found on Crustdata.")
        if not x["second_contact_note"]:
            sc_note = st.text_input("Who else confirmed (name, role, how)", key=f"scn_{k}")
            if st.button("Record second confirmation", key=f"scb_{k}"):
                err = vendor_admin.record_second_contact(conn, k, who, sc_note)
                st.error(err) if err else st.rerun()
        else:
            st.caption(x["second_contact_note"])

        pd_txt = {"not_run": "not run", "name_match": f"bank name **{x['penny_drop_name']}** matches the vendor",
                  "name_mismatch": f"⚠️ bank says the account belongs to **{x['penny_drop_name']}**", "failed": "can't verify: validation failed"}
        st.markdown(f"**{_done(x['penny_drop_status'] == 'name_match')} 4 · Penny drop** (`penny_drop_mock` — Razorpay Account "
                    f"Validation isn't available in test mode; live in production): {pd_txt[x['penny_drop_status']]}")
        if x["penny_drop_status"] == "not_run" and st.button("Run penny drop (mock)", key=f"pd_{k}"):
            err = vendor_admin.run_penny_drop(conn, k, who or "system")
            st.error(err) if err else st.rerun()

        st.markdown(f"**{_done(x['old_contact_notified_at'])} 5 · Tell the old contact on file**"
                    + (f" — sent {ist(x['old_contact_notified_at'])} (mock, logged)" if x["old_contact_notified_at"] else ""))
        if not x["old_contact_notified_at"]:
            d = vendor_admin.draft_old_contact_notice(conn, k)
            st.text_area(f"Draft to {d['to']}", d["text"], height=130, key=f"nt_{k}", disabled=True)
            if st.button("Send notice (needs your click · mock)", key=f"ns_{k}"):
                err = vendor_admin.send_old_contact_notice(conn, k, who)
                st.error(err) if err else st.rerun()

        blockers = vendor_admin.approval_blockers(x, who or None)
        st.markdown(f"**{_done(False)} 6 · Approve (a different person from the proposer and the caller)**")
        note = st.text_input("Approval / rejection note", key=f"an_{k}")
        ca, cb = st.columns(2)
        if blockers:
            ca.caption("Approve is disabled: " + "; ".join(blockers))
        if ca.button("Approve change", key=f"ap_{k}", disabled=bool(blockers) or not who):
            err = vendor_admin.approve_bank_change(conn, k, who, note)
            st.error(err) if err else st.rerun()
        if cb.button("Reject change", key=f"rj_{k}"):
            err = vendor_admin.reject_bank_change(conn, k, who, note or "rejected")
            st.error(err) if err else st.rerun()
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
