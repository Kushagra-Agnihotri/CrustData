# Session 1: Live data (branch `live-data`)

You are working in a git worktree on branch `live-data` of the Payables Desk repo. Code freeze is 6:00 PM IST today; merge this branch into `main` by 5:15 PM even if A4 is unfinished.

1. Setup: `pip install -r requirements.txt` if needed. On Windows set `PYTHONUTF8=1` before running scripts.
2. Read `CHANGES_TODO.md` fully.
3. Do these items in this order, committing after each: **C1, C2, then A2, A3, A1, A4, A5**.

Rules:
- The Crustdata key is in `.env`. Never print it, never commit it, never paste it into any file.
- Check every endpoint against the Crustdata docs linked in A2. Don't invent field names.
- Run `python scripts/record_live.py --pick` before choosing vendors in A1, then `python scripts/record_live.py` to record live responses.
- Re-run `python scripts/run_test_set.py` after every item and fix failures before moving on.
- Stop and tell me if a live call fails twice.
- Don't touch `app/api.py`, `app/request.py` or `web/` (another session owns them).
- When done (or at 5:15 PM): `git push -u origin live-data`, then tell me so I can merge it.
