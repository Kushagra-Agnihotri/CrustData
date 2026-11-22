# Session 3: Bank-change flow + README (branch `bank-change`)

You are working in a git worktree on branch `bank-change` of the Payables Desk repo. Code freeze is 6:00 PM IST today. At 5:15 PM run `git merge main` (keep both sides' test cases if `generate_mock_data.py` or `expected.json` conflict), then push.

1. Setup: `pip install -r requirements.txt` if needed. On Windows set `PYTHONUTF8=1`.
2. Read `CHANGES_TODO.md` Part B and Part D.
3. Do B1, B2, B3 and test cases 19 and 20 (skip 20b if short on time). Add the B4 checklist to `ui/pages/3_Vendor_360.py` (Streamlit) for now. Commit after each.
4. Do C3 (Gemini extraction with parser fallback; the key is in `.env` if present) and C4 (`NOTES.md`). Commit.
5. Draft README Part D. Leave clear placeholders for: demo video link, credits used, cost per invoice, test table. Commit.

Rules:
- Re-run `python scripts/run_test_set.py` after each item.
- Don't touch `app/sources.py`, `app/api.py`, `app/request.py` or `web/`.
- Never print or commit any key from `.env`.
- `git push -u origin bank-change` after each commit.
