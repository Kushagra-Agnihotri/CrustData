"""Shared UI helpers."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import streamlit as st  # noqa: E402

from app import config  # noqa: E402
from app.db import connect, init_schema, q, unj  # noqa: E402
from app.money import inr  # noqa: E402

DECISION_BADGE = {"PAY_ELIGIBLE": "🟢 ELIGIBLE", "HOLD": "🟠 HOLD", "BLOCK": "🔴 BLOCK", None: "⚪ —"}
SEV_ICON = {"BLOCK": "🔴", "HOLD": "🟠", "OK": "✅"}
TIER_BADGE = {0: "🟥 Tier 0 · must pay", 1: "🟨 Tier 1 · discount", 2: "🟦 Tier 2 · priority"}


def page(title: str, icon: str = "📒"):
    st.set_page_config(page_title=f"Payables Desk · {title}", page_icon=icon, layout="wide")
    st.markdown("""<style>
      .block-container {padding-top: 2rem;}
      .small {font-size: 0.85rem; color: #6b7280;}
      .fact {font-size: 0.82rem; font-family: ui-monospace, monospace; background: rgba(127,127,127,.08);
             padding: .35rem .5rem; border-radius: .35rem; margin: .15rem 0;}
    </style>""", unsafe_allow_html=True)
    st.sidebar.caption(f"Run date **{config.RUN_DATE}** · Crustdata **{config.CRUSTDATA_MODE}** · IFSC **{config.IFSC_MODE}** · "
                       f"payments **{config.PAYMENT_PROVIDER} (test)**")


def db():
    conn = connect()
    init_schema(conn)
    return conn


def facts_by_ids(conn, ids) -> list[dict]:
    ids = [i for i in (ids or []) if i]
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    return q(conn, f"SELECT * FROM facts WHERE id IN ({marks})", ids)


def ist(ts) -> str:
    """'2026-11-22T10:42:11.066Z' → '22 Nov 16:12 IST'."""
    import datetime as dt
    try:
        d = dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return (d + dt.timedelta(hours=5, minutes=30)).strftime("%d %b %H:%M IST")
    except ValueError:
        return str(ts)[:16]


def render_fact(f: dict):
    src = f["source_type"]
    if src in ("crustdata", "ifsc_api", "crustdata_web"):
        live = f"🌐 live, recorded {ist(f['retrieved_at'])}" if f.get("recorded") else f"🌐 live {ist(f['retrieved_at'])}"
    elif src == "penny_drop_mock":
        live = "🧪 penny drop (mock provider)"
    elif src.endswith("_mock"):
        live = "🧪 mock fixture"
    elif src == "human":
        live = "👤 person"
    else:
        live = "📄 record"
    val = unj(f["value"], f["value"])
    if isinstance(val, (list, dict)):
        import json
        val = json.dumps(val, ensure_ascii=False)[:220]
    st.markdown(f"<div class='fact'><b>{f['key']}</b> = {val}<br>"
                f"{live} · <b>{src}</b> · {f['source_ref'][:140]} · field <code>{f['source_field']}</code> · retrieved {str(f['retrieved_at'])[:19]}</div>",
                unsafe_allow_html=True)


def render_reasons(conn, reasons: list[dict], key_prefix: str):
    order = {"BLOCK": 0, "HOLD": 1, "OK": 2}
    for i, r in enumerate(sorted(reasons, key=lambda r: order.get(r["severity"], 3))):
        facts = facts_by_ids(conn, r.get("fact_ids"))
        label = f"{SEV_ICON.get(r['severity'], '•')} **{r['code']}** — {r['message']}"
        if facts:
            with st.expander(label, expanded=False):
                for f in facts:
                    render_fact(f)
        else:
            st.markdown(label + ("  \n<span class='small'>No record settles this — shown as “can't verify”.</span>"
                                 if r["severity"] != "OK" and "can't verify" in r["message"].lower() else ""), unsafe_allow_html=True)


__all__ = ["st", "page", "db", "q", "unj", "inr", "config", "render_reasons", "render_fact", "facts_by_ids", "ist",
           "DECISION_BADGE", "TIER_BADGE", "SEV_ICON"]
