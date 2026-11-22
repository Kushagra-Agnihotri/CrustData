# NOTES: where the live API differed from our assumptions

Checked against docs.crustdata.com (API version `2025-11-01`) on 22 Nov 2026.

| Call | First guess (draft) | Docs say (now in `app/sources.py`) |
|---|---|---|
| Identify | `POST /company/identify` `{domains:[...]}` | Correct. Response `[{matched_on, match_type, matches:[{confidence_score, company_data}]}]`. `company_data` holds only `crustdata_company_id`, `basic_info`, `social_profiles`: **no headcount** (our fixture had it; removed). |
| Enrich | `POST /company/enrich` `{crustdata_company_ids:[id]}`, response = one object with `headcount.timeseries` | Same path, but the response is the **same array shape as identify**. Without `fields` only `basic_info` comes back, so we send `fields: [basic_info, headcount, locations, news]`. There is **no headcount timeseries**: we read `headcount.total` + `headcount.growth_percent.yoy` (a percentage). HQ is `locations.headquarters / state / country`. Enrich also returns dated `news[]` (`article_title`, `article_url`, `article_publish_date`). |
| Person search | `POST /person/search`, filters `current_employer_id`, `name`; response `profiles[].title` | Filters are `{op:"and", conditions:[{field, type, value}]}` with fields `experience.employment_details.current.company_id` and `basic_profile.name` (fuzzy `(.)`). Title is `basic_profile.current_title` (requested via `fields`). |
| Web search | `POST /web/search` `{query, limit}`, results with `date` | Path is **`POST /web/search/live`**, body `{query, location:"IN", sources:["news"]}`. Results are `{source, title, url, snippet, position}`: **no date field**. Dated news comes from enrich `news[]` instead. |
| Credits | hard-coded `1.0` per call | Every data response carries an **`X-Credits-Used`** header; live calls record that. Mock estimates use the documented prices (identify 0.2/match, web search 1/page, person search 0.03/result + 0.1 per premium group filtered/received). |
| Errors | 401/403 retried like any error | 401/403 fail fast (`AuthError`), 402 never retried, 429 waits for `x-ratelimit-reset`. |

Rate limits from the docs: enrich 15/min, person search 30/min, web search 10/min.



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
| The second-contact step needs a People Search by **title** | Supported: `experience.employment_details.current.title` with fuzzy `(.)` (see the person search row above) | `vendor_admin.suggest_second_contact` calls `sources.person_search(..., title="cfo\|finance\|accounts\|...")`. In replay mode the committed recording has titles only, so the UI shows the title without a name |

## Test harness

| Assumed | Found | Change |
|---|---|---|
| `run_test_set.py` owns `%TEMP%/payables_desk_tests` | Parallel worktrees share that folder, so one session's `golden.db` locks another's (`PermissionError: [WinError 32]`) | Run each worktree with its own `TMPDIR` (e.g. `TMPDIR=$PWD/.tmp python scripts/run_test_set.py`) |
| A vendor always has a trust score | Trust is set to NULL after a bank-master change until it's re-scored, and `scheduler._context_reasons` crashed formatting `None` | Shows "re-scoring" instead |
