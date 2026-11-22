# Payables Desk — prototype

> A payables desk that checks every invoice against live records, then plans each week's payments around the company's cash — who gets paid, how much, and why.

ContextCon 05 · Theme 6.1 (Buying and Paying) · team Aditya, Kushagra, Rajat. Full design: [BUILD_PROMPT.md](BUILD_PROMPT.md). API findings: [NOTES.md](NOTES.md).

**Demo video:** [demo/payables-desk-demo.mp4](demo/payables-desk-demo.mp4) (~5 min walkthrough of every screen; re-record with
`node demo/record-demo.js`, which needs Playwright and ffmpeg).

**Docs:** [ARCHITECTURE.md](ARCHITECTURE.md) (technical reference with workflow diagrams) ·
[docs/system-architecture.html](docs/system-architecture.html) and [docs/architecture.html](docs/architecture.html) (visual
architecture) · [PRODUCT.md](PRODUCT.md) · [UI_REDESIGN.md](UI_REDESIGN.md) · [CHANGES_TODO.md](CHANGES_TODO.md).

## What changed in this version

- **Chat-first UI.** The four sidebar pages (Invoice Desk, Payment Run, Vendor 360, Audit Ledger) are replaced by one guided
  flow in `ui/screens/`: chat → live evaluation → results → per-invoice overlay → pay now (mock paywall) → receipt.
- **Intent parser** (`app/intent.py`, tested in `tests/test_intent.py`): reads the scope from the prompt, refuses off-topic
  prompts, and ignores any attempt to switch a check off.
- **Live bank-change verification** (`app/bank_verify.py`): paste an email/SMS in the overlay's Bank details tab for a
  🟢/🟡/🔴 verdict, plus the call-back checklist with a second approver.
- **Real vendors and live data**: test-set vendors are real Indian companies; Crustdata and Razorpay IFSC responses are
  recorded (`data/live_recordings.json`, `scripts/record_live.py`) and replayed with their original dates.
- **New checks**: `ADVERSE_NEWS` from live news (case 18), genuine bank change → checklist → payment to the new account
  (cases 19, 20, 20b), weekly summary (`app/summary.py`).
- **Demo assets**: a recorded walkthrough video and a Playwright recorder script in `demo/`.

> **All invoices, emails, people and bank accounts in the test set are synthetic. Company names/domains are real so live checks can run.**
> GSTINs, PANs, phone numbers, MSME categories and our POs/GRNs/receivables are synthetic too. The case 07 fraud email impersonates a real company's name as a test fixture only.

## Run it (Windows / macOS / Linux)

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt        # macOS/Linux: .venv/bin/pip
.venv/Scripts/python scripts/setup_demo.py            # builds the DB (add --run to also evaluate the inbox up front)
.venv/Scripts/streamlit run ui/Home.py                # UI at http://localhost:8501
```

**Using the app.** One guided flow, no sidebar:
1. **Chat.** `🗂 View database` shows the inbox and the records the checks run against; `➕ Add invoices` drops files into the inbox.
   Type what you want, e.g. *"Here are this week's invoices from our accounts inbox. Match each one to its purchase order,
   check the vendor and its bank account, pay each one once, and hold anything unusual for my approval with the reason."*
   The prompt picks the scope ("this week's inbox", "the past two months", "October", "last 30 days", a vendor name).
   Off-topic prompts are refused, and a prompt can never switch a check off ("skip the bank check" is noted and ignored).
2. **Live evaluation** streams in the chat, then the app moves to **Results**: what's ready to pay, what's held or
   blocked and why, duplicates caught.
3. **Explore each invoice** opens an overlay with the reasons and evidence, the vendor's trust score, and a **Bank details**
   tab: paste a bank-change email and/or SMS and it is checked live (sender domain and lookalikes, the requester on
   Crustdata people search, IFSC branch vs the vendor's state, beneficiary name, email/SMS agreement, pressure language).
   The verdict is 🟢 looks genuine / 🟡 couldn't verify / 🔴 fraud indicators — and in every case the call-back checklist
   still has to be completed and approved by a second person.
4. **Pay now** → a mock paywall (real approval + payout code path against the mock bank) → a receipt with bank refs.

```bash
.venv/Scripts/python scripts/run_test_set.py    # golden test set, pass/fail table
.venv/Scripts/python scripts/crash_demo.py      # kill mid-payment, restart, reconcile
.venv/Scripts/python -m app.worker              # background worker (pipeline + approved payments)
.venv/Scripts/python scripts/record_live.py     # re-record live data (needs CRUSTDATA_API_KEY in .env)
```

On Windows set `PYTHONUTF8=1` if the console shows encoding errors.

**Live data without a key.** `CRUSTDATA_MODE` / `IFSC_MODE` = `replay` (default) serves `data/live_recordings.json`:
real Crustdata and Razorpay IFSC responses recorded on 22 Nov 2026, with their original retrieval times. The UI labels
them "🌐 live, recorded 22 Nov 16:12 IST". `live` calls the APIs (and appends to the recordings); `mock` uses offline
synthetic fixtures, labelled `_mock` so a fixture is never shown as live data.

## Hardest case: a polite bank-change email — and a genuine one

**The fraud (case 07).** "Rahul Mehta, Accounts Manager, Astral Limited" writes from `astral-pipes.co` (real domain:
`astralpipes.com`), Reply-To a Gmail address, asks to send all pending payments to a new SBI account in Jharkhand,
"urgently", "please do not call our office line". Result: **BLOCK** — `BANK_CHANGE_FRAUD`, `SENDER_DOMAIN_MISMATCH`
(lookalike; Crustdata identify finds no company at that domain), `REPLY_TO_MISMATCH`, `SENDER_NOT_VERIFIED_AT_VENDOR`
(Crustdata people search: nobody by that name at Astral), `PRESSURE_LANGUAGE`, `IFSC_LOCATION_MISMATCH`. The vendor is put on hold.
An email can never change the vendor master; it only creates a *pending* account.

**The genuine change (cases 19 → 20).** Gujarat Alkalies and Chemicals writes from `gacl.com`, Reply-To = From, no
pressure, documents attached. Result: **HOLD** `BANK_CHANGE_REQUEST`, not BLOCK. A request is never proof — a real vendor
and a fraudster send the same email — so the change is verified through channels the requester doesn't control
(the checklist in the invoice overlay's Bank details tab, every step in the audit trail):

1. documents received (cancelled cheque / bank letter)
2. **call-back on the number already on file** (the UI only ever shows that number)
3. second contact at the vendor, suggested from Crustdata people search (finance/accounts titles; never the sender)
4. **penny drop**: the bank's registered name must match the vendor's legal name (`penny_drop_mock` — Razorpay Account
   Validation isn't available in test mode; this is the live step in production)
5. the *old* contact on file is told about the change (drafted; sending needs a human click; mock send)
6. approval by someone other than the proposer **and** the caller

Case 20: after the checklist, a new GACL invoice with the new details is `PAY_ELIGIBLE` with `BANK_MATCHES_MASTER`.
Case 20b: penny drop says the account belongs to "R K TRADING COMPANY" → approval refused (`name_mismatch`).

## Where live data decides

| Live call | Feeds | Where it changes the outcome |
|---|---|---|
| Crustdata **identify** by vendor domain (`POST /company/identify`) | trust `identity` (25%), `crustdata_company_id` | Every vendor; a vendor with no Crustdata match loses 0.125–0.25 trust |
| Crustdata **identify** by sender domain | `SENDER_DOMAIN_MISMATCH` message | Case 07: `astral-pipes.co` → "no company in Crustdata" |
| Crustdata **identify** by name | registry lookup for unknown vendors | Case 11 (Sharma Traders): name match shown, but not accepted as proof |
| Crustdata **enrich** (`POST /company/enrich`) | trust `company_age`, `headcount_stability` (`growth_percent.yoy`), `domain_consistency`; HQ | Trust gate (< 0.4 → HOLD); receivable confidence |
| Crustdata **people search** (`POST /person/search`) | `SENDER_NOT_VERIFIED_AT_VENDOR`; second-contact suggestion | Case 07, case 19, bank-change checklist step 3 |
| Crustdata **web search** (news) + **web fetch** (publish date) | **`ADVERSE_NEWS`** (HOLD on its own); trust `no_adverse_news`; customer health | **Case 18**: Think and Learn (BYJU'S) — clean on every internal check, held only because of a dated ET article (published 3 Nov 2026) about its insolvency. Future Retail's liquidation news keeps its ₹4L receivable out of expected cash |
| Razorpay **IFSC** | `IFSC_OK`, `IFSC_NOT_FOUND`, `IFSC_LOCATION_MISMATCH` | Case 07 (Jharkhand branch for a Gujarat company), case 08 |

If Crustdata is down: a timeout is never read as "no problem" — the invoice gets `SOURCE_UNAVAILABLE` (case 12), and a
trust score computed during an outage is recomputed on the next use.

## Business case

- **Who pays for this today:** AP clerks at Indian SMEs and the outsourced accountants (CA firms) who run their payables —
  checking each invoice against the PO and GRN by hand, keeping vendor bank details in Excel, and deciding each Friday
  who gets paid. Bank-change fraud ("business email compromise") is the expensive failure.
- **Buyer:** finance head / owner of an Indian SME on Tally or Busy with 50–2,000 purchase invoices a month.
- **Price (our proposal):** ₹15 per invoice processed, minimum ₹2,000/month — under the cost of an AP clerk's time per invoice.
- **Cost of one job** (from the `calls` ledger of the recorded run, 17 documents, 9 vendors, 4 customers):
  40.8 Crustdata credits in total — identify 0.2 + enrich 2 + news search 0–1 (+ 1 for an article fetch when news is
  adverse) **per company, once per run** (cached), people search 0–0.23 per bank-change email. That is ≈ 3.2 credits per
  new vendor and ~0 for repeat vendors within a run, i.e. 2.4 credits per document in this test set. IFSC lookups are free.
  LLM tokens: 0 in the recorded run (deterministic parser); with Gemini on, extraction adds roughly ₹0.05–0.2 per invoice
  at the rates in `.env.example`. Set `CRUSTDATA_INR_PER_CREDIT` to turn credits into ₹ in the UI.

## Approaches considered

- **LLM decides vs rules decide.** Rules decide. The LLM (optional) only extracts fields; every HOLD/BLOCK comes from a
  reason code with evidence (`app/reasons.py`), so the same facts always give the same decision (case 16 re-worded invoice).
- **Greedy vs LP optimiser** for the payment run. Greedy by tier (legal deadlines → discounts → priority), with minimum
  instalments. Explainable line by line; an LP would be marginally better on paper and impossible to justify to a vendor.
- **One score vs two.** Separate *trust* (is it safe to pay at all — a gate) and *priority* (who first — an order). One blended
  score lets a loyal vendor's history paper over a fraud signal.
- **Live Tally vs CSV import.** CSV + a Tally-shaped voucher export behind an `AccountingSource` interface; live TallyPrime
  XML is the same interface.
- **Live-only vs record/replay.** Record/replay: the demo can't depend on Wi-Fi, but the data must be real and dated.

## What we left out

- Real penny drop (Razorpay Account Validation is not available in test mode) — mocked, labelled `penny_drop_mock`
- PDF / OCR ingestion (text and .eml only)
- Live Gmail inbox (folder of .eml/.txt files)
- Live Tally / Busy connection (CSV export import)
- Supabase/Postgres — we ship SQLite with the same constraints (maker-checker CHECK, append-only triggers, unique invoice identity)
- Crustdata Watch-based monitoring of vendors between payment runs
- GSTIN registration status (GST portal is captcha-gated) — checksum only, and the UI says so

## Test set (from a clean DB, `replay` mode)

```
case    scenario                                                        result  detail
case01  Clean invoice, PO matches (large vendor, low priority)          ✅ PASS  PAY_ELIGIBLE T2
case02  Clean invoice via email; critical supplier overdue              ✅ PASS  PAY_ELIGIBLE T0
case03  Clean invoice; MSME trader (45-day rule n/a)                    ✅ PASS  PAY_ELIGIBLE T2
case04  Clean invoice; MSME small, deadline not near                    ✅ PASS  PAY_ELIGIBLE T2
case05  Re-issue of #2 as INV/42 vs INV-0042                            ✅ PASS  HOLD [DUPLICATE_INVOICE, OVER_BILLED]
case06  Same vendor + amount, new number, 3 days later                  ✅ PASS  HOLD [POSSIBLE_DUPLICATE]
case07  Polite bank-change email from lookalike domain                  ✅ PASS  BLOCK [BANK_CHANGE_FRAUD, BANK_CHANGE_REQUEST, SENDER_DOMAIN_MISMATCH]
case08  Homoglyph lookalike vendor, different GSTIN                     ✅ PASS  HOLD [VENDOR_NOT_APPROVED]
case09  Billed qty 120 > PO qty 100                                     ✅ PASS  HOLD [OVER_BILLED]
case10  GSTIN fails checksum                                            ✅ PASS  BLOCK [INVALID_GSTIN, GSTIN_MISMATCH]
case11  Sharma Traders: no GSTIN, no PO → I can't verify                ✅ PASS  HOLD [UNVERIFIABLE_VENDOR]
case12  Crustdata + IFSC timeout injected (untouched vendor)            ✅ PASS  HOLD [SOURCE_UNAVAILABLE]
case13  MSME micro, accepted 40 days ago → Tier 0                       ✅ PASS  PAY_ELIGIBLE T0
case14  2/10 net 30 within discount window → Tier 1                     ✅ PASS  PAY_ELIGIBLE T1
case15  Large invoice, cash short → instalments                         ✅ PASS  PAY_ELIGIBLE T2 ₹872,238 of ₹1,770,000
case16  Case 1 re-worded + re-laid-out                                  ✅ PASS  PAY_ELIGIBLE (case01: PAY_ELIGIBLE), 10 checks
case17  kill -9 mid-payment, restart → each instalment paid once        ✅ PASS  crash exit 137; 7 payouts for 7 instalments; 1 recovered via idem key
case18  Clean invoice; vendor in insolvency per live news               ✅ PASS  HOLD [ADVERSE_NEWS]
case19  Genuine bank-change email (vendor's own domain)                 ✅ PASS  HOLD [BANK_CHANGE_REQUEST, SENDER_NOT_VERIFIED_AT_VENDOR]
case20  After call-back + penny drop + 2nd approver → pays new account  ✅ PASS  PAY_ELIGIBLE [BANK_MATCHES_MASTER, new A/c ****5678]
case20b Penny drop returns another name → approval refused              ✅ PASS  refused: name_mismatch (bank says 'R K TRADING COMPANY')

21/21 passed
```

Crash demo (`scripts/crash_demo.py`): `provider payouts 7 == approved instalments 7; recovered-after-crash 1` → **✅ NO RUPEE PAID TWICE**.

The same 21 cases also pass in `mock` mode.

## Vendors in the test set

| Code | Real company (name/domain from Crustdata) | Role in tests |
|---|---|---|
| V1 | Chander Prabhu Packers · cppackers.com (2 staff on Crustdata) | MSME micro → Tier 0 (case 13); over-billed (case 09) |
| V2 | Astral Limited · astralpipes.com | Impersonated by the case 07 fraud email |
| V3 | Pankaj Steels · pankajsteels.com (10 staff) | MSME trader (case 03), possible duplicate (case 06) |
| V4 | Kirloskar Ferrous Industries · kirloskarferrous.com | Critical castings supplier, overdue → Tier 0 (cases 02, 05, 10) |
| V5 | Tata Communications · tatacommunications.com | Large, trusted, low priority (cases 01, 16) |
| V6 | Gujarat Alkalies and Chemicals · gacl.com | 2/10 net 30 (case 14); genuine bank change (cases 19, 20, 20b) |
| V7 | Transport Corporation of India · tcifreight.in | Instalments (case 15); timeout case (case 12) |
| V8 | iLuxer Technologies · i-luxer.com (2 staff) | MSME small (case 04); homoglyph lookalike target (case 08) |
| V9 | Think and Learn (BYJU'S) · byjus.com | Real insolvency news → `ADVERSE_NEWS` (case 18) |

Customers (receivables): Maruti Suzuki, Future Retail (real liquidation news → low confidence), Thermax, Britannia.

## Honest notes

- GSTINs carry valid checksums (except case 10) and their state code matches each company's real HQ state; PAN = GSTIN[2:12].
  GSTIN *registration* can't be checked live (GST portal is captcha-gated): the product says so explicitly.
- Every live fact shows its source, the exact JSON field, and when it was retrieved. Recorded data keeps its original date.
- Real people's names are never committed: people-search recordings keep titles only.
- The API key was shared in a chat during the hackathon — rotate it.
