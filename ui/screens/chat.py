"""Chat screen: add/view the invoice database, type a prompt, watch the evaluation."""
from __future__ import annotations

import hashlib
import os
import time

from common import DECISION_BADGE, SEV_ICON, inr, mode_caption, q, st
from screens.overlays import add_invoices_dialog, database_dialog, inbox_files

from app import intent, pipeline, scheduler
from app.db import q1

PACE = float(os.environ.get("PAYDESK_EVAL_PACE", "0.12"))  # seconds per check line, so the walk-through is watchable
STAGE_LABEL = {"extract": "📄 Read document", "validate": "🧮 Validate fields", "resolve": "🏢 Identify vendor",
               "match": "📦 Match PO & goods receipt", "check": "🛡️ Duplicate, bank & sender checks", "decide": "⚖️ Decide"}
CHECKS = "PO match · vendor identity · bank account · sender · duplicates"


def _say(role: str, content: str, **extra):
    st.session_state.messages.append({"role": role, "content": content, **extra})


def check_line(r) -> str:
    return f"{SEV_ICON.get(r['severity'], '•')} **{r['code']}** — {r['message']}"


def evaluate_live(conn, only):
    log = []
    head = st.empty()
    bar = st.progress(0.0, text="Reading the inbox…")
    cur, entry = None, None
    for ev in pipeline.iter_pipeline(conn, actor="ui", only=only):
        if ev["type"] == "invoice_start":
            inv = ev["invoice"]
            name = inv["source_path"].split("/")[-1]
            head.markdown(f"**Evaluating {ev['index']} of {ev['total']}**")
            bar.progress((ev["index"] - 1) / max(ev["total"], 1), text=f"{ev['index'] - 1} of {ev['total']} evaluated")
            cur = st.status(f"{name} — evaluating…", expanded=True, state="running")
            entry = {"label": name, "stages": []}
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
            label = (f"{entry['label']} · {inv['vendor_name_raw'] or 'unknown sender'} · "
                     f"{inr(inv['total']) if inv['total'] else ''} → {DECISION_BADGE.get(inv['decision'])}")
            cur.update(label=label, state="error" if inv["decision"] == "BLOCK" else "complete", expanded=False)
            entry.update(label=label, explanation=inv["explanation"])
            log.append(entry)
        elif ev["type"] == "run_done":
            bar.progress(1.0, text=f"{ev['processed']} evaluated")
            head.markdown(f"**Evaluated {ev['processed']} document(s)**")
    with st.status("💸 Planning payments around the cash we have…", expanded=False) as s:
        try:
            scheduler.propose_run(conn)
            s.update(label="💸 Payment plan ready", state="complete")
        except ValueError as e:
            s.update(label=f"💸 Payment plan not changed: {e}", state="error")
    return log


def _new_in_scope(conn, req):
    known = {r["file_sha256"] for r in q(conn, "SELECT file_sha256 FROM invoices")}
    return [p for p in inbox_files() if hashlib.sha256(p.read_bytes()).hexdigest() not in known and intent.in_scope(p, req)]


def _echo(req, n: int) -> str:
    what = "check and plan payment for" if req.action == "pay" else "check"
    msg = (f"I'll {what} **{n} document(s)** from **{req.scope_label}**"
           + (f" for **{', '.join(req.vendors)}**" if req.vendors else "") + f".  \nChecks: {CHECKS}. Anything unusual is held for you, with the reason.")
    if req.refused:
        msg += "  \n⚠️ I'll still run every check — they can't be switched off from a prompt."
    if req.action == "pay":
        msg += "  \nNothing moves until you press **Pay now** and confirm."
    return msg


def handle_prompt(conn, prompt: str):
    _say("user", prompt)
    vendors = q(conn, "SELECT legal_name, aliases FROM vendors")
    req = intent.parse(prompt, vendors=vendors)
    if isinstance(req, intent.Rejection):
        _say("assistant", req.message, examples=req.examples)
        return
    files = _new_in_scope(conn, req)
    pending = pipeline.pending_count(conn)
    if not files and not pending:
        evaluated = q1(conn, "SELECT COUNT(*) n FROM invoices")["n"]
        _say("assistant", f"Nothing new to check in **{req.scope_label}**"
             + (f" — all {evaluated} document(s) there are already evaluated." if evaluated else " — the inbox has nothing in that range.")
             + ("" if evaluated else " Add invoices first, or try a wider range.")
             + ("  \n⚠️ Checks can't be switched off from a prompt — every document always gets every check." if req.refused else ""),
             show_results=bool(evaluated))
        return
    _say("assistant", _echo(req, len(files) + pending))
    st.session_state.run_scope = req.scope_label
    st.session_state.run_req = req
    st.session_state.running = True


def render(conn):
    ss = st.session_state
    ss.setdefault("messages", [])
    top = st.columns([6, 1])
    if top[1].button("↺ Reset demo", help="Reload the mock database and empty the chat", use_container_width=True):
        import scripts.setup_demo as sd
        conn.close()
        sd.reset(run=False)
        for k in list(ss.keys()):
            del ss[k]
        st.rerun()

    if not ss.messages:
        st.markdown("<div class='hero'><h1>📒 Payables Desk</h1><p>Tell me which invoices to look at. I match each one to its purchase "
                    "order, check the vendor and its bank account, pay each one once, and hold anything unusual for you — with the reason."
                    "</p></div>", unsafe_allow_html=True)

    n_files = len(inbox_files())
    b = st.columns([1, 2, 2, 1])
    if b[1].button("➕ Add invoices", use_container_width=True):
        add_invoices_dialog(conn)
    if b[2].button(f"🗂 View database · {n_files} in inbox", use_container_width=True):
        database_dialog(conn)
    if ss.get("flash"):
        st.toast(ss.pop("flash"))
    if not n_files:
        st.info("The inbox is empty. Add invoices first.")

    for i, m in enumerate(ss.messages):
        with st.chat_message(m["role"]):
            st.markdown(m["content"])
            for j, ex in enumerate(m.get("examples", [])):
                if st.button(ex, key=f"ex_{i}_{j}"):
                    ss.queued_prompt = ex
                    st.rerun()
            if m.get("show_results") and st.button("View last results →", key=f"vr_{i}"):
                ss.screen = "results"
                st.rerun()

    if ss.get("running"):
        req = ss.run_req
        with st.chat_message("assistant"):
            ss.eval_log = evaluate_live(conn, only=lambda p: intent.in_scope(p, req))
        ss.running = False
        _say("assistant", f"Done — {len(ss.eval_log)} document(s) checked. Opening the results…")
        time.sleep(1.0)
        ss.screen = "results"
        st.rerun()

    prompt = st.chat_input("e.g. Here are this week's invoices — check them and pay each one once")
    prompt = prompt or ss.pop("queued_prompt", None)
    if prompt:
        handle_prompt(conn, prompt)
        st.rerun()
    st.caption(mode_caption())
