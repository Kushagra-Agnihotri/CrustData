"""Central configuration. Reads .env (if present) then environment variables."""
from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


def _load_dotenv() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()


def _get(name: str, default: str) -> str:
    return os.environ.get(name, default)


DB_PATH = Path(_get("DB_PATH", str(DATA / "payables.db")))
INBOX = Path(_get("INBOX_DIR", str(DATA / "inbox")))
RUN_DATE = dt.date.fromisoformat(_get("RUN_DATE", "2026-11-22"))
HORIZON_DAYS = int(_get("HORIZON_DAYS", "7"))
COST_OF_CASH = float(_get("COST_OF_CASH", "0.12"))  # annual

# Run ceilings (checked before every external call)
RUN_TIME_LIMIT_S = int(_get("RUN_TIME_LIMIT_S", "900"))
RUN_CREDIT_LIMIT = float(_get("RUN_CREDIT_LIMIT", "200"))

# Sources: "mock" (offline fixtures in data/mock_sources.json) or "live"
CRUSTDATA_MODE = _get("CRUSTDATA_MODE", "mock")
IFSC_MODE = _get("IFSC_MODE", "mock")
CRUSTDATA_API_KEY = _get("CRUSTDATA_API_KEY", "")
CRUSTDATA_BASE = _get("CRUSTDATA_BASE", "https://api.crustdata.com")
CRUSTDATA_API_VERSION = _get("CRUSTDATA_API_VERSION", "2025-11-01")
# Assumption for the cost ledger; set from Crustdata's pricing page.
CRUSTDATA_INR_PER_CREDIT = float(_get("CRUSTDATA_INR_PER_CREDIT", "1.0"))

# LLM (optional). Without a key, the deterministic parser extracts invoices.
GEMINI_API_KEY = _get("GEMINI_API_KEY", "")
LLM_MODEL = _get("LLM_MODEL", "gemini-2.5-flash")
LLM_INR_PER_1K_INPUT = float(_get("LLM_INR_PER_1K_INPUT", "0.025"))
LLM_INR_PER_1K_OUTPUT = float(_get("LLM_INR_PER_1K_OUTPUT", "0.21"))

PAYMENT_PROVIDER = _get("PAYMENT_PROVIDER", "mock")

# Decision + allocation parameters
TRUST_GATE = 0.4
PRICE_TOLERANCE = 0.02
MIN_INSTALMENT_PCT = 0.25
MIN_INSTALMENT_PAISE = 10_000 * 100
MAX_INSTALMENTS = 3
RECEIPT_CONFIDENCE_MIN = 0.7
MAX_INVOICE_AGE_DAYS = 180

BUYER_NAME = "Orbit Manufacturing Pvt Ltd"
