"""Mock paywall and receipt. Uses the real approval + payout code path against the mock bank provider."""
from __future__ import annotations

import time

from common import TIER_BADGE, config, inr, mask, q, st, unj
from screens.overlays import audit_dialog, invoice_dialog

from app import payments, scheduler
from app.db import q1


def _latest_run(conn):
    return scheduler.current_run(conn) or q1(conn, "SELECT * FROM payment_runs ORDER BY created_at DESC LIMIT 1")


def _lines(conn, run_id):
    rows = q(conn, "SELECT n.*, i.invoice_no_raw, i.total, v.legal_name vendor, v.id vid FROM instalments n "
                   "JOIN invoices i ON i.id=n.invoice_id JOIN vendors v ON v.id=i.vendor_id "
                   "WHERE n.run_id=? AND n.status NOT IN ('CANCELLED') ORDER BY n.tier, n.created_at", (run_id,))
    for r in rows:
        acct = q1(conn, "SELECT account_no, ifsc FROM vendor_bank_accounts WHERE vendor_id=? AND status='current'", (r["vid"],))
        r["to"] = f"{mask(acct['account_no'])} · {acct['ifsc']}" if acct else "no verified account"
    return rows


def render(conn):
    ss = st.session_state
    if st.button("← Back to results"):
        ss.screen = "results"
        st.rerun()
    run = _latest_run(conn)
    if not run or run["status"] != "PROPOSED":
        st.info("There's no payment run waiting for approval.")
        return
    st.markdown("<div class='mock'>MOCK PAYMENT — test mode, no real money moves</div>", unsafe_allow_html=True)
    st.markdown("## Confirm payment")
    lines = _lines(conn, run["id"])
    cash = unj(run["summary"], {}).get("cash") or scheduler.cash_position(conn)
    total = sum(x["amount"] for x in lines)
    bank = scheduler.bank_position()

    left, right = st.columns([3, 2])
    with left:
        st.dataframe([{"Vendor": x["vendor"], "Invoice": x["invoice_no_raw"], "Priority": TIER_BADGE.get(x["tier"]),
                       "Amount": inr(x["amount"]), "To account (on file)": x["to"]} for x in lines],
                     hide_index=True, use_container_width=True)
        st.caption("Money only ever goes to the account on file. Part-payments follow the cash plan; the rest rolls to next week.")
    with right:
        with st.container(border=True):
            st.markdown(f"**Pay from** {bank.get('source', 'company current account').split(' statement')[0].split(' (')[0]}")
            st.metric(f"Total · {len(lines)} payment(s)", inr(total))
            after = cash["bank_balance"] - total
            st.markdown(f"Balance now **{inr(cash['bank_balance'])}**  \nAfter payment **{inr(after)}**"
                        + (f", **{inr(after + cash['expected_receipts'])}** once {inr(cash['expected_receipts'])} of expected "
                           f"customer receipts arrive" if cash["expected_receipts"] else "")
                        + f"  \n<span class='small'>Safety buffer {inr(cash['safety_buffer'])} (payroll, rent, taxes) stays covered — "
                          "the plan only spends what's left above it.</span>", unsafe_allow_html=True)
            with st.form("pay"):
                who = st.text_input("Approver name")
                ok = st.checkbox("I've reviewed every payment above")
                go = st.form_submit_button(f"Confirm · pay {inr(total)}", type="primary", use_container_width=True)
            if go:
                if not who.strip():
                    st.error("Enter your name — every approval is logged.")
                elif not ok:
                    st.error("Tick the review box.")
                else:
                    try:
                        with st.spinner("Contacting the bank (mock)…"):
                            scheduler.approve_run(conn, run["id"], who)
                            time.sleep(1.2)
                            ss.pay_stats = payments.execute_approved(conn, actor="ui")
                        ss.paid_run = run["id"]
                        ss.screen = "receipt"
                        st.rerun()
                    except ValueError as e:
                        st.error(str(e))


def render_receipt(conn):
    ss = st.session_state
    run = q1(conn, "SELECT * FROM payment_runs WHERE id=?", (ss.get("paid_run"),)) if ss.get("paid_run") else _latest_run(conn)
    if not run:
        st.info("No payments yet.")
        return
    lines = _lines(conn, run["id"])
    paid = [x for x in lines if x["status"] == "PAID"]
    failed = [x for x in lines if x["status"] == "FAILED"]
    st.markdown("<div class='mock'>MOCK RECEIPT — test mode</div>", unsafe_allow_html=True)
    st.markdown(f"## ✅ Paid {len(paid)} invoice(s) · {inr(sum(x['amount'] for x in paid))}")
    st.caption(f"Run {run['run_date']} · approved by {run['approved_by'] or '—'} · status {run['status']} · provider {config.PAYMENT_PROVIDER}")
    st.dataframe([{"Vendor": x["vendor"], "Invoice": x["invoice_no_raw"], "Amount": inr(x["amount"]), "To": x["to"],
                   "Status": x["status"], "Bank ref (UTR)": x["provider_ref"] or "—"} for x in lines],
                 hide_index=True, use_container_width=True)
    if failed:
        st.warning(f"{len(failed)} payment(s) failed — usually the vendor is on hold or has no verified account. They stay unpaid.")

    held = q(conn, "SELECT id, vendor_name_raw, invoice_no_raw, total, decision FROM invoices WHERE decision IN ('HOLD','BLOCK') ORDER BY rowid")
    if held:
        st.markdown(f"#### Still waiting on you ({len(held)})")
        for h in held:
            c = st.columns([4, 1])
            c[0].markdown(f"{'🟠' if h['decision'] == 'HOLD' else '🔴'} **{h['vendor_name_raw'] or 'unknown sender'}** · "
                          f"{h['invoice_no_raw'] or 'bank-change request'} · {inr(h['total']) if h['total'] else '—'}")
            if c[1].button("Open", key=f"open_{h['id']}", use_container_width=True):
                ss.inv_focus = h["id"]
                invoice_dialog(conn)

    b = st.columns(2)
    if b[0].button("📜 View audit trail", use_container_width=True):
        audit_dialog(conn, run["id"])
    if b[1].button("Start a new run", type="primary", use_container_width=True):
        ss.messages = []
        ss.pop("eval_log", None)
        ss.screen = "chat"
        st.rerun()
