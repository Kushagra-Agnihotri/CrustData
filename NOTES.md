# NOTES — where the live APIs differed from our assumptions

Recorded 22 Nov 2026 against Crustdata API version `2025-11-01` and `https://ifsc.razorpay.com`.
Everything below was observed in a real response (kept in `data/live_recordings.json`) or read in docs.crustdata.com.

## Crustdata

| What we assumed | What the API actually does | Change made |
|---|---|---|
| `identify` returns headcount (our old fixtures had `headcount.total` inside identify) | Identify returns only `crustdata_company_id`, `basic_info`, `social_profiles`. On our plan `headcount` and `locations` are not available from identify at all | Headcount, growth and HQ come from **enrich** (`app/sources.py: company_enrich`) |
| Enrich: `POST /company/enrich {"crustdata_company_ids": [...]}` returns the full profile | Path and id field were right, but without `fields` only `crustdata_company_id` + `basic_info` come back. Response is a **list** `[{matched_on, match_type, matches:[{confidence_score, company_data}]}]`, same shape as identify | We send an explicit `fields` list and read `[0].matches[0].company_data` |
| Headcount series is `headcount.timeseries[].value`, 12 monthly points | Points are `{date, employee_count}`, weekly-ish, 150–360 points per company. A ready-made `headcount.growth_percent.yoy` exists | Trend = `growth_percent.yoy`; timeseries only as fallback (`app/scoring.py: _yoy`) |
| Enrich costs 1 credit | **2 credits per returned match**. A domain or name can fuzzy-match several companies (byjus.com → BYJU'S and "BYJU'S Support"; futureretail.in → 2 companies), so we identify first (0.2/match) and enrich **by id** | `credits_fn` = 2 × matches; prefer the `x-credits-used` header when present |
| Person search filters: `{"field": "current_employer_id"}`, `{"field": "name"}` | Filters are a tree `{op: "and"/"or", conditions: [{field, type, value}]}`. Fields: `experience.employment_details.current.company_id`, `basic_profile.name`. Results under `profiles[]`; title at `basic_profile.current_title` | `sources.person_search`, `sources.finance_contacts`. The API also returns `remarks` explaining which condition removed every row — useful for "can't verify" messages |
| Person search costs 1 credit per call | 0.03/result base + 0.1 per premium group delivered (experience). 0 results = 0 credits. Our finance-contact search cost 0.23 for 1 result | `credits_fn` = 0.23 × profiles |
| News: `POST /web/search {"query", "limit"}` | Path is **`/web/search/live`**; body `{query, sources: ["news"], location?, start_date?, end_date?, page?}`. No `limit`. Results have `title, url, snippet, position, source` — **no publish date** | Publish date comes from **web fetch** (`POST /web/enrich/live {urls}`), read from page metadata or page text ("Published On Nov 03, 2026 at 11:34 PM IST") |
| Web search costs 1 credit | 1 credit per page — but **0 credits when nothing is found** (Chander Prabhu Packers, iLuxer) | `credits_fn` = 1 if results else 0 |
| `location: "IN"` narrows news to India | With `location: "IN"` + `sources: ["news"]` the Byju's query returned **0 results**; without it, 10 | We don't send `location` |
| A news hit mentioning the company + "NCLT" is adverse | **False positives in real data**: "NCLT gives nod to Tata Communications land demerger", "NCLT clears merger of Suzuki Motor Gujarat with Maruti Suzuki", Thermax/Kirloskar mergers. And "Thermax defaults on dollar loans" is a *different* Thermax Group in Bangladesh. "Resolution Professional Pankaj…" is a person, not Pankaj Steels | Adverse = company name in title/snippet **and** an insolvency term (`insolvency`, `CIRP`, `liquidation`, `resolution professional`, `winding up`). "NCLT" alone and bare "default" don't count (`app/scoring.py: ADVERSE_TERMS`). Customers still show the Thermax collision risk in principle; it didn't trigger with these terms |
| Google News URLs are article URLs | Most news results are `https://www.google.com/goto?url=…` redirects with long session-bound tokens; web fetch can't read them | We fetch the first **direct** article URL among the hits for the date; recordings shorten redirect tokens |
| 429 → exponential backoff | Rate limits are per endpoint per minute (`/web/search/live` and `/web/enrich/live`: 10 RPM; enrich hit 429 when we sent 13 in parallel). The 429 carries `x-ratelimit-reset` (seconds) and no `Retry-After` | `_call` waits `x-ratelimit-reset` (capped 30 s) before retrying. 401 fails fast with a clear message; 402 is not retried → `SOURCE_UNAVAILABLE (out of credits)` |
| Account balance needs a paid call | `GET /account/credits` is free | `record_live.py` prints the balance before and after |

Cost of the recording: balance 199.8 → 146.47 credits. 41.03 credits are the responses we kept; the rest was
candidate search for small vendors (company search, ~1 credit), probing, two retries after 429/timeouts, and one
fetch that 404'd because we had shortened its URL.

## IFSC (Razorpay)

| What we assumed | What we saw | Change made |
|---|---|---|
| `SBIN0000813` (used in the old fixtures as "SBI Bangalore") is a real branch | **404 Not Found** live. The old mock fixture was wrong | Fixtures now carry only codes verified live; Karnataka vendors use HDFC0000053 / HDFC0000075 / SBIN0021745 |
| Branch state matches today's state names | Hyderabad branches (HDFC, SBI, Axis) are listed under **ANDHRA PRADESH** — the dataset predates Telangana (2014) | `STATE_EQUIV = {"TELANGANA": {"ANDHRA PRADESH"}}` in `app/pipeline.py`; the IFSC_OK message says so |

## How the recording was made

There was no `CRUSTDATA_API_KEY` in `.env` on the recording machine, so the 22 Nov recording was made through the
Crustdata MCP server, which calls the same REST endpoints with the account's key. Each entry keeps its request
body, status, retrieval time and credits. Trimmed fields are listed per entry (`trimmed`): HTML page bodies,
long descriptions, Google redirect tokens, timeseries kept to first+last point of the window, news kept to the
first 5 results. People-search entries are reduced to `{count, matches:[{title, current_company_id}]}` — no names.

`scripts/record_live.py` (with a key in `.env`) re-records the same cache keys by running the pipeline in live
mode, so a fresh recording drops straight into replay.

## Not verified live

- Penny drop: Razorpay Account Validation is not available in test mode → `penny_drop_mock` (fixture in `data/penny_drop_mock.json`).
- GSTIN registration status: the GST portal is captcha-gated; we only check format + checksum.
- Gemini extraction: wired with the parser as fallback on any error; not exercised in the recorded run (no key).
