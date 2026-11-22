import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common import DECISION_BADGE, db, inr, page, q, render_reasons, st, unj  # noqa: E402

from app import pipeline  # noqa: E402
from app.db import q1  # noqa: E402

page("Invoice Desk", "🧾")
conn = db()
st.title("🧾 Invoice Desk")

invs = q(conn, "SELECT i.*, v.legal_name vname FROM invoices i LEFT JOIN vendors v ON v.id=i.vendor_id ORDER BY i.rowid")
if not invs:
    st.warning("No documents yet — run the pipeline from Home.")
    st.stop()

f = st.radio("Show", ["All", "🟢 Eligible", "🟠 Held", "🔴 Blocked"], horizontal=True)
flt = {"🟢 Eligible": "PAY_ELIGIBLE", "🟠 Held": "HOLD", "🔴 Blocked": "BLOCK"}.get(f)
shown = [i for i in invs if not flt or i["decision"] == flt]
rows = [{"Case": i["case_id"], "File": i["source_path"].split("/")[-1], "Vendor (resolved)": i["vname"] or f"✗ {i['vendor_name_raw']}",
         "Invoice no": i["invoice_no_raw"] or ("bank-change request" if i["doc_kind"] == "bank_change" else "—"),
         "Total": inr(i["total"]), "State": i["state"], "Decision": DECISION_BADGE.get(i["decision"]),
         "Top reason": next((r["code"] for r in unj(i["reasons"], []) if r["severity"] in ("BLOCK", "HOLD")), "—")} for i in shown]
sel = st.dataframe(rows, hide_index=True, use_container_width=True, on_select="rerun", selection_mode="single-row", key="inv_table")
idx = sel.selection.rows[0] if sel and sel.selection.rows else 0
inv = shown[idx] if shown else None
if not inv:
    st.stop()

st.divider()
st.subheader(f"{DECISION_BADGE.get(inv['decision'])}  {inv['source_path'].split('/')[-1]}")
if inv["explanation"]:
    st.markdown(f"> {inv['explanation']}")
left, right = st.columns([3, 2])
with left:
    st.markdown("#### Reasons & evidence")
    render_reasons(conn, unj(inv["reasons"], []), inv["id"])
with right:
    st.markdown("#### Extracted fields")
    ex = unj(inv["extracted"], {}) or {}
    st.caption(f"Extraction method: {ex.get('_method', '—')} · sha256 {inv['file_sha256'][:16]}…")
    fields = {"Vendor (as written)": inv["vendor_name_raw"], "GSTIN": inv["gstin_raw"], "Invoice no": f"{inv['invoice_no_raw']} → {inv['invoice_no_norm']}" if inv["invoice_no_raw"] else None,
              "Invoice date": inv["invoice_date"], "PO": inv["po_number_raw"], "Sub-total": inr(inv["subtotal"]), "Tax": inr(inv["tax"]),
              "Total": inr(inv["total"]), "Bank a/c": inv["bank_account_raw"], "IFSC": inv["ifsc_raw"], "Beneficiary": inv["beneficiary_raw"],
              "Sender": inv["sender_email"], "Reply-To": inv["reply_to"], "Signatory": f"{inv['signatory']}, {inv['signatory_title']}" if inv["signatory"] else None,
              "Accepted (MSME clock)": inv["accepted_date"], "Due": inv["due_date"], "MSME deadline": inv["msme_deadline"],
              "Outstanding": inr(inv["outstanding"]) if inv["outstanding"] is not None else None}
    st.table({k: str(v) for k, v in fields.items() if v not in (None, "None", "—")})
    lines = q(conn, "SELECT line_no, item, qty, unit_price, gst_rate, amount FROM invoice_lines WHERE invoice_id=?", (inv["id"],))
    if lines:
        st.dataframe([{"#": l["line_no"], "Item": l["item"], "Qty": l["qty"], "Rate": inr(l["unit_price"], True), "GST %": l["gst_rate"],
                       "Amount": inr(l["amount"], True)} for l in lines], hide_index=True, use_container_width=True)
    with st.expander("Original document"):
        from app import config
        p = config.ROOT / inv["source_path"]
        st.code(p.read_text(encoding="utf-8", errors="replace")[:4000] if p.exists() else "file missing", language="text")

st.markdown("#### Actions")
a1, a2 = st.columns(2)
with a1:
    if st.button("🔁 Re-check (re-run verification)", disabled=inv["state"] not in ("HELD", "BLOCKED")):
        pipeline.recheck(conn, inv["id"], actor="ui")
        ctx = pipeline.start_run(conn)
        pipeline.process_one(conn, inv["id"], ctx, actor="ui")
        st.rerun()
with a2:
    if inv["state"] == "HELD":
        with st.form("release"):
            who = st.text_input("Your name (approver)")
            note = st.text_input("Why is it OK to pay? (logged)")
            if st.form_submit_button("✅ Release with note"):
                err = pipeline.release_with_note(conn, inv["id"], who, note)
                st.error(err) if err else st.rerun()
    elif inv["state"] == "BLOCKED":
        st.caption("Blocked documents can't be released here — fix the root cause (e.g. vendor master) and re-check.")

st.markdown("#### State history")
ev = q(conn, "SELECT seq, at, from_state, to_state, actor, detail FROM events WHERE entity_id=? ORDER BY seq", (inv["id"],))
st.dataframe(ev, hide_index=True, use_container_width=True)
