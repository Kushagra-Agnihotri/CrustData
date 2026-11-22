import os  # noqa: I001
import time

from common import DECISION_BADGE, SEV_ICON, TIER_BADGE, config, db, inr, page, q, st, unj

from app import pipeline, scheduler, summary
from app.db import q1

PACE = float(os.environ.get("PAYDESK_EVAL_PACE", "0.25"))  # seconds per check line, so the walk-through is watchable
STAGE_LABEL = {"extract": "📄 Read document", "validate": "🧮 Validate fields", "resolve": "🏢 Identify vendor",
               "match": "📦 Match PO & goods receipt", "check": "🛡️ Duplicate, bank & sender checks", "decide": "⚖️ Decide"}

page("Home", "📒")
conn = db()

st.title("📒 Payables Desk")
st.markdown("*A payables desk that checks every invoice against live records, then plans each week's payments around the company's cash — "
            "who gets paid, how much, and why.*")
st.info("**The customer's request:** “Here are this week's invoices from our accounts inbox. Match each one to its purchase order, "
        "check the vendor and its bank account, pay each one once — according to the cash we have — and hold anything unusual "
        "for my approval with the reason.”")

ss = st.session_state
ss.setdefault("show_db", False)

c1, c2, c3 = st.columns(3)
if c1.button("♻️ Reset mock database", use_container_width=True, help="Regenerate mock data and reload the vendor master, POs, GRNs and receivables"):
    import scripts.setup_demo as sd
    conn.close()
    sd.reset(run=False)
    ss.pop("eval_log", None)
    st.success("Mock database reset. Now hit **Pay invoices**.")
    st.rerun()
if c2.button(("🙈 Hide database" if ss.show_db else "📂 View database"), use_container_width=True,
             help="The inbox invoices and the records every check runs against"):
    ss.show_db = not ss.show_db
    st.rerun()
pay = c3.button("💸 Pay invoices", type="primary", use_container_width=True,
                help="Evaluate every inbox invoice live, then plan this week's payments around cash (approval still on Payment Run)")


# --------------------------------------------------------------------------- database view
def mask(acct):
    return f"****{acct[-4:]}" if acct else "—"


def render_database():
    seen = {r["source_path"].split("/")[-1]: r for r in q(conn, "SELECT source_path, state, decision FROM invoices")}
    files = sorted(p for p in config.INBOX.iterdir() if p.suffix.lower() in (".txt", ".eml", ".pdf"))
    vendors = {v["id"]: v["legal_name"] for v in q(conn, "SELECT id, legal_name FROM vendors")}
    t = st.tabs([f"📥 Inbox ({len(files)})", "🏢 Vendors", "📝 Purchase orders", "📦 Goods receipts", "🏦 Bank accounts", "💰 Receivables"])
    with t[0]:
        st.caption(f"Invoices waiting in `{config.INBOX.relative_to(config.ROOT) if config.INBOX.is_relative_to(config.ROOT) else config.INBOX}`")
        st.dataframe([{"File": p.name, "Type": p.suffix.lstrip(".").upper(), "Size": f"{p.stat().st_size / 1024:.1f} KB",
                       "Status": (DECISION_BADGE.get(seen[p.name]["decision"]) if p.name in seen else "⏳ not evaluated yet")}
                      for p in files], hide_index=True, use_container_width=True)
        for p in files:
            with st.expander(p.name):
                if p.suffix.lower() == ".pdf":
                    st.caption("PDF — text is extracted when evaluated.")
                else:
                    st.code(p.read_text(encoding="utf-8", errors="replace"), language=None)
    with t[1]:
        st.dataframe([{"Vendor": v["legal_name"], "GSTIN": v["gstin"], "MSME": v["msme_category"], "Trader": bool(v["is_trader"]),
                       "Critical": bool(v["is_critical"]), "Domain": v["domain"], "City": v["city"], "State": v["state"],
                       "Credit days": v["credit_days"], "Discount": f"{v['discount_pct']:g}%/{v['discount_days']}d" if v["discount_pct"] else "—",
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
                       "Bank": r["bank_name"], "Valid from": r["valid_from"], "Status": r["status"], "Proposed by": r["proposed_by"]}
                      for r in q(conn, "SELECT * FROM vendor_bank_accounts ORDER BY vendor_id, status")], hide_index=True, use_container_width=True)
    with t[5]:
        st.dataframe([{"Customer": r["legal_name"], "Invoice": r["invoice_no"], "Amount": inr(r["amount"]), "Due": r["due_date"],
                       "Confidence": f"{r['confidence']:.0%}" if r["confidence"] is not None else "—", "Status": r["status"]}
                      for r in q(conn, "SELECT r.*, c.legal_name FROM receivables r LEFT JOIN customers c ON c.id=r.customer_id ORDER BY r.due_date")],
                     hide_index=True, use_container_width=True)


if pay:
    ss.show_db = False  # make room for the live evaluation
if ss.show_db:
    st.subheader("Database")
    render_database()


# --------------------------------------------------------------------------- live evaluation
def check_line(r) -> str:
    return f"{SEV_ICON.get(r['severity'], '•')} **{r['code']}** — {r['message']}"


def evaluate_live():
    log = []
    head = st.empty()
    bar = st.progress(0.0, text="Reading the inbox…")
    cur, entry = None, None
    for ev in pipeline.iter_pipeline(conn, actor="ui"):
        if ev["type"] == "invoice_start":
            inv = ev["invoice"]
            name = inv["source_path"].split("/")[-1]
            head.markdown(f"#### Evaluating invoice {ev['index']} of {ev['total']}")
            bar.progress((ev["index"] - 1) / max(ev["total"], 1), text=f"{ev['index'] - 1} of {ev['total']} evaluated")
            cur = st.status(f"{inv['case_id'] or ''} · {name} — evaluating…", expanded=True, state="running")
            entry = {"label": f"{inv['case_id'] or ''} · {name}", "stages": []}
        elif ev["type"] == "stage":
            with cur:
                st.markdown(f"**{STAGE_LABEL.get(ev['stage'], ev['stage'])}**")
                lines = [check_line(r) for r in ev["reasons"]] or ["➖ nothing to check at this stage"]
                for ln in lines:
                    st.markdown(ln)
                    time.sleep(PACE)
            entry["stages"].append((ev["stage"], lines))
        elif ev["type"] == "invoice_done":
            inv = ev["invoice"]
            badge = DECISION_BADGE.get(inv["decision"])
            vendor = inv["vendor_name_raw"] or "unknown sender"
            label = f"{entry['label']} · {vendor} · {inr(inv['total']) if inv['total'] else ''} → {badge}"
            with cur:
                st.caption(inv["explanation"] or "")
            cur.update(label=label, state="error" if inv["decision"] == "BLOCK" else "complete", expanded=False)
            entry.update(label=label, explanation=inv["explanation"])
            log.append(entry)
        elif ev["type"] == "run_done":
            bar.progress(1.0, text=f"{ev['processed']} of {ev['processed']} evaluated")
            head.markdown(f"#### Evaluated {ev['processed']} document(s)")
    with st.status("💸 Planning this week's payments around the cash we have…", expanded=False) as s:
        try:
            scheduler.propose_run(conn)
            s.update(label="💸 Payment run planned — awaiting approval on **Payment Run**", state="complete")
        except ValueError as e:
            s.update(label=f"💸 Payment run not re-planned: {e}", state="error")
    return log


if pay:
    run = scheduler.current_run(conn) or q1(conn, "SELECT * FROM payment_runs ORDER BY created_at DESC LIMIT 1")
    known = {r["file_sha256"] for r in q(conn, "SELECT file_sha256 FROM invoices")}
    import hashlib
    new_files = [p for p in config.INBOX.iterdir() if p.suffix.lower() in (".txt", ".eml", ".pdf")
                 and hashlib.sha256(p.read_bytes()).hexdigest() not in known]
    if not new_files and not pipeline.pending_count(conn):
        if run and run["status"] in ("APPROVED", "EXECUTING", "DONE"):
            st.info(f"This week's run is already **{run['status']}** — nothing new to evaluate. Reset the mock database to start over.")
        else:
            st.info("Every inbox invoice has already been evaluated — showing this week's summary. Reset the mock database to replay the evaluation.")
            try:
                scheduler.propose_run(conn)
            except ValueError:
                pass
    else:
        st.subheader("Evaluating this week's invoices")
        ss.eval_log = evaluate_live()
elif ss.get("eval_log"):
    with st.expander(f"🔎 Evaluation log — {len(ss.eval_log)} document(s) checked one by one", expanded=False):
        for e in ss.eval_log:
            st.markdown(f"**{e['label']}**")
            for stage, lines in e["stages"]:
                st.markdown(f"<span class='small'>{STAGE_LABEL.get(stage, stage)}</span>", unsafe_allow_html=True)
                for ln in lines:
                    st.markdown(f"&nbsp;&nbsp;{ln}", unsafe_allow_html=True)
            st.caption(e.get("explanation") or "")
            st.divider()


# --------------------------------------------------------------------------- summary
if not q1(conn, "SELECT 1 x FROM invoices LIMIT 1"):
    st.warning("No invoices evaluated yet. Click **📂 View database** to see what's in the inbox, then **💸 Pay invoices**.")
    st.stop()

s = summary.week_summary(conn)
st.subheader("This week at a glance")
status = s["run_status"]
if status == "PROPOSED":
    st.success(f"**{s['payments']} payment(s) totalling {inr(s['planned'])} are planned** and waiting for approval — nothing has moved yet.")
elif status:
    st.success(f"Payment run is **{status}** — {s['payments']} payment(s), {inr(s['planned'])}.")

def tile(col, label, value, note):
    col.metric(label, value)
    col.caption(note)


m = st.columns(4)
tile(m[0], "Documents evaluated", s["docs"], f"{s['invoices']} invoices · {s['bank_change']} bank-change request(s) · {inr(s['value'])}")
tile(m[1], "Payments planned", inr(s["planned"]), f"{s['payments']} payment(s): {s['full_payments']} full · {s['part_payments']} part"
            + (f" · of {inr(s['spendable'])} spendable" if s["spendable"] is not None else ""))
tile(m[2], "🚨 Fraudulent / suspicious", s["fraud"], f"{inr(s['fraud_protected'])} of payables protected")
tile(m[3], "🟥 High priority (Tier 0)", s["tiers"][0]["count"], f"{inr(s['tiers'][0]['amount'])} — MSME deadline / critical overdue")
m = st.columns(4)
tile(m[0], "🟢 Eligible", s["eligible"], inr(s["eligible_value"]))
tile(m[1], "🟠 Held for review", s["held"], inr(s["held_value"]) + " of invoices")
tile(m[2], "🔴 Blocked", s["blocked"], inr(s["blocked_value"]) + " of invoices")
tile(m[3], "♊ Duplicates caught", s["duplicates"], inr(s["duplicate_value"]))
m = st.columns(4)
tile(m[0], "🟨 Discounts captured (Tier 1)", inr(s["discounts"]), f"{s['tiers'][1]['count']} early payment(s), {inr(s['tiers'][1]['amount'])}")
tile(m[1], "🟦 Tier 2 instalments", s["tiers"][2]["count"], inr(s["tiers"][2]["amount"]))
tile(m[2], "⏸️ Deferred to next run", s["deferred"], "eligible but not paid this week")
tile(m[3], "Cost per invoice", f"₹{s['cost_per_invoice']:.2f}", f"{s['credits']:.1f} credits total")

if s["reasons"]:
    st.markdown("##### Why invoices were stopped")
    top = max(n for _, n in s["reasons"])
    st.dataframe([{"Reason": code.replace("_", " ").capitalize(), "Invoices": n} for code, n in s["reasons"]],
                 hide_index=True, use_container_width=True,
                 column_config={"Invoices": st.column_config.ProgressColumn("Invoices", format="%d", min_value=0, max_value=top)})

st.markdown("##### Every document")
st.dataframe([{"Case": r["invoice"]["case_id"], "Vendor": r["invoice"]["vendor_name_raw"], "Invoice no": r["invoice"]["invoice_no_raw"],
               "Total": inr(r["invoice"]["total"]), "Decision": DECISION_BADGE.get(r["invoice"]["decision"]),
               "Priority": TIER_BADGE.get(r["tier"], "—"), "Planned now": inr(r["planned"]) if r["planned"] else "—",
               "Flags": " ".join(f for f, on in (("🚨 fraud", r["fraud"]), ("♊ duplicate", r["duplicate"])) if on),
               "Outcome": r["outcome"], "Top reason": r["top_reason"][:110]}
              for r in s["rows"]], hide_index=True, use_container_width=True)

if status == "PROPOSED":
    st.page_link("pages/2_Payment_Run.py", label="Review & approve the payment run →", icon="💸")
st.caption(f"Mock database: `{config.DB_PATH}` · pages on the left: Invoice Desk → Payment Run → Vendor 360 → Audit Ledger")
