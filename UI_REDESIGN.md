# Payables Desk: UI redesign spec (chat-first, no sidebar)

**Status:** spec only, nothing built yet. **Start after the live-data changes in `CHANGES_TODO.md` land.** Both touch `ui/common.py` and `ui/pages/3_Vendor_360.py`, so building in parallel will collide.

Commit after each numbered section. Re-run `python scripts/run_test_set.py` after any change outside `ui/`.

---

## Goal

Replace the five sidebar pages with one guided path:

```
Chat screen ──(prompt)──► live evaluation in chat ──► Results screen ──(Pay now)──► Paywall ──► Receipt
   │                                                     │
   └─ 🗂 View database overlay                           └─ 🔍 Explore each invoice overlay
                                                              └─ Bank details tab (verify a change request)
```

## Decisions already made

| Question | Decision |
|---|---|
| Chat follow-ups after results ("why is Acme held?") | **No.** One command per run. Explanations live in the invoice overlay. |
| Prompt parsing | **Rules first, LLM fallback.** Fallback reuses the existing Gemini path in `app/extract.py` (`gemini_extract`) only when a key is set. |
| Old pages | **Fold into the flow.** Vendor 360 → invoice overlay, Payment Run → paywall, Audit Ledger → link on the receipt. |
| Confirm step | **Echo, then run.** Show what was understood in one line, start immediately, no extra click. |
| Bank-change verdict | **The LLM only extracts and explains; rules decide.** An LLM never approves a bank change. |
| Design pass | Run `/impeccable critique` and `/impeccable clarify` **after** the flow is built, not before. |

---

## 0. App shell (no sidebar)

**Files:** `ui/Home.py` (becomes the only entry point), `ui/pages/*` (moved), new `ui/screens/`.

- Move the page bodies out of `ui/pages/` into functions under `ui/screens/` (`chat.py`, `results.py`, `invoice_overlay.py`, `paywall.py`). Delete `ui/pages/` so Streamlit stops generating a sidebar.
- `st.set_page_config(layout="centered", initial_sidebar_state="collapsed")`, plus CSS in `common.page()` that hides `[data-testid="stSidebar"]` and `[data-testid="collapsedControl"]`.
- A small router keyed on `st.session_state.screen` ∈ `{"chat", "results", "paywall", "receipt"}`. Every screen except chat has a `← Back to chat` button in the top-left.
- **Overlays use `@st.dialog`.** `width="large"` is about 750px, so add CSS: `div[role="dialog"] { width: 50vw; min-width: 640px; max-width: 1100px; }`.
- **Only one dialog can be open at a time.** The bank-change flow therefore lives *inside* the invoice dialog, as a tab, never as its own dialog.
- A small `Reset demo` link in the top-right corner calls the existing reset (`sd.reset(run=False)` in `Home.py`).

## 1. Chat screen

- Centered title and a one-line subtitle. No tiles or tables.
- Two buttons directly above the input:
  - `➕ Add invoices` uploads `.txt/.eml/.pdf` files into `config.INBOX`, plus an optional accounting CSV through `app/importer.py` (`CsvImporter` → `load`).
  - `🗂 View database` opens the database overlay (section 2).
- `st.chat_input(placeholder="e.g. Here are this week's invoices — match each to its PO, check the vendor and bank, pay each once, hold anything unusual for my approval.")`
- **Empty state** (empty inbox): the input stays enabled, and a hint above it says "Add invoices first."
- Messages render as `st.chat_message` bubbles: the user's prompt, then the app's echo, then the live evaluation (section 4).

## 2. Database overlay

- `@st.dialog("Invoice database")` wrapping the existing `render_database()` from `Home.py`, moved as-is: raw invoice rows, vendor selection, per-file preview, status column.
- Nothing here changes data. It's read-only.

## 3. Prompt parser

**New file:** `app/intent.py` → `parse(prompt: str, today: date) -> RunRequest | Rejection`.

```python
@dataclass
class RunRequest:
    action: Literal["pay", "evaluate"]       # "pay" if pay/settle/release/clear appears, else "evaluate"
    date_from: date | None                   # None = whole inbox
    date_to: date | None
    vendors: list[str]                       # matched against vendor names/aliases; empty = all
    acknowledged: list[str]                  # policy cues we heard, e.g. "pay once", "hold unusual"
    refused: list[str]                       # requests to weaken checks, e.g. "skip bank check"
    source: Literal["rules", "llm"]
```

**Rules (in order):**
1. **Relevance gate.** The prompt needs at least one finance noun (invoice, bill, payable, vendor, payment, PO) *and* one action verb (pay, check, match, process, evaluate, review, settle). Otherwise return `Rejection`.
2. **Scope:**
   - "this week" = Monday to today
   - "last week" = the previous Monday to Sunday
   - "past/last N days/weeks/months"
   - "this month" / "last month"
   - month names ("October invoices")
   - explicit dates via `extract.parse_date`
   - no scope given = the whole inbox
3. **Vendor filter.** Fuzzy match against `vendors.name_normalized` and `aliases`.
4. **Check-weakening phrases** ("skip", "don't check", "ignore", "bypass", "just pay everything") go into `refused`. **The checks always run.** The echo tells the user: "I'll still run every check."
5. **LLM fallback.** Only if the rules found no scope and no intent, and a key is set. Ask the LLM for the same JSON shape, then **re-validate it with the same rules**. The LLM can't add a refused item back as allowed.

**Rejection message:** short and friendly, with two example prompts. Clicking an example fills the input.

**Echo, then run:** one assistant bubble, for example:
> Evaluating **14 invoices** from **16–22 Nov** · checks: PO match, vendor identity, bank account, duplicates · anything unusual is held for you.

If the scope matches 0 invoices, say so and stop. Don't run on an empty set.

**Scope enforcement:** today `pipeline.iter_pipeline` processes every pending inbox file. Add an optional `scope` argument to `pipeline.ingest` / `iter_pipeline` that skips files whose invoice date (from a cheap regex pass using `extract.parse_date`) falls outside the range. Files that are skipped stay un-ingested. They are not marked as evaluated.

**Data change:** spread the generated invoice dates in `scripts/generate_mock_data.py` over about 8 weeks, so "past two months" and "this week" give different sets. Regenerate the golden set afterwards and make sure `run_test_set.py` still passes.

**Tests:** `tests/test_intent.py` with 10 to 15 prompts, covering the two sample prompts, three that must be rejected, two that try to weaken checks, and a few date phrases.

## 4. Live evaluation (in the chat)

- Reuse `evaluate_live()` from `Home.py` as-is (progress bar, per-invoice `st.status`, stage-by-stage check lines), rendered inside an assistant chat bubble.
- When it finishes, call `scheduler.propose_run(conn)` (already done there), then set `screen = "results"` and rerun.
- **Second run:** invoices already paid stay paid. A repeat invoice arriving later is caught as a duplicate and held. Remove the current "already evaluated, reset to replay" dead end: if nothing in scope is new, the echo says so and offers `View last results`.

## 5. Results screen

Show only what's needed:

- **4 tiles:**
  - **Ready to pay** (₹ total and count)
  - **Held for you** (count)
  - **Blocked** (count)
  - **Duplicates caught** (count)

  The numbers come from `summary.week_summary(conn)`.
- **One table, held and blocked invoices only:** vendor, invoice number, amount, decision badge, and a **one-line reason** (the top reason's message, not the full list).
- **Paid / ready invoices** go in a collapsed expander.
- **Buttons:**
  - `🔍 Explore each invoice` opens the invoice overlay at the first held item.
  - `💳 Pay now` (primary) goes to the paywall. It's disabled when nothing is ready.
- Cash position is one caption line ("₹X available · ₹Y planned this run"), not a section.

## 6. Invoice overlay

`@st.dialog("Invoice")` with `‹ Prev  3 of 14  Next ›` navigation. It opens on held items first, then blocked, then paid.

**Header:** vendor, invoice number, amount, due date, decision badge, trust score.

**Tabs:**
1. **Why**: the explanation (`inv["explanation"]`) plus `render_reasons()` from `common.py`, with each reason's facts and sources. Live (Crustdata/IFSC) facts get a "live" tag.
2. **Details**: the PO/GRN match, MSME deadline, early-payment discount and the scheduler's classification (from `scheduler.classify`).
3. **Vendor**: what Vendor 360 showed today: identity checks, headcount, adverse news, and the trust-score breakdown.
4. **Bank details**: section 7.

**Actions** (only for held items): `Release with note` (`pipeline.release_with_note`) and `Re-check` (`pipeline.recheck`). Released items join the paywall list.

## 7. Bank details tab (fraud-critical)

Shows the account on file (masked), plus any pending change request already detected by the pipeline (`_check_bank_change`).

**Verify a change request:**
1. Two text areas: **Paste the email** (headers included, if possible) and **Paste the SMS / WhatsApp message**. Both are optional, but at least one is required.
2. **Extract** with `extract.parse_bank_change_text` (rules). If key fields are missing and a key is set, call the LLM, which **extracts only**: sender address, requester name and title, new account number, IFSC, beneficiary name, and phrases that create pressure ("urgent", "confidential", "today only", "don't call").
3. **Check with rules and live data** (reusing `_bank_checks` / `_sender_checks` where possible):
   - Does the sender's domain belong to the vendor? (Crustdata identify by domain)
   - Is it a lookalike domain? (edit distance to the known domain, wrong top-level domain, extra hyphens)
   - Does the requester work there, in a finance role? (Crustdata people search)
   - Does the IFSC branch city match the vendor's head office state? (Razorpay IFSC)
   - Is there adverse news? (insolvency, NCLT, fraud)
   - Does the beneficiary name match the vendor's legal name?
   - Do the email and SMS agree with each other?
4. **Verdict** (rules only):

   | Verdict | When |
   |---|---|
   | 🟢 **Looks genuine** | The domain matches the vendor **and** the requester is confirmed in a finance role **and** no red flags |
   | 🟡 **Couldn't verify — call back required** | Any check returned "not found" or failed, with no red flags. *Not found is not proof of fraud, and not proof of genuineness.* |
   | 🔴 **Fraud indicators** | Any of: lookalike domain, requester not linked to the vendor, adverse news, beneficiary mismatch, email/SMS conflict |

   Each verdict lists the checks behind it, each with its source and a "live" tag.
5. **The checklist always follows, whatever the verdict.** It's the existing `vendor_admin` flow, moved from Vendor 360: call back on the number **on file** (`callback_number` / `record_callback`), second contact, penny drop (`run_penny_drop`), notice to the old contact, then `approval_blockers` → `approve_bank_change` / `reject_bank_change`.
6. **No verdict releases the invoice.** The invoice stays held until a person approves the change and the checklist is complete.

## 8. Paywall (mock)

A full screen with `← Back to results`.

- A clear **MOCK — no real money moves** banner.
- Lists the invoices to pay: everything ready, plus anything released in the overlay. Each shows vendor, amount and the masked beneficiary account.
- Total, the paying account, and the balance before and after (`scheduler.cash_position`).
- Approver name field (required) → `Confirm payment`.
- On confirm: a 1–2 second "processing" state, then `scheduler.approve_run(conn, run_id, approver)` → `payments.execute_approved(conn, actor="ui")`. In mock mode `get_provider()` returns `MockProvider`, so this uses the real code path against a fake bank.

## 9. Receipt

- ✅ Paid N invoices, ₹X total, each with a mock payment reference (UTR) from `MockProvider`.
- Anything still held is listed with "waiting on you" and a button to open its overlay.
- `📜 View audit trail` opens a dialog with the Audit Ledger table for this run (the current `4_Audit_Ledger.py` body).
- `Start a new run` returns to the chat.

---

## Build order (if time runs short, stop after any step)

1. Shell and router, chat screen, database overlay (sections 0–2)
2. Parser and live evaluation in the chat (3–4)
3. Results screen (5)
4. Invoice overlay without the bank tab (6)
5. Paywall and receipt (8–9)
6. Bank details tab (7)
7. `/impeccable critique` and `/impeccable clarify` on the running app

## Done means

- No sidebar anywhere. Every screen is reachable only through the flow, and every non-chat screen has a working `← Back`.
- Both sample prompts run. Off-topic prompts are rejected. "Skip the bank check" is refused with a visible note.
- A date scope changes which invoices get evaluated.
- The fraud demo vendor ends up 🔴 in the bank tab. A genuine change with an unknown requester ends up 🟡, never 🟢.
- Pay now → Confirm marks invoices paid. A second run catches the repeat invoice as a duplicate.
- `python scripts/run_test_set.py` and `tests/test_intent.py` pass.
