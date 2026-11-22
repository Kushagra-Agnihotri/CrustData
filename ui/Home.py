from common import DECISION_BADGE, config, db, inr, page, q, st, unj  # noqa: I001

from app import pipeline, scheduler
from app.db import q1

page("Home", "📒")
conn = db()

st.title("📒 Payables Desk")
st.markdown("*A payables desk that checks every invoice against live records, then plans each week's payments around the company's cash — "
            "who gets paid, how much, and why.*")
st.info("**The customer's request:** “Here are this week's invoices from our accounts inbox. Match each one to its purchase order, "
        "check the vendor and its bank account, pay each one once — according to the cash we have — and hold anything unusual "
        "for my approval with the reason.”")

c1, c2, c3 = st.columns(3)
if c1.button("♻️ Reset mock database", use_container_width=True, help="Regenerate mock data and reload the vendor master, POs, GRNs and receivables"):
    import scripts.setup_demo as sd
    conn.close()
    sd.reset(run=False)
    st.success("Mock database reset. Now run the pipeline.")
    st.rerun()
if c2.button("▶️ Run pipeline on inbox", type="primary", use_container_width=True):
    with st.spinner("Extracting, validating, resolving, matching, checking…"):
        r = pipeline.run_pipeline(conn, actor="ui")
    st.success(f"Processed {r['processed']} document(s).")
if c3.button("💸 Plan this week's payment run", use_container_width=True):
    try:
        scheduler.propose_run(conn)
        st.success("Payment run proposed — open **Payment Run**.")
    except ValueError as e:
        st.warning(str(e))

invs = q(conn, "SELECT * FROM invoices")
if not invs:
    st.warning("No documents yet. Click **Run pipeline on inbox** (files in `data/inbox/`).")
    st.stop()

st.subheader("Value delivered this week")
dec = {k: [i for i in invs if i["decision"] == k] for k in ("PAY_ELIGIBLE", "HOLD", "BLOCK")}
dups = [i for i in invs if any(r["code"] in ("DUPLICATE_INVOICE", "POSSIBLE_DUPLICATE") for r in unj(i["reasons"], []))]
fraud_vendors = {i["vendor_id"] for i in invs if any(r["code"] == "BANK_CHANGE_FRAUD" for r in unj(i["reasons"], []))}
at_risk = q1(conn, f"SELECT COALESCE(SUM(total),0) s FROM invoices WHERE doc_kind='invoice' AND vendor_id IN ({','.join('?' * len(fraud_vendors)) or 'NULL'})",
             list(fraud_vendors))["s"] if fraud_vendors else 0
disc = q1(conn, "SELECT COALESCE(SUM(discount_amount),0) s FROM instalments WHERE status NOT IN ('CANCELLED','FAILED')")["s"]
msme = q1(conn, "SELECT COALESCE(SUM(i.amount),0) s FROM instalments i WHERE i.tier=0 AND i.status NOT IN ('CANCELLED','FAILED') "
                "AND i.justification LIKE '%MSME_DEADLINE%'")["s"]
cost = q1(conn, "SELECT COALESCE(SUM(est_cost_inr),0) c, COALESCE(SUM(credits),0) cr FROM calls")
n_inv = len([i for i in invs if i["doc_kind"] == "invoice"]) or 1
m = st.columns(3) + st.columns(3)
m[0].metric("Documents", len(invs), f"{len(dec['PAY_ELIGIBLE'])} eligible · {len(dec['HOLD'])} held · {len(dec['BLOCK'])} blocked", delta_color="off")
m[1].metric("Bank-change fraud stopped", f"{len(fraud_vendors)} request", f"{inr(at_risk)} of payables protected", delta_color="off")
m[2].metric("Duplicates caught", inr(sum(i["total"] or 0 for i in dups)), f"{len(dups)} invoice(s)", delta_color="off")
m[3].metric("Discounts captured", inr(disc), "early-payment, planned", delta_color="off")
m[4].metric("MSME dues on time", inr(msme), "43B(h) deduction protected", delta_color="off")
m[5].metric("Cost per invoice", f"₹{cost['c'] / n_inv:.2f}", f"{cost['cr']:.1f} credits total", delta_color="off")

st.subheader("This week's documents")
rows = [{"Case": i["case_id"], "File": i["source_path"].split("/")[-1], "Vendor": i["vendor_name_raw"], "Invoice no": i["invoice_no_raw"],
         "Total": inr(i["total"]), "State": i["state"], "Decision": DECISION_BADGE.get(i["decision"]),
         "Top reason": next((r["message"] for r in unj(i["reasons"], []) if r["severity"] in ("BLOCK", "HOLD")), "all checks passed")[:110]}
        for i in invs]
st.dataframe(rows, hide_index=True, use_container_width=True)
st.caption(f"Mock database: `{config.DB_PATH}` · pages on the left: Invoice Desk → Payment Run → Vendor 360 → Audit Ledger")
