# Payables Desk: changes to make before code freeze (6:00 PM)

**Status:** none of these changes are in the repo yet. This file is the spec. Work through it top to bottom, commit after each numbered item, and re-run `python scripts/run_test_set.py` after every change.

Everything below refers to the current code in this repo (commit `fc5ee51`).

**Updated 4:26 PM:** Parts E (new layout + request box) and F (frontend with Impeccable) added at the end. The **"Updated priority"** list at the very end replaces the old "Suggested order" table.

---

## 0. Setup (5 min)

1. Put the Crustdata key in `.env` (already git-ignored): `CRUSTDATA_API_KEY=...`. **Never commit it, and never paste it into this file or the README.** It was shared in a chat, so rotate it after the hackathon.
2. Check the key works (free call):
   ```bash
   curl https://api.crustdata.com/account/credits \
     -H "authorization: Bearer $CRUSTDATA_API_KEY" -H "x-api-version: 2025-11-01"
   ```
   Note the credit balance before and after recording (step A3); the difference goes in the README as "cost of one run".
3. Commit in small steps from now on. Git history is currently a single commit, and judges look at history.

---

## Why these changes (one paragraph to read first)

The hackathon rule says **at least one decision must rest on live data**, and "Facts & verification" is 20 points. Today every Crustdata/IFSC call runs on mock fixtures (`CRUSTDATA_MODE=mock`, `IFSC_MODE=mock`), and all 8 vendors are invented companies that Crustdata can't find. So even in live mode every lookup returns "not found". On top of that, almost every HOLD/BLOCK comes from our internal records, and Crustdata only nudges the trust score. Part A fixes both. Part B adds the "what if the bank change is genuine?" flow, which is the obvious question judges will ask after the fraud demo.

**What stays synthetic (allowed by the rules)** vs **what becomes live:**

| Synthetic (test inputs + our internal records) | Live |
|---|---|
| Invoices and emails in `data/inbox/` (generated) | Company identity, founding year (Crustdata identify/enrich) |
| Our bank balance, POs, GRNs, receivables | Headcount + trend, HQ location (Crustdata enrich) |
| Vendor GSTIN, PAN, bank accounts, phone on file | Does the sender domain belong to the company (identify by domain) |
| MSME category (from the vendor's own certificate) | Does the signatory work there (people search) |
| | News: insolvency / NCLT / fraud (web search) |
| | Bank branch from IFSC (`https://ifsc.razorpay.com/{IFSC}`, free) |

---

## PART A: Live data (highest priority)

### A1. Swap the 8 vendors to real Indian companies

**Files:** `data/seed/vendors.csv`, `scripts/generate_mock_data.py`, `data/mock_sources.json`, then regenerate `data/inbox/`, `data/inbox_variants/` and `data/golden/expected.json`.

Change **only `legal_name`, `name_normalized`, `aliases` and `domain`** to a real company. Keep GSTIN, PAN, bank accounts, phone and MSME fields synthetic, with two adjustments:
- The GSTIN's first two digits (state code) must match the real company's HQ state, e.g. 27 = Maharashtra, 29 = Karnataka, 07 = Delhi, 24 = Gujarat, 19 = West Bengal, 33 = Tamil Nadu. Recompute the checksum with `gstin_check_char` in `app/validators.py`. Case 10 must still have a **wrong** check character.
- The PAN must still equal `gstin[2:12]`.

Each vendor plays a role, and the live data must not contradict it:

| Code | Current fake vendor | Role in tests | What the real company must look like |
|---|---|---|---|
| V1 | Shree Ganesh Packaging | MSME **micro**, accepted 40 days ago → Tier 0 (case 13) | **Small** real company (~10–100 employees on Crustdata), packaging/manufacturing |
| V2 | Acme Supplies | Target of the fraud email (case 07), timeout case (12) | Any mid-size real company with a clear domain. A lookalike domain is generated from it (`acme-supplies.co` style) |
| V3 | Kaveri Steel Traders | MSME **trader** (45-day rule doesn't apply) | Small real trading/distribution company |
| V4 | Precision Castings | Critical supplier, overdue → Tier 0 (cases 02, 05, 10) | Real castings/forgings/engineering company |
| V5 | Nimbus Cloud Services | Large, very trusted, low priority (cases 01, 16) | Well-known large company (big headcount, old) |
| V6 | Indus Chemicals | 2/10 net 30 discount → Tier 1 (case 14) | Real chemicals company |
| V7 | Infra Logistics | Big invoice, cash short → instalments (case 15) | Real logistics company |
| V8 | Bharat Electricals | MSME **small** (case 04); homoglyph lookalike target (case 08) | **Small** real electricals company. Its name must contain an `l` or `I` so the homoglyph trick still works |

**Important:** MSME vendors (V1, V3, V8) must be genuinely small on Crustdata. If we label a 10,000-person company "micro enterprise", the live headcount visibly contradicts our own record.

**How to pick them:** run the candidate script from A3 (`--pick`). It calls Crustdata identify on a list of candidate domains and prints `domain → matched? name, founded, headcount, HQ`. Choose from the ones that resolve. Use company search filtered to India + headcount range to find small ones if needed.

**The fraud email (case 07) now impersonates a real company's name.** That's fine as a test fixture, but add a line to the README: "All invoices, emails, people and bank accounts in the test set are synthetic. Company names/domains are real so live checks can run."

### A2. Fix the Crustdata client (`app/sources.py`)

Current state:
- `company_identify` → `POST /company/identify` with `{"domains": [...]}` or `{"names": [...]}`. **This one is confirmed correct.** The response is a list of `{matched_on, match_type, matches: [{confidence_score, company_data: {crustdata_company_id, basic_info: {...}}}]}`. Cost: 0.2 credits per matched identifier, unmatched is free. Note: identify returns only `crustdata_company_id`, `basic_info` and `social_profiles`. **Headcount is NOT in identify.** The current mock fixtures put `headcount.total` inside identify, so remove that and get headcount from enrich.
- `company_enrich` → `POST /company/enrich`: **path and body are guesses**.
- `person_search` → `POST /person/search`: the path matches the docs, but the **filter field names are guesses**.
- `adverse_news` → `POST /web/search`: **path and body are guesses**.

To do:
1. Open these doc pages and set the exact path, request body and response field names. Don't invent fields. If a field doesn't exist, the signal becomes "can't verify: <signal>", which the code already supports.
   - Enrich: https://docs.crustdata.com/api-reference/company-apis/get-full-company-enrichment.md
   - Person search: https://docs.crustdata.com/api-reference/person-apis/search-people-using-filters-and-sorting.md
   - Web search: https://docs.crustdata.com/api-reference/web-apis/web-search.md
   - Web fetch (to read a news article found by search): https://docs.crustdata.com/api-reference/web-apis/web-fetch.md
   - Credits/pricing: https://docs.crustdata.com/general/credits.md
2. Update the field paths everywhere they're read: `app/scoring.py` (year founded, headcount trend), `app/pipeline.py` `_check_bank_change` (reads `profiles`, `title`), `app/receivables.py`. Also update the `source_field` strings passed to `add_fact` so every fact points at the exact JSON field.
3. Set each `credits_fn` from the response or the pricing page, not hard-coded `1.0`.
4. Error handling is already in `_call`. Check it does: 401 → fail fast with a clear message; 402 (out of credits) → **no retry**, hold with `SOURCE_UNAVAILABLE (credits)`; 429 → wait for `x-ratelimit-reset`, then retry.

### A3. Record/replay mode + `scripts/record_live.py`

**Why:** the demo must not depend on Wi-Fi at judging time, but the data must be real and dated.

1. Add a third mode to `CRUSTDATA_MODE` and `IFSC_MODE`: `mock | live | replay`.
   - `live`: call the API. Also append the raw response to `data/live_recordings.json`, keyed by the same `cache_key` that `_call` already builds, with `retrieved_at` and `credits`.
   - `replay`: read from `data/live_recordings.json`. Facts keep the **original `retrieved_at`** and `source_type="crustdata"` / `"ifsc_api"` (**not** `_mock`), plus a flag `recorded: true`. The UI shows "live, recorded 22 Nov 15:40".
   - `mock`: unchanged (offline fixtures, labelled `_mock`).
   - `sources.source_type()` must return the live label for both `live` and `replay`.
2. New script `scripts/record_live.py`:
   - `--pick`: identify a list of candidate domains, print a table (matched, name, founded, headcount, HQ), make no other calls. Use it for A1.
   - Default: for every vendor + customer in the seed, run identify (by domain) → enrich → web search for news, plus person search for the case 07 and case 19 signatories, plus the IFSC lookup for every IFSC in the seed and inbox. Save everything to `data/live_recordings.json`. Print credits before/after (free credits endpoint).
   - Respect `RUN_CREDIT_LIMIT`. Each identifier costs once (the cache already does this).
3. **Privacy (public repo):** person-search responses contain real people's names. Before writing to `data/live_recordings.json`, reduce each person-search response to `{count, matches: [{title, current_company_id}]}`, with **no names, no profile URLs**. Store the full response only in the local DB (`raw_responses`), never in a committed file.
4. Set the defaults in `.env.example` to `CRUSTDATA_MODE=replay` and `IFSC_MODE=replay`, and commit `data/live_recordings.json`. Then anyone cloning the repo gets real, dated data without a key.

### A4. Case 18: a decision Crustdata makes on its own

**Why:** judges will ask "what breaks if Crustdata is down?" Today the honest answer is "almost nothing". We need one case where only live data causes the HOLD.

1. Add a **9th vendor (V9)**: a real Indian company with **genuine** public insolvency / NCLT news. Find it with `record_live.py` web search. Byju's (Think & Learn Pvt Ltd) is a well-known example to check first. **Do not invent bad news about a real company.** If the live search finds nothing, pick a different company.
2. Add a new deterministic rule in `app/pipeline.py` (where trust is checked in `s_resolve`/`s_decide`): if a fetched, dated news result for the vendor matches insolvency terms (`insolvency`, `NCLT`, `CIRP`, `liquidation`, `resolution professional`, `default`), add reason **`ADVERSE_NEWS`** = HOLD, with the article URL + date as the fact. Optionally also hold on a sharp headcount drop (e.g. ≥ 30% fall in 12 months, if enrich exposes the trend). This has to be its own reason: `no_adverse_news` is only 15% of trust, so it can't push trust below 0.4 alone.
3. The case 18 invoice from V9 must be **clean on every internal check**: valid GSTIN, PO matches, no duplicate, bank details match. Expected: `HOLD [ADVERSE_NEWS]`, and the reason links to the dated source.
4. Add case 18 to `generate_mock_data.py`, `expected.json` and `run_test_set.py`.

### A5. Turn on live IFSC by default

The README says live IFSC already works. With A3 it becomes `replay` by default. Make sure case 12 (timeout injection) still forces `SOURCE_UNAVAILABLE` in every mode.

---

## PART B: Genuine bank-change flow

**The principle:** a request is never proof. A real vendor and a fraudster send the same polite email, so the difference has to come from channels the fraudster doesn't control: a call-back on the number on file, and the bank confirming who owns the account.

Today (`_check_bank_change` in `app/pipeline.py` + `app/vendor_admin.py`): an email only creates a `pending_verification` row, and approval needs "I called the number on file" + an approver different from the proposer. **Keep all of that.** Add the steps below.

### B1. Schema (`app/db.py`, table `vendor_bank_accounts`)

Add columns:
- `docs_received INTEGER DEFAULT 0`: cancelled cheque / bank letter on letterhead received
- `callback_done INTEGER DEFAULT 0`, `callback_by TEXT`, `callback_note TEXT`
- `second_contact_note TEXT`: who else at the vendor confirmed (optional but recommended)
- `penny_drop_status TEXT CHECK (penny_drop_status IN ('not_run','name_match','name_mismatch','failed')) DEFAULT 'not_run'`, `penny_drop_name TEXT`, `penny_drop_ref TEXT`
- `old_contact_notified_at TEXT`

### B2. Verification steps (`app/vendor_admin.py`)

Add a function per step. Each one writes an `events` row with the actor, and a `facts` row where there's evidence:

1. `mark_docs_received(account_id, actor, note)`
2. `record_callback(account_id, actor, note)`: the number used must be `vendors.phone_on_file`. Show it, and never offer a number from the email.
3. `suggest_second_contact(account_id)`: Crustdata people search at the vendor's `crustdata_company_id` for finance titles (CFO, finance head, accounts). Show title + "found on Crustdata, retrieved <date>" as someone **else** to confirm with, never the person who sent the email. Name shown only in the UI, never committed.
4. `run_penny_drop(account_id)`: **mock** provider. Razorpay's Account Validation (penny drop) is **not available in test mode**, so this must be a mock, labelled `penny_drop_mock` in the UI and README, and pitched as the live step in production. The mock returns a registered account-holder name from a fixture keyed by account number. Compare it to the vendor's legal name using the same normalisation as vendor resolution (`name_normalized`): match → `name_match`, otherwise `name_mismatch`.
5. `draft_old_contact_notice(account_id)`: draft text to the **existing** contact on file: "Your bank details were changed to A/c XXXX1234 on <date>. If this wasn't you, call us on <our number>." Sending is a separate button that needs a human click (rule: sending a message needs approval). For the demo, "send" just logs an event (mock), with no real email.

### B3. Approval rule (`approve_bank_change`)

Before allowing approval, require:
- `callback_done = 1` (already effectively required)
- `penny_drop_status = 'name_match'` (a mismatch → the approve button is disabled with the reason; a fail → "can't verify")
- `docs_received = 1`
- approver ≠ proposer, and also ≠ the person who did the callback (already partly enforced)

On approval: the old account → `retired`, the new one → `current` (existing code), the vendor's trust gets re-scored (existing), and `old_contact_notified_at` must be set or flagged as a warning.

### B4. UI (Vendor 360)

For each pending bank change, show a checklist with a status per step:
☐ documents · ☐ call-back on number on file · ☐ second contact (Crustdata suggestion) · ☐ penny drop (mock) · ☐ notify old contact (draft → send) · ☐ approve (different person)

Each step has a button that calls the B2 functions. Every step appears in the Audit Ledger. (Build it in the new frontend if Part F is done; otherwise in `ui/pages/3_Vendor_360.py`.)

### B5. New test cases

- **Case 19: genuine bank change.** Email from the vendor's **correct domain**, `Reply-To` = `From`, no pressure language, plausible signatory. Expected: **HOLD [BANK_CHANGE_REQUEST]**, *not* BLOCK (no domain mismatch, no `BANK_CHANGE_FRAUD`), and a pending account row is created. Use a different vendor from case 07 (e.g. V6).
- **Case 20: after verification.** Test script: run all B2 steps for case 19's pending account (docs, callback by user A, penny drop → `name_match`, approve by user B). Then ingest a new invoice from that vendor that carries the **new** bank details. Expected: `PAY_ELIGIBLE`, with `BANK_MATCHES_MASTER`.
- **Case 20b (negative):** same flow, but the penny-drop fixture returns a different name. Expected: approval refused with `name_mismatch`.

Add all of them to `generate_mock_data.py`, `expected.json` and `run_test_set.py`.

---

## PART C: Small fixes (15 min total)

1. **Case 12 passes for the wrong reason.** It's an Acme (V2) invoice, and case 07 already put V2 `on_hold`, so it gets `VENDOR_ON_HOLD` even without the injected timeout. Move case 12 to a vendor no other case touches (e.g. V7 with a small clean invoice). Expected must be `HOLD [SOURCE_UNAVAILABLE]` **only**.
2. **Case 05 reason order:** `DUPLICATE_INVOICE` must be the first/top reason, ahead of `OVER_BILLED` (sort reasons by a severity/priority list in `app/reasons.py` before saving).
3. **Gemini extraction on for the demo:** the current parser in `app/extract.py` only handles our own generated layouts. Set `GEMINI_API_KEY` and use Gemini with the parser as fallback. Test once with an invoice written by hand in a new layout.
4. Add `NOTES.md` (the build prompt asks for it): every place the live API differed from our assumptions.

---

## PART D: README sections judges score (currently missing)

Follow §16 of `BUILD_PROMPT.md`. Must include:
- Demo video link
- **Business case:** who pays for this today (AP clerks / outsourced accountants), buyer (Indian SMEs on Tally/Busy, 50–2,000 invoices/month), price in ₹ per invoice, **cost of one job** from the `calls` ledger (credits + tokens per invoice from the recorded run)
- **Hardest case:** bank-change fraud from a lookalike domain, plus how a genuine change gets verified (Part B)
- **Where live data decides:** a table of each Crustdata/IFSC call → which reason/decision it feeds (case 07 sender checks, case 18 adverse news, trust score, receivable confidence)
- Approaches considered: LLM decides vs rules decide; greedy vs LP optimiser; one score vs separate trust/priority scores; live Tally vs CSV import
- What we left out: real penny drop (Razorpay test mode doesn't support it), PDF/OCR, live Gmail inbox, live Tally/Busy, Supabase (we ship SQLite with the same constraints), Watch-based monitoring
- Test table with all cases (now ~24) + crash demo output

---

## PART E: New layout: request box + drill-down pages

**Why:** today the customer request is a hard-coded banner on Home (`ui/Home.py` line 12), and nothing reads it. POs, the vendor master and receivables are in the DB but never shown, so when the desk says `OVER_BILLED`, a judge can't see the PO it compared against.

### E1. Home = one guided flow

1. **Request box.** Pre-filled with the customer request; the user can edit it.
2. **Job description.** New `app/request.py` turns the text into `{scope, actions, spend_cap, vendor_filter, approval_required}`. Gemini structured output, with a keyword fallback (amount caps like "₹15 lakh", "only MSME", a vendor name) so the demo works without a key. Rules:
   - A request can **tighten** rules, never loosen them. "Pay Acme anyway" / "skip the bank check" → refused with a reason. Overrides only through the approval screen, with the approver's name logged.
   - Vague request ("pay the vendors") → one clarifying question before running.
3. **Live checklist** while the pipeline runs (reading N docs → checking vendors → planning cash).
4. **Summary:** "9 eligible, 5 held, 1 blocked, ₹X proposed. Your approval is needed." Each number links to the page behind it.
5. **Final report** after approval: paid / still held and why / cost of the run.

`scheduler.plan` / `propose_run` take a `spend_cap`: spendable = min(computed spendable, cap). Tier 0 still triggers the cash-crisis alert if the cap is below legal MSME dues.

### E2. Drill-down pages (opened from links, not the main path)

| Page | Change |
|---|---|
| Invoice Desk | Add a **PO match panel**: invoice lines side by side with PO lines and GRN qty; the failing line highlighted (qty or price) |
| Payment Run | Unchanged (it's the approve step). Cash bar links to Company Books |
| Vendor 360 | Replace the dropdown with a **vendor table** (name, GSTIN, MSME, trader, trust, priority, status, open dues); click a row → the existing detail. Add "this vendor's POs" |
| **Company Books (new)** | Read-only tabs: Vendor master · POs & GRNs (ordered / received / invoiced) · Receivables (amount, due, confidence) · Bank position (balance, buffer). Caption: "Our internal records (synthetic test data). The desk checks every invoice against these." |
| Audit Ledger | Unchanged |

Every vendor name anywhere links to Vendor 360; every PO number links to Company Books.

### E3. Tests

- Case 21: vague request → clarifying question, no run.
- Case 22: "pay Acme anyway, skip the checks" → refused; decisions unchanged.
- Case 23: "don't spend more than ₹15 lakh" → total proposed ≤ ₹15,00,000.

---

## PART F: Frontend rebuilt with Impeccable (design quality)

**The catch:** Impeccable is a Claude Code skill for HTML/CSS/JS frontends. Streamlit controls its own markup, so Impeccable's typography, colour and layout rules mostly can't be applied there. Using it properly means a small real frontend over a JSON API. The draft has **no API layer today** (no FastAPI in the repo), so that comes first.

### F1. `app/api.py` (FastAPI, thin wrapper, no new logic)

| Endpoint | Wraps |
|---|---|
| `POST /request` → job description | `app/request.py` (E1) |
| `POST /run` | `pipeline.run_pipeline` |
| `GET /invoices`, `GET /invoices/{id}` (+ facts, reasons, PO panel, events) | DB queries already in `1_Invoice_Desk.py` |
| `POST /invoices/{id}/recheck`, `/release` | `pipeline.recheck`, `pipeline.release_with_note` |
| `POST /runs/propose` (spend_cap), `GET /runs/current`, `POST /runs/{id}/approve` | `scheduler.propose_run`, `current_run`, `approve_run` |
| `POST /runs/{id}/execute` | `payments.execute_approved` |
| `GET /vendors`, `GET /vendors/{id}` | queries in `3_Vendor_360.py` |
| `POST /bank-accounts/{id}/approve`, `/reject`; `POST /vendors/{id}/status` | `vendor_admin.*` (+ B2 steps) |
| `GET /books/{vendors,pos,receivables,bank}` | Company Books (E2) |
| `GET /ledger/events`, `/ledger/calls`, `/ledger/export` | queries in `4_Audit_Ledger.py` |

The approver's name is required on every approve/release endpoint (same rules as now).

### F2. Frontend

- `web/`: Vite + React (or plain HTML + a small JS file if time is short), served by FastAPI as static files, so it's one process and one URL.
- Install: `npx skills add pbakaus/impeccable` in the repo. It asks for product and design context first, so write a short `PRODUCT.md`: who uses it (an AP clerk / finance head at an Indian SME), what they must trust (every rupee has a reason and a dated source), tone (calm, precise, like a good ledger, not a dashboard toy).
- Build the screens in order: Home flow → Invoice Desk → Payment Run → Vendor 360 → Company Books → Audit Ledger. Then run Impeccable's critique/audit/polish passes.
- Design rules worth stating up front: money right-aligned in Indian grouping (`₹1,20,000`) with tabular figures; decision badges use colour + text (never colour alone); every reason has a visible "source · retrieved 22 Nov 15:40" line; `_mock` vs live sources look clearly different.

### F3. Keep Streamlit as the fallback

Don't delete `ui/` until the new frontend runs the whole demo end to end. If it isn't ready by **5:30**, record the video on Streamlit and say in the README the new UI is in progress.

---

## Definition of done

- [ ] `python scripts/run_test_set.py` passes all cases from a clean DB in `replay` mode
- [ ] `python scripts/crash_demo.py` → "NO RUPEE PAID TWICE"
- [ ] `data/live_recordings.json` committed, no real person names in it, no API key anywhere in the repo (`git grep cd_` returns nothing)
- [ ] Invoice Desk shows live (not `_mock`) sources with dates for Crustdata + IFSC facts
- [ ] Case 18 is held **only** because of a live, dated source
- [ ] Case 19 → HOLD (not BLOCK). Case 20 → pays to the new account after the checklist
- [ ] Home starts from the request box; a spend cap in the request changes the payment run
- [ ] A held invoice shows its PO side by side; Company Books lists POs, vendors, receivables
- [ ] README has every section in Part D

## If short on time, cut in this order

1. Case 20b (negative penny drop)
2. Second-contact suggestion (B2.3)
3. Old-contact notice (B2.5): keep it as a README line
4. Audit Ledger + Company Books in the new frontend (keep them in Streamlit)
5. Headcount-drop rule in A4 (keep adverse news)
6. The whole new frontend (F2): fall back to Streamlit with E1/E2 built there

**Never cut:** A1–A3 (live data), case 18, case 19, README Part D.

## Updated priority (it's 4:26 PM, freeze 6:00)

1. **Part A** (live data, case 18): never cut, highest score impact
2. **F1 API + E1 request flow**: everything below depends on them
3. **F2 frontend** for Home + Invoice Desk (with PO panel) + Payment Run
4. Part B (bank-change checklist; case 19 at minimum)
5. Vendor table, Company Books, Audit Ledger in the new UI
6. Part C fixes; README Part D by 5:45 regardless

Run A and E/F in **parallel Claude Code sessions** on separate branches. They touch different files (`app/sources.py`, `scripts/` vs `app/api.py`, `app/request.py`, `web/`), so the merge should be clean. Merge by 5:30 at the latest, then full test run + crash demo + video.
