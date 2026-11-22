# PRODUCT.md: Payables Desk

<!-- impeccable:product-schema 1 -->

## Platform

web

## What it is

Payables Desk is an agent a company hands its accounts-payable work to. You give it this week's invoices from the accounts inbox. It reads each one, checks it against the company's own records and live public data, decides what is safe to pay, plans payments around the cash the company actually has, and waits for a person to approve before any money moves.

**One line:** A payables desk that checks every invoice against live records, then plans each week's payments around the company's cash: who gets paid, how much, and why.

**The request it answers, in the customer's words:**
> "Here are this week's invoices from our accounts inbox. Match each one to its purchase order, check the vendor and its bank account, pay each one once, according to the cash we have, and hold anything unusual for my approval with the reason."

It replaces a service, not a tool: the AP clerk or outsourced accountant who does this by hand every week.

## Positioning

It is the only option that both **verifies** and **plans cash**. Every invoice is checked against the company's own records (PO, goods receipt, vendor master) and against live public data (Crustdata company facts, the IFSC registry). Payments are then planned around the cash the company actually has, in instalments where needed. Every rupee carries a written reason and a dated source, and each payment goes out exactly once. Tally records what happened; AP automation tools route approvals; an outsourced clerk does it by hand without a traceable source for each claim.

## Operating Context

- The weekly AP run: invoices arrive in an accounts inbox (PDF, text, email), the finance head says what they want this week ("don't spend more than ₹15 lakh"), reviews what the desk held or blocked, and approves the payment run by name.
- Company records come from the books (Tally/Busy exports): vendor master, purchase orders, goods receipts (GRNs), receivables, bank balance and safety buffer.
- Bank-detail changes are verified out of band: call the phone number already on file, maker-checker approval by a second person.
- The auditor replays decisions later from the event ledger, the call log and the CSV export.
- Demo setting: a laptop or projector, offline-capable, two minutes, no voice-over.

## Who uses it

**Primary user: the finance head or owner of an Indian SME** (50–2,000 invoices a month, books kept in Tally or Busy). Not a designer, not a developer. Comfortable with spreadsheets, GST, TDS, purchase orders and bank portals. Busy, careful with money, and personally accountable when a payment goes wrong.

What they come to the screen to do:
1. Hand over the week's invoices and say what they want ("don't spend more than ₹15 lakh this week").
2. See at a glance what's safe to pay, what's held, and what was blocked.
3. Understand **why** for anything held or blocked, and check the evidence themselves.
4. Approve the payment run, adjust an amount if needed, and know each rupee goes out exactly once.

**Secondary user: the auditor**, who replays the books later, line by line. They need every decision traced to a source with a date.

**Third audience: hackathon judges**, who see it for two minutes. The product has to explain itself without a voice-over.

## What they must be able to trust

This is the whole product. Every design decision serves these:

- **Every rupee has a reason.** "₹1,20,000 of ₹1,80,000 now. MSME deadline 4 Dec, 12 days left." Never a bare number.
- **Every claim has a source and a date.** "Bank: HDFC · IFSC API · retrieved 22 Nov 15:40." A claim with no source is a bug.
- **"I can't verify" is an answer, not a failure.** When no record settles a question, the product says so plainly and names what's missing. It never guesses.
- **Nothing irreversible happens without a person.** Paying, changing a vendor's bank details and sending a message all wait for a named approver.
- **Each payment happens exactly once**, even through a crash.
- **Our data vs live data is always visible.** Internal records (POs, vendor master, bank balance) are labelled as ours. Live facts (Crustdata, IFSC) show their source and retrieval time. Test fixtures are labelled as mock.

## How it should feel

**Calm, precise, accountable.** Like a well-kept ledger or a good bank statement, not a startup dashboard. The user is handling real money and possible fraud; the interface should lower their pulse, not raise it.

- **Quiet by default, loud only when it matters.** Most of the screen is neutral. Colour and weight are saved for a blocked fraud attempt, a legal deadline in 3 days, or a cash shortfall.
- **Dense but readable.** Finance people like tables and numbers. Give them real information density, with clear hierarchy, not big empty cards with one number each.
- **Confident, plain language.** "Blocked: the bank-change email came from acme-supplies.co, not acmesupplies.com." Not "Uh-oh! Something looks off 🚩".
- **Indian finance context is native**, not translated: ₹, lakh/crore grouping, GSTIN, PAN, IFSC, MSME, Tally.

## Content rules

- **Money:** always `₹` with Indian grouping (`₹1,20,000`, `₹23.79L` only in summaries). Right-aligned in tables, tabular (monospaced) figures so columns line up. Paise only where they matter.
- **Dates:** `22 Nov 2026` in the UI; times as `15:40` IST. Deadlines also show the countdown ("12 days left").
- **Decisions use words, not only colour:** **Eligible**, **Held**, **Blocked**. Each has a distinct colour and icon, but the word is always present (colour-blind users, printed audit reports).
- **Reasons:** short headline first, evidence one click away. Format: *what's wrong* → *the fact* → *the source · date*.
  - "Over-billed: line 2 billed 120 units, PO 4471 ordered 100 · Purchase order PO-4471 · our records"
- **Sensitive data:** account numbers masked (`A/c ••••1234`). Real people's names from Crustdata are never shown in exports.
- **Mock vs live:** live sources read "live · retrieved 22 Nov 15:40"; recorded ones "live, recorded 22 Nov 15:40"; mocks "mock data". Never let mock data look live.
- **No AI cheerleading.** No "✨ AI-powered insights", no exclamation marks, no emoji in body copy. The AI only reads documents and writes explanations; the rules decide. The copy should reflect that.

## Screens (in priority order)

1. **Home: the guided flow.** Request box (pre-filled with the customer request) → job description the desk understood (scope, spend cap, approval required) → live checklist while it works → summary ("9 eligible, 5 held, 1 blocked, ₹X proposed. Your approval is needed.") → final report after payment. This is the first 20 seconds of the demo.
2. **Payment Run: the hero screen and the approval step.** Cash bar (bank balance − safety buffer + expected receipts = spendable), allocation table by tier (Tier 0 must-pay, Tier 1 early-pay discount, Tier 2 by priority), one headline + reasons per instalment, a slider per row to change an amount, and a named-approver "Approve run" action. Live status per instalment after approval.
3. **Invoice Desk.** Table of every document with its decision and top reason. Detail view: extracted fields, each reason with expandable evidence, the **PO match panel** (invoice lines side by side with PO and goods-receipt lines, the failing line highlighted), the original document, state history. Actions: re-check, release with note.
4. **Vendor 360.** Vendor table (name, GSTIN, MSME, trust, priority, status, open dues) → vendor detail: identity, live Crustdata facts with sources, trust and priority score breakdowns (kept separate on purpose), bank account history, and the bank-change verification checklist.
5. **Company Books.** Read-only: vendor master, POs and goods receipts, receivables, bank position. Labelled "Our internal records (synthetic test data)".
6. **Audit Ledger.** Event timeline, API/model calls with cost, payout reconciliation ("no rupee paid twice"), cost per invoice, auditor CSV export.

Every vendor name links to Vendor 360; every PO number links to Company Books; every number in the Home summary links to the screen behind it.

## Moments that must land

- **The blocked fraud email.** A polite "we've changed banks" email from a lookalike domain. The block should be the most visually serious thing in the product, with the evidence side by side: the domain on file vs the sender's domain, Reply-To vs From.
- **The cash bar.** One glance shows why not everything gets paid today.
- **An instalment's justification.** "₹1,20,000 of ₹1,80,000 now, the rest on 29 Nov after the ₹3.1L expected from Client X." Plain, specific, trustworthy.
- **"I can't verify."** Shown as a calm, deliberate state with the missing items listed, not as an error.

## What it is not

- Not a generic SaaS dashboard: no hero gradients, no glassmorphism, no rows of identical KPI cards, no stock illustrations.
- Not a chatbot. The request box takes a request; the product answers with work, not conversation.
- Not playful or gamified. No confetti on approval.
- Not a marketing site. There's no landing page; the first screen is the work.

## Evidence on Hand

- **Synthetic:** all company records (vendors, POs, GRNs, invoices in `data/inbox/`, receivables, bank position in `data/seed/`) are generated test data. Label them "our records (synthetic test data)".
- **Real:** Crustdata and IFSC lookups, live or recorded with their retrieval time. Shown with source and date; mock fixtures are always labelled as mock.
- **Golden test set:** `data/golden/expected.json` and `tests/` define the scenarios the product must handle (fraud email, duplicates, over-billing, cash crisis, crash recovery).
- **Absent, never fabricate:** real customers, testimonials, pilot results, usage metrics, pricing, savings claims.

## Product Principles

1. **Every claim has a source and a date.** If no record settles a question, the answer is "I can't verify", with the missing items named.
2. **Rules decide; the AI reads and explains.** A request can tighten the rules (spend cap, fewer vendors), never loosen them.
3. **Nothing irreversible without a named person.** Payments, bank-detail changes and releases all wait for an approver, and the approver's name is logged.
4. **Exactly once.** Each instalment is paid once, even through a crash, and the reconciliation proves it.
5. **Cash decides how much, trust decides whether.** Priority and trust stay separate; priority never makes an unsafe payment safe.

## Accessibility & Inclusion

- Target WCAG 2.1 AA: text contrast, full keyboard operation, visible focus, labelled controls on every screen.
- Decisions always use the word (Eligible, Held, Blocked) plus an icon, never colour alone. This also keeps printed audit reports readable.
- Readable when projected: 1280–1920 px desktop first, real information density, tabular figures.

## Technical context

- Frontend in `web/`, served by FastAPI (`app/api.py`) as one process and one URL. Data comes from JSON endpoints that wrap the existing Python functions.
- Must run offline for the demo (no CDN-only fonts or scripts that break without Wi-Fi; bundle assets).
- Desktop first (judges watch on a laptop or projector, 1280–1920 px wide). It should still be readable on a laptop at 1280 px and look sharp when projected.
- Light theme is the default (ledger feel, projector-friendly).
