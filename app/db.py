"""Mock database: SQLite with the same tables, constraints and invariants as the
Supabase/Postgres design in BUILD_PROMPT.md §4. Swap point for Postgres: this file.

Money columns are INTEGER paise. Timestamps are ISO-8601 text (UTC).
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import uuid
from contextlib import contextmanager

from app import config

SCHEMA = r"""
CREATE TABLE IF NOT EXISTS vendors(
  id TEXT PRIMARY KEY,
  legal_name TEXT NOT NULL,
  name_normalized TEXT NOT NULL,
  aliases TEXT NOT NULL DEFAULT '[]',
  gstin TEXT UNIQUE,
  pan TEXT,
  udyam_no TEXT,
  msme_category TEXT NOT NULL DEFAULT 'none' CHECK (msme_category IN ('micro','small','medium','none')),
  is_trader INTEGER NOT NULL DEFAULT 0,
  has_written_agreement INTEGER NOT NULL DEFAULT 1,
  domain TEXT,
  phone_on_file TEXT,
  city TEXT, state TEXT,
  credit_days INTEGER NOT NULL DEFAULT 30,
  discount_pct REAL NOT NULL DEFAULT 0,
  discount_days INTEGER NOT NULL DEFAULT 0,
  is_critical INTEGER NOT NULL DEFAULT 0,
  relationship_since TEXT,
  hist_invoices INTEGER NOT NULL DEFAULT 0,
  hist_clean INTEGER NOT NULL DEFAULT 0,
  hist_on_time INTEGER NOT NULL DEFAULT 0,
  hist_deliveries INTEGER NOT NULL DEFAULT 0,
  hist_disputes INTEGER NOT NULL DEFAULT 0,
  crustdata_company_id INTEGER,
  trust_score REAL, priority_score REAL,
  trust_breakdown TEXT, priority_breakdown TEXT,
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','on_hold','blocked')),
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS vendor_bank_accounts(
  id TEXT PRIMARY KEY,
  vendor_id TEXT NOT NULL REFERENCES vendors(id),
  account_no TEXT NOT NULL, ifsc TEXT NOT NULL, beneficiary_name TEXT,
  bank_name TEXT, branch TEXT, city TEXT, state TEXT,
  valid_from TEXT, valid_to TEXT,
  status TEXT NOT NULL CHECK (status IN ('current','pending_verification','rejected','retired')),
  proposed_by TEXT, approved_by TEXT, verification_note TEXT,
  source_doc_id TEXT,
  evidence_fact_ids TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  CHECK (approved_by IS NULL OR proposed_by IS NULL OR approved_by <> proposed_by)  -- maker-checker
);
CREATE UNIQUE INDEX IF NOT EXISTS one_current_account ON vendor_bank_accounts(vendor_id) WHERE status='current';

CREATE TABLE IF NOT EXISTS purchase_orders(
  id TEXT PRIMARY KEY, po_number TEXT UNIQUE NOT NULL, vendor_id TEXT NOT NULL REFERENCES vendors(id),
  po_date TEXT, promised_date TEXT,
  status TEXT NOT NULL CHECK (status IN ('OPEN','PARTIALLY_RECEIVED','PARTIALLY_INVOICED','CLOSED','CANCELLED')),
  source TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS po_lines(
  id TEXT PRIMARY KEY, po_id TEXT NOT NULL REFERENCES purchase_orders(id), line_no INTEGER NOT NULL,
  item TEXT NOT NULL, unit TEXT, qty_ordered REAL NOT NULL, unit_price INTEGER NOT NULL, gst_rate REAL NOT NULL,
  qty_received REAL NOT NULL DEFAULT 0, qty_invoiced REAL NOT NULL DEFAULT 0,
  UNIQUE(po_id, line_no),
  CHECK (qty_invoiced <= qty_ordered)
);
CREATE TABLE IF NOT EXISTS grns(
  id TEXT PRIMARY KEY, po_id TEXT NOT NULL REFERENCES purchase_orders(id), line_no INTEGER NOT NULL,
  qty REAL NOT NULL, received_date TEXT NOT NULL, grn_number TEXT UNIQUE NOT NULL
);

CREATE TABLE IF NOT EXISTS invoices(
  id TEXT PRIMARY KEY,
  file_sha256 TEXT UNIQUE NOT NULL,
  source_path TEXT, source_kind TEXT, doc_kind TEXT NOT NULL DEFAULT 'invoice',
  case_id TEXT,
  vendor_id TEXT REFERENCES vendors(id),
  vendor_name_raw TEXT, gstin_raw TEXT,
  invoice_no_raw TEXT, invoice_no_norm TEXT,
  invoice_date TEXT, accepted_date TEXT, due_date TEXT, msme_deadline TEXT,
  po_number_raw TEXT, po_id TEXT REFERENCES purchase_orders(id),
  subtotal INTEGER, tax INTEGER, total INTEGER,
  outstanding INTEGER CHECK (outstanding IS NULL OR outstanding >= 0),
  bank_account_raw TEXT, ifsc_raw TEXT, beneficiary_raw TEXT,
  sender_email TEXT, reply_to TEXT, signatory TEXT, signatory_title TEXT,
  content_hash TEXT,
  extracted TEXT,
  state TEXT NOT NULL,
  decision TEXT CHECK (decision IN ('PAY_ELIGIBLE','HOLD','BLOCK')),
  reasons TEXT NOT NULL DEFAULT '[]',
  explanation TEXT,
  attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT, locked_until TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS invoice_lines(
  invoice_id TEXT NOT NULL REFERENCES invoices(id), line_no INTEGER NOT NULL,
  item TEXT, qty REAL, unit_price INTEGER, gst_rate REAL, amount INTEGER,
  PRIMARY KEY (invoice_id, line_no)
);
-- The DB itself refuses to make the same invoice identity payable twice.
-- (A duplicate can still exist as a HELD row in `invoices`.)
CREATE TABLE IF NOT EXISTS invoice_identities(
  vendor_id TEXT NOT NULL, invoice_no_norm TEXT NOT NULL, invoice_id TEXT NOT NULL UNIQUE,
  PRIMARY KEY (vendor_id, invoice_no_norm)
);

CREATE TABLE IF NOT EXISTS pipeline_runs(
  id TEXT PRIMARY KEY, started_at TEXT, finished_at TEXT,
  time_limit_s INTEGER, credit_limit REAL, credits_used REAL NOT NULL DEFAULT 0, status TEXT
);

CREATE TABLE IF NOT EXISTS payment_runs(
  id TEXT PRIMARY KEY, run_date TEXT, bank_balance INTEGER, safety_buffer INTEGER,
  expected_receipts INTEGER, spendable INTEGER,
  status TEXT NOT NULL CHECK (status IN ('DRAFT','PROPOSED','APPROVED','EXECUTING','DONE','ABORTED')),
  summary TEXT NOT NULL DEFAULT '{}',
  approved_by TEXT, approved_at TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS instalments(
  id TEXT PRIMARY KEY,
  idem_key TEXT UNIQUE NOT NULL,          -- generated once at insert, frozen; never regenerated
  invoice_id TEXT NOT NULL REFERENCES invoices(id), run_id TEXT NOT NULL REFERENCES payment_runs(id),
  instalment_no INTEGER NOT NULL,
  amount INTEGER NOT NULL CHECK (amount > 0),
  discount_amount INTEGER NOT NULL DEFAULT 0,
  tier INTEGER CHECK (tier IN (0,1,2)),
  justification TEXT NOT NULL DEFAULT '{}',
  proposed_amount INTEGER,
  approver_override INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL CHECK (status IN ('PROPOSED','APPROVED','INTENT_WRITTEN','SUBMITTED','PAID','FAILED','CANCELLED')),
  provider TEXT, provider_ref TEXT, provider_status TEXT, last_error TEXT,
  approved_by TEXT, approved_at TEXT, paid_at TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  UNIQUE(invoice_id, instalment_no)
);
-- invariant: sum(non-cancelled instalments + discounts) <= invoice total
CREATE TRIGGER IF NOT EXISTS instalments_cap BEFORE INSERT ON instalments
BEGIN
  SELECT RAISE(ABORT, 'instalments would exceed invoice total')
  WHERE (SELECT COALESCE(SUM(amount + discount_amount),0) FROM instalments
         WHERE invoice_id = NEW.invoice_id AND status NOT IN ('CANCELLED','FAILED'))
        + NEW.amount + NEW.discount_amount
        > (SELECT total FROM invoices WHERE id = NEW.invoice_id);
END;

CREATE TABLE IF NOT EXISTS mock_provider_payouts(
  idem_key TEXT PRIMARY KEY, payout_id TEXT UNIQUE NOT NULL, amount INTEGER NOT NULL,
  account_no TEXT, ifsc TEXT, beneficiary TEXT, narration TEXT, status TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS customers(
  id TEXT PRIMARY KEY, legal_name TEXT, gstin TEXT, domain TEXT, crustdata_company_id INTEGER,
  on_time_rate REAL, health_score REAL, health_breakdown TEXT
);
CREATE TABLE IF NOT EXISTS receivables(
  id TEXT PRIMARY KEY, customer_id TEXT REFERENCES customers(id), invoice_no TEXT, amount INTEGER,
  due_date TEXT, confidence REAL, confidence_breakdown TEXT,
  status TEXT CHECK (status IN ('OPEN','PAID','OVERDUE','WRITTEN_OFF')),
  paylink_id TEXT, paylink_url TEXT, paylink_status TEXT
);

-- Evidence & audit ledger (append-only) ---------------------------------
CREATE TABLE IF NOT EXISTS raw_responses(
  id TEXT PRIMARY KEY, endpoint TEXT, request TEXT, response TEXT, status INTEGER, retrieved_at TEXT
);
CREATE TABLE IF NOT EXISTS facts(
  id TEXT PRIMARY KEY, entity_type TEXT, entity_id TEXT, key TEXT, value TEXT,
  source_type TEXT, source_ref TEXT, source_field TEXT, retrieved_at TEXT, raw_response_id TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS events(
  seq INTEGER PRIMARY KEY AUTOINCREMENT, entity_type TEXT, entity_id TEXT,
  from_state TEXT, to_state TEXT, actor TEXT, detail TEXT,
  at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS calls(
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, invoice_id TEXT, tool TEXT, endpoint TEXT,
  credits REAL NOT NULL DEFAULT 0, input_tokens INTEGER, output_tokens INTEGER, est_cost_inr REAL,
  latency_ms INTEGER, ok INTEGER, http_status INTEGER, error TEXT, mode TEXT,
  at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS cache(key TEXT PRIMARY KEY, value TEXT, retrieved_at TEXT);

CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT,'events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT,'events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS facts_no_update BEFORE UPDATE ON facts BEGIN SELECT RAISE(ABORT,'facts are append-only'); END;
CREATE TRIGGER IF NOT EXISTS facts_no_delete BEFORE DELETE ON facts BEGIN SELECT RAISE(ABORT,'facts are append-only'); END;
"""


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def new_id() -> str:
    return str(uuid.uuid4())


def connect(path=None) -> sqlite3.Connection:
    p = path or config.DB_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


@contextmanager
def tx(conn: sqlite3.Connection):
    """One atomic transaction. Every state transition goes through here."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def q(conn, sql: str, args=()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def q1(conn, sql: str, args=()) -> dict | None:
    r = conn.execute(sql, args).fetchone()
    return dict(r) if r else None


def j(v) -> str:
    return json.dumps(v, default=str, ensure_ascii=False)


def unj(s, default=None):
    if s is None or s == "":
        return default
    try:
        return json.loads(s)
    except (TypeError, ValueError):
        return default
