# NOTES: where the live APIs differed from our assumptions

A running log, as `BUILD_PROMPT.md` asks. One entry per surprise: what we assumed, what we found, what we changed.
The Crustdata entries come from the Part A work (`app/sources.py`, branch `live-data`). That session adds its own findings here when it merges.

## Gemini (invoice extraction)

| Assumed | Found (22 Nov 2026) | Change |
|---|---|---|
| `gemini-2.5-flash` works with our key | `404: This model models/gemini-2.5-flash is no longer available to new users. Please update your code to use models/gemini-3.8-flash` (the model still shows up in `GET /v1beta/models`) | Default `LLM_MODEL` is now `gemini-3.8-flash` (`app/config.py`, `.env.example`) |
| A Gemini error would surface as an error | Any Gemini error stopped extraction, so with a key set, **every** invoice was held (2/17 tests passed) | `app/extract.py`: Gemini first, deterministic parser as the fallback on any failure (no key, 404, 429/5xx, bad JSON, missing total/lines). `method = "rules (llm fallback)"`, and the LLM error is saved in the `calls` ledger (`error` column) |
| 429 was the only error worth retrying | `503 Service Unavailable` and dropped connections (`WinError 10054`) during the afternoon | 429 and 5xx both back off and retry 3 times, then fall back |
| Gemini handles a layout the parser can't | **Not verified yet.** We tried a hand-written invoice in a new layout (free-text labels, "PLEASE PAY ..."). Both `gemini-3.8-flash` and `gemini-flash-latest` were unreachable at the time (503/timeouts), so the run fell back to the parser, which can't read that layout either: `ValueError: could not find total and line items` → HOLD | TODO: re-run once Gemini responds. That invoice is held, never paid on a guess |

## Penny drop (bank account-holder name)

| Assumed | Found | Change |
|---|---|---|
| Razorpay Account Validation (penny drop) could run in test mode | It isn't available in Razorpay test mode | Mock provider `penny_drop_mock` (`data/penny_drop_mock.json`, keyed by account number), labelled as mock in the UI and README. It's the live step in production |

## Crustdata People Search (second-contact suggestion)

| Assumed | Found | Change |
|---|---|---|
| We can search a company's finance staff by title | `sources.person_search` only looks up a person by name (filter field names still unconfirmed, see Part A2) | `vendor_admin.suggest_second_contact` uses the fixtures in mock mode. In live/replay it calls `sources.finance_contacts(conn, ctx, company_id)` if Part A adds it; otherwise it says "I can't suggest a second contact" and the flow continues (the step is optional) |

## Test harness

| Assumed | Found | Change |
|---|---|---|
| `run_test_set.py` owns `%TEMP%/payables_desk_tests` | Parallel worktrees share that folder, so one session's `golden.db` locks another's (`PermissionError: [WinError 32]`) | Run each worktree with its own `TMPDIR` (e.g. `TMPDIR=$PWD/.tmp python scripts/run_test_set.py`) |
| A vendor always has a trust score | Trust is set to NULL after a bank-master change until it's re-scored, and `scheduler._context_reasons` crashed formatting `None` | Shows "re-scoring" instead |
