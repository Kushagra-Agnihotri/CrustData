"""Payables Desk — one guided flow, no sidebar.

chat ──prompt──► live evaluation ──► results ──Pay now──► paywall (mock) ──► receipt
Overlays (st.dialog): invoice database, add invoices, explore each invoice (with the bank-change tab), audit trail.
"""
from common import db, page, st
from screens import chat, paywall, results

page()
conn = db()
ss = st.session_state
ss.setdefault("screen", "chat")

SCREENS = {"chat": chat.render, "results": results.render, "paywall": paywall.render, "receipt": paywall.render_receipt}
SCREENS.get(ss.screen, chat.render)(conn)
