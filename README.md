# Payables Desk — prototype

> A payables desk that checks every invoice against live records, then plans each week's payments around the company's cash — who gets paid, how much, and why.

ContextCon 05 · Theme 6.1 (Buying and Paying) · team Aditya, Kushagra, Rajat. Full design: [BUILD_PROMPT.md](BUILD_PROMPT.md).

## Run it (Windows / macOS / Linux)

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt        # macOS/Linux: .venv/bin/pip
.venv/Scripts/python scripts/setup_demo.py --run      # builds the mock DB, runs the pipeline, proposes a payment run
.venv/Scripts/streamlit run ui/Home.py                # UI at http://localhost:8501
```

```bash
.venv/Scripts/python scripts/run_test_set.py    # golden test set, pass/fail table
.venv/Scripts/python scripts/crash_demo.py      # kill mid-payment, restart, reconcile
.venv/Scripts/python -m app.worker              # background worker (pipeline + approved payments)
```

On Windows set `PYTHONUTF8=1` if the console shows encoding errors.

## What's in the prototype

| Area | Where | Status |
|---|---|---|
| Mock database (SQLite, same tables/constraints as the Postgres design) | `app/db.py` | ✅ — money in integer paise; maker-checker CHECK; append-only triggers on `events`/`facts`; `invoice_identities` so a duplicate can be *held* but never made payable |
| Mock world generator (vendors, POs, GRNs, receivables, 16 invoices in 3 layouts + emails) | `scripts/generate_mock_data.py` | ✅ deterministic |
| CSV + Tally-shaped importer | `app/importer.py` | ✅ (PO-4480 comes from `tally_export_sample.csv`) |
| Extraction | `app/extract.py` | ✅ deterministic parser; Gemini path wired (set `GEMINI_API_KEY`), cached by sha256 |
| Validation: arithmetic, GSTIN checksum, PAN, IFSC, dates | `app/validators.py`, `app/pipeline.py` | ✅ |
| Vendor resolution: GSTIN key, aliases, homoglyph lookalikes, "I can't verify" | `app/pipeline.py` | ✅ |
| PO 2-/3-way match, 3-level duplicates, bank-detail fraud | `app/pipeline.py` | ✅ |
| Sources with ceilings, cache, retries, fault injection, cost ledger | `app/sources.py` | ✅ mock fixtures; live IFSC works (`IFSC_MODE=live`); live Crustdata identify wired, other endpoints need doc check |
| Trust + priority scores (separate) | `app/scoring.py` | ✅ |
| Liquidity-aware payment run (tiers, greedy allocation, justifications, overrides) | `app/scheduler.py` | ✅ |
| Exactly-once payments + crash recovery | `app/payments.py`, `scripts/crash_demo.py` | ✅ MockProvider; RazorpayX test provider scaffolded |
| UI: Home, Invoice Desk, Payment Run, Vendor 360, Audit Ledger | `ui/` | ✅ |

## Test set (from a clean DB)

```
case01  Clean invoice, PO matches (large vendor, low priority)    ✅ PASS  PAY_ELIGIBLE T2
case02  Clean invoice via email; critical supplier overdue        ✅ PASS  PAY_ELIGIBLE T0
case03  Clean invoice; MSME trader (45-day rule n/a)              ✅ PASS  PAY_ELIGIBLE T2
case04  Clean invoice; MSME small, deadline not near              ✅ PASS  PAY_ELIGIBLE T2
case05  Re-issue of #2 as INV/42 vs INV-0042                      ✅ PASS  HOLD [OVER_BILLED, DUPLICATE_INVOICE]
case06  Same vendor + amount, new number, 3 days later            ✅ PASS  HOLD [POSSIBLE_DUPLICATE]
case07  Polite bank-change email from lookalike domain            ✅ PASS  BLOCK [BANK_CHANGE_REQUEST, SENDER_DOMAIN_MISMATCH, …]
case08  Homoglyph lookalike vendor, different GSTIN               ✅ PASS  HOLD [VENDOR_NOT_APPROVED]
case09  Billed qty 120 > PO qty 100                               ✅ PASS  HOLD [OVER_BILLED]
case10  GSTIN fails checksum                                      ✅ PASS  BLOCK [INVALID_GSTIN, GSTIN_MISMATCH]
case11  Sharma Traders: no GSTIN, no PO → I can't verify          ✅ PASS  HOLD [UNVERIFIABLE_VENDOR]
case12  Crustdata + IFSC timeout injected                         ✅ PASS  HOLD [VENDOR_ON_HOLD, SOURCE_UNAVAILABLE]
case13  MSME micro, accepted 40 days ago → Tier 0                 ✅ PASS  PAY_ELIGIBLE T0
case14  2/10 net 30 within discount window → Tier 1               ✅ PASS  PAY_ELIGIBLE T1
case15  Large invoice, cash short → instalments                   ✅ PASS  PAY_ELIGIBLE T2 ₹872,238 of ₹1,770,000
case16  Case 1 re-worded + re-laid-out                            ✅ PASS  PAY_ELIGIBLE (case01: PAY_ELIGIBLE)
case17  kill -9 mid-payment, restart → each instalment paid once  ✅ PASS  crash exit 137; 7 payouts for 7 instalments; 1 recovered
17/17 passed
```

## Honest notes

- **All companies, people, GSTINs, PANs and bank accounts are synthetic.** GSTINs carry valid checksums (except case 10). IFSC codes are real branch codes, so the live Razorpay IFSC API resolves them.
- In `mock` mode, facts are labelled `crustdata_mock` / `ifsc_api_mock` in the UI — a fixture is never shown as live data. For the hackathon's live-data rule, switch `IFSC_MODE=live` (works today) and `CRUSTDATA_MODE=live` with a key, then swap the fictional vendors for real companies that Crustdata identifies.
- GSTIN *registration* can't be checked live (GST portal is captcha-gated): the product says so explicitly.
- Next: Postgres/Supabase swap (`app/db.py`), PDF ingestion, live Crustdata enrich/person/web endpoints verified against docs, RazorpayX test account.
