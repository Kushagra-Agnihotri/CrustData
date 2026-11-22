import csv
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common import config, db, inr, page, q, st, unj  # noqa: E402

from app.db import q1  # noqa: E402

page("Audit Ledger", "📜")
conn = db()
st.title("📜 Audit Ledger")
st.caption("Append-only: database triggers refuse UPDATE/DELETE on `events` and `facts`.")

run = q1(conn, "SELECT * FROM pipeline_runs ORDER BY started_at DESC LIMIT 1")
tot = q1(conn, "SELECT COUNT(*) n, COALESCE(SUM(credits),0) cr, COALESCE(SUM(est_cost_inr),0) inr_, COALESCE(SUM(input_tokens),0) it, "
               "COALESCE(SUM(output_tokens),0) ot, SUM(ok=0) fails FROM calls")
n_inv = q1(conn, "SELECT COUNT(*) n FROM invoices")["n"] or 1
m = st.columns(5)
m[0].metric("External/model calls", tot["n"], f"{tot['fails'] or 0} failed", delta_color="off")
m[1].metric("Crustdata credits", f"{tot['cr']:.1f}", f"limit {config.RUN_CREDIT_LIMIT:g} per run", delta_color="off")
m[2].metric("LLM tokens", f"{tot['it'] + tot['ot']:,}", f"{tot['it']:,} in / {tot['ot']:,} out", delta_color="off")
m[3].metric("Est. cost", f"₹{tot['inr_']:.2f}", f"₹{tot['inr_'] / n_inv:.2f} per document", delta_color="off")
if run:
    m[4].metric("Last run", run["status"], f"time limit {run['time_limit_s']}s · credits {run['credits_used']:.1f}/{run['credit_limit']:g}", delta_color="off")

tab1, tab2, tab3, tab4 = st.tabs(["Events", "Calls & cost", "Payouts reconciliation", "Export"])
with tab1:
    kinds = [r["entity_type"] for r in q(conn, "SELECT DISTINCT entity_type FROM events")]
    k = st.multiselect("Entity type", kinds, default=kinds)
    ev = q(conn, f"SELECT seq, at, entity_type, entity_id, from_state, to_state, actor, detail FROM events "
                 f"WHERE entity_type IN ({','.join('?' * len(k)) or 'NULL'}) ORDER BY seq DESC LIMIT 500", k)
    st.dataframe(ev, hide_index=True, use_container_width=True)
with tab2:
    st.dataframe(q(conn, "SELECT at, tool, endpoint, mode, credits, input_tokens, output_tokens, est_cost_inr, latency_ms, ok, http_status, error "
                         "FROM calls ORDER BY id DESC LIMIT 500"), hide_index=True, use_container_width=True)
    st.markdown("**Cost per document**")
    st.dataframe(q(conn, "SELECT i.case_id, i.source_path, COUNT(c.id) calls, COALESCE(SUM(c.credits),0) credits, ROUND(COALESCE(SUM(c.est_cost_inr),0),3) est_inr "
                         "FROM invoices i LEFT JOIN calls c ON c.invoice_id=i.id GROUP BY i.id ORDER BY i.rowid"), hide_index=True, use_container_width=True)
with tab3:
    rec = q(conn, "SELECT i.case_id, v.legal_name vendor, i.invoice_no_raw, i.total, i.outstanding, "
                  "(SELECT COALESCE(SUM(amount),0) FROM instalments n WHERE n.invoice_id=i.id AND n.status='PAID') paid, "
                  "(SELECT COUNT(*) FROM instalments n WHERE n.invoice_id=i.id AND n.status='PAID') paid_instalments, "
                  "(SELECT COUNT(*) FROM mock_provider_payouts p JOIN instalments n ON n.idem_key=p.idem_key WHERE n.invoice_id=i.id) provider_payouts "
                  "FROM invoices i JOIN vendors v ON v.id=i.vendor_id WHERE i.decision='PAY_ELIGIBLE' ORDER BY i.rowid")
    st.dataframe([dict(r, total=inr(r["total"]), outstanding=inr(r["outstanding"]), paid=inr(r["paid"]),
                       ok="✅" if r["paid_instalments"] == r["provider_payouts"] else "❌ MISMATCH") for r in rec], hide_index=True, use_container_width=True)
    st.caption("Every PAID instalment must map to exactly one provider payout (same idempotency key).")
with tab4:
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
                    "; ".join(f"{s['source_type']}:{s['source_ref'][:60]}" for s in srcs)])
    st.download_button("⬇️ Export auditor CSV", buf.getvalue(), "auditor_export.csv", "text/csv")
