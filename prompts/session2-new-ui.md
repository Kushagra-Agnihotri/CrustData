# Session 2: New UI (branch `new-ui`)

You are working in a git worktree on branch `new-ui` of the Payables Desk repo. Code freeze is 6:00 PM IST today. At 5:15 PM run `git merge main` to pick up the live-data work. By 5:30 PM the new UI must run the whole demo, or we fall back to Streamlit.

1. Setup: `pip install -r requirements.txt` if needed (add `fastapi` and `uvicorn` to requirements.txt). On Windows set `PYTHONUTF8=1`.
2. Read `CHANGES_TODO.md` Parts E and F, and `PRODUCT.md`.
3. Build `app/request.py` (E1: request text → job description, keyword fallback, tighten-never-loosen, one question for vague requests) and add `spend_cap` to `scheduler.plan` / `propose_run`. Add tests for cases 21–23. Commit.
4. Build `app/api.py` (F1). It only wraps existing functions, no new business logic. Serve `web/` as static files from the same process. Run it on port 8001. Commit.
5. Use the `/impeccable` skill to build `web/` in this order: Home flow, Payment Run, Invoice Desk (with the PO match panel). Use `PRODUCT.md` as the product context. Commit after each screen. Then Vendor 360, Company Books, Audit Ledger if time allows.

Rules:
- Don't touch `app/sources.py`, `scripts/generate_mock_data.py` or `data/golden/` (another session owns them).
- Don't delete `ui/` (Streamlit is the fallback).
- Bundle assets locally; the demo must work offline.
- `git push -u origin new-ui` after each commit.
