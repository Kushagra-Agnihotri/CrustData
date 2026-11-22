import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common import TIER_BADGE, db, facts_by_ids, inr, page, q, render_fact, st, unj  # noqa: E402

from app import payments, scheduler  # noqa: E402
from app.db import q1  # noqa: E402

page("Payment Run", "💸")
conn = db()
st.title("💸 Payment Run")
st.caption("Don't clear everything at once: pay according to liquidity, in instalments where needed, and justify every rupee.")

run = scheduler.current_run(conn) or q1(conn, "SELECT * FROM payment_runs ORDER BY created_at DESC LIMIT 1")
if not run:
    if st.button("Plan this week's payment run", type="primary"):
        scheduler.propose_run(conn)
        st.rerun()
    st.stop()

summary = unj(run["summary"], {})
cash = summary.get("cash") or scheduler.cash_position(conn)

# ---- cash bar
st.markdown("### Cash available for this run")
c = st.columns(5)
c[0].metric("Bank balance", inr(cash["bank_balance"]), cash.get("balance_as_of", "")[:10], delta_color="off")
c[1].metric("− Safety buffer", inr(cash["safety_buffer"]), "never touched", delta_color="off")
c[2].metric("+ Expected receipts", inr(cash["expected_receipts"]), f"{len(cash['receipts_counted'])} confident receipt(s)", delta_color="off")
c[3].metric("= Spendable", inr(cash["spendable"]), delta_color="off")
insts = q(conn, "SELECT n.*, i.invoice_no_raw, i.total, i.outstanding inv_out, v.legal_name vendor FROM instalments n "
                "JOIN invoices i ON i.id=n.invoice_id JOIN vendors v ON v.id=i.vendor_id WHERE n.run_id=? ORDER BY n.tier, n.created_at", (run["id"],))
alloc = sum(x["amount"] for x in insts if x["status"] != "CANCELLED")
paid = sum(x["amount"] for x in insts if x["status"] == "PAID")
c[4].metric("Allocated / paid", inr(alloc), f"{inr(paid)} paid so far", delta_color="off")
st.progress(min(1.0, alloc / cash["spendable"]) if cash["spendable"] > 0 else 0.0, text=f"{inr(alloc)} of {inr(cash['spendable'])} spendable allocated")
with st.expander("Where these numbers come from"):
    st.markdown(f"**Bank balance** — {cash.get('balance_source')} as of {cash.get('balance_as_of')}")
    st.markdown("**Safety buffer** — " + ", ".join(f"{b['label']} {inr(b['amount'])}" for b in cash["buffer_items"]))
    for r in cash["receipts_counted"]:
        st.markdown(f"✅ {r['customer']} {r['invoice_no']}: {inr(r['amount'])} due {r['due_date']} × confidence {r['confidence']:.0%} = **{inr(r['counted'])}**")
    for r in cash["receipts_not_counted"]:
        st.markdown(f"➖ {r['customer']} {r['invoice_no']}: {inr(r['amount'])} — {r['why_not']}")

for a in summary.get("alerts", []):
    {"error": st.error, "warning": st.warning, "info": st.info}[a["level"]](a["text"])

st.markdown(f"### Allocation · run status **{run['status']}**" + (f" · approved by **{run['approved_by']}**" if run["approved_by"] else ""))
editable = run["status"] == "PROPOSED"
overrides = {}
for x in insts:
    jx = unj(x["justification"], {})
    with st.container(border=True):
        h1, h2, h3 = st.columns([2.2, 3.2, 1.6])
        h1.markdown(f"{TIER_BADGE.get(x['tier'])}  \n**{x['vendor']}**  \n<span class='small'>{x['invoice_no_raw']} · total {inr(x['total'])}</span>",
                    unsafe_allow_html=True)
        h2.markdown(f"**{jx.get('headline')}**" + (" · ✏️ approver override" if x["approver_override"] else ""))
        for r in jx.get("reasons", []):
            h2.markdown(f"- {r['text']}")
        rem = jx.get("remaining")
        if rem:
            h2.markdown(f"<span class='small'>Remaining {inr(rem['amount'])} planned {rem['planned_date']} — {rem['because']}</span>", unsafe_allow_html=True)
        if editable:
            out = x["proposed_amount"] + x["discount_amount"] if x["tier"] == 1 else None
            mx = (x["inv_out"] or x["total"]) // 100
            v = h3.number_input("Amount now (₹)", min_value=0, max_value=int(mx), value=int(x["amount"] // 100), step=1000, key=f"amt_{x['id']}")
            if v * 100 != x["amount"]:
                overrides[x["invoice_id"]] = v * 100
            h3.caption(f"min instalment {inr(min(x['inv_out'] or x['total'], max(int(x['total'] * 0.25), 1_000_000)))}")
        else:
            status_icon = {"PAID": "✅", "FAILED": "❌", "SUBMITTED": "📤", "INTENT_WRITTEN": "📝", "APPROVED": "👍"}.get(x["status"], "•")
            h3.metric("Amount", inr(x["amount"]), f"{status_icon} {x['status']}", delta_color="off")
            if x["provider_ref"]:
                h3.caption(f"ref {x['provider_ref']}")

deferred = summary.get("deferred", [])
if deferred:
    st.markdown("#### Not paid this run")
    st.dataframe([{"Tier": d["tier"], "Vendor": d["vendor"], "Invoice": d["invoice_no"], "Outstanding": inr(d["outstanding"]), "Why ₹0 now": d["why"]}
                  for d in deferred], hide_index=True, use_container_width=True)

if editable:
    b1, b2 = st.columns(2)
    if b1.button("🔄 Recompute with my changes", disabled=not overrides, help="Fixes your amounts and re-flows the remaining cash across Tier 2"):
        scheduler.propose_run(conn, overrides=overrides, actor="approver-edit")
        st.rerun()
    with b2.form("approve"):
        who = st.text_input("Approver name")
        ok = st.checkbox("I have reviewed every line above")
        if st.form_submit_button("✅ Approve run", type="primary"):
            if overrides:
                st.error("You changed amounts — click Recompute first so the plan reflects them.")
            elif not ok:
                st.error("Tick the review box.")
            else:
                try:
                    scheduler.approve_run(conn, run["id"], who)
                    st.rerun()
                except ValueError as e:
                    st.error(str(e))
elif run["status"] in ("APPROVED", "EXECUTING"):
    st.info("Approved. The worker (`python -m app.worker`) executes payments exactly once; or run them here.")
    if st.button("▶️ Execute approved payments now (test mode)", type="primary"):
        stats = payments.execute_approved(conn, actor="ui")
        st.success(stats)
        st.rerun()
elif run["status"] == "DONE":
    st.success("Run complete — every instalment executed exactly once. Remaining balances roll to next week's run (RUN_DATE + 7). See Audit Ledger.")
