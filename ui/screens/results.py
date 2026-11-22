"""Results screen: only what needs a decision, plus the way into each invoice and to payment."""
from __future__ import annotations

from common import DECISION_BADGE, inr, st
from screens.chat import STAGE_LABEL
from screens.overlays import _replan, invoice_dialog

from app import summary


def back_to_chat():
    if st.button("← Back to chat"):
        st.session_state.screen = "chat"
        st.rerun()


def render(conn):
    ss = st.session_state
    back_to_chat()
    if ss.get("flash"):
        st.toast(ss.pop("flash"))
    s = summary.week_summary(conn)
    if not s["docs"]:
        st.info("Nothing evaluated yet — go back to the chat and send a prompt.")
        return
    st.markdown(f"## Results · {ss.get('run_scope', 'this week')}")
    st.caption(f"{s['docs']} document(s) evaluated so far · {s['invoices']} invoice(s) worth {inr(s['value'])}"
               + (f" · {s['bank_change']} bank-change request(s)" if s["bank_change"] else ""))

    status = s["run_status"]
    paid = status in ("APPROVED", "EXECUTING", "DONE")
    tiles = [("Paid this run" if paid else "🟢 Ready to pay", inr(s["planned"]), f"{s['payments']} payment(s)"),
             ("🟠 Held for you", s["held"], f"{inr(s['held_value'])} of invoices"),
             ("🔴 Blocked", s["blocked"], f"{inr(s['blocked_value'])} of invoices"),
             ("♊ Duplicates caught", s["duplicates"], f"{inr(s['duplicate_value'])} not paid twice")]
    for col, (label, value, note) in zip(st.columns(4), tiles):
        with col.container(border=True):
            st.metric(label, value)
            st.caption(note)
    if s["spendable"] is not None:
        st.caption(f"{inr(s['spendable'])} available to spend after the safety buffer · {inr(s['planned'])} planned now"
                   + (f" · {s['deferred']} eligible invoice(s) deferred to next week for cash" if s["deferred"] else ""))
    if s["fraud"]:
        st.error(f"🚨 {s['fraud']} document(s) show fraud indicators — {inr(s['fraud_protected'])} of payables protected.")

    need = [r for r in s["rows"] if r["invoice"]["decision"] in ("HOLD", "BLOCK")]
    if need:
        st.markdown("#### Needs your decision")
        st.dataframe([{"Vendor": r["invoice"]["vendor_name_raw"] or "unknown sender",
                       "Document": r["invoice"]["invoice_no_raw"] or ("bank-change request" if r["invoice"]["doc_kind"] == "bank_change" else "—"),
                       "Amount": inr(r["invoice"]["total"]) if r["invoice"]["total"] else "—",
                       "Decision": DECISION_BADGE.get(r["invoice"]["decision"]),
                       "Why": r["top_reason"][:140]} for r in need],
                     hide_index=True, use_container_width=True)
    else:
        st.success("Nothing needs your decision — every document passed its checks.")

    rest = [r for r in s["rows"] if r["invoice"]["decision"] == "PAY_ELIGIBLE"]
    with st.expander(f"🟢 Payable ({len(rest)})"):
        st.dataframe([{"Vendor": r["invoice"]["vendor_name_raw"], "Invoice": r["invoice"]["invoice_no_raw"],
                       "Total": inr(r["invoice"]["total"]), "Now": inr(r["planned"]) if r["planned"] else "—", "Plan": r["outcome"]}
                      for r in rest], hide_index=True, use_container_width=True)

    b = st.columns(2)
    if b[0].button("🔍 Explore each invoice", use_container_width=True):
        ss.inv_idx = 0
        invoice_dialog(conn)
    if paid:
        if b[1].button("🧾 View receipt", type="primary", use_container_width=True):
            ss.screen = "receipt"
            st.rerun()
    elif b[1].button(f"💳 Pay now · {inr(s['planned'])}", type="primary", use_container_width=True, disabled=not s["payments"]):
        _replan(conn)  # include anything released in the overlay
        ss.screen = "paywall"
        st.rerun()

    log = ss.get("eval_log")
    if log:
        with st.expander(f"🔎 How each document was checked ({len(log)})"):
            for e in log:
                st.markdown(f"**{e['label']}**")
                for stage, lines in e["stages"]:
                    st.markdown(f"<span class='small'>{STAGE_LABEL.get(stage, stage)}</span>", unsafe_allow_html=True)
                    for ln in lines:
                        st.markdown(f"&nbsp;&nbsp;{ln}", unsafe_allow_html=True)
                st.caption(e.get("explanation") or "")
