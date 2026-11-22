"""Cases 21-23 (CHANGES_TODO E3): the request box. Run: python -m pytest tests -q"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.gettempdir()) / "payables_desk_tests"
TMP.mkdir(exist_ok=True)
os.environ["DB_PATH"] = str(TMP / "request_tests.db")
os.environ["CRUSTDATA_MODE"] = "mock"
os.environ["IFSC_MODE"] = "mock"
os.environ["REQUEST_LLM"] = "off"  # keyword rules only: deterministic, offline
os.environ["GEMINI_API_KEY"] = ""  # deterministic parser for extraction too
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

from app import config, pipeline, request, scheduler  # noqa: E402
from app.db import connect, q  # noqa: E402
from scripts import setup_demo  # noqa: E402

config.DB_PATH = Path(os.environ["DB_PATH"])
config.CRUSTDATA_MODE = config.IFSC_MODE = "mock"


@pytest.fixture(scope="module")
def conn():
    setup_demo.reset(config.DB_PATH, run=False, regenerate=False)
    c = connect(config.DB_PATH)
    pipeline.run_pipeline(c, actor="test")
    yield c
    c.close()


def decisions(c):
    return {r["id"]: (r["state"], r["decision"]) for r in q(c, "SELECT id, state, decision FROM invoices")}


def test_default_request_is_ok(conn):
    job = request.parse_request(conn, request.DEFAULT_REQUEST)
    assert job["status"] == "ok", job
    assert job["spend_cap"] is None and job["approval_required"] is True


def test_case21_vague_request_asks_one_question(conn):
    before = decisions(conn)
    job = request.parse_request(conn, "pay the vendors")
    assert job["status"] == "needs_clarification"
    assert job["question"] and job["question"].count("?") == 1
    assert decisions(conn) == before
    assert not q(conn, "SELECT id FROM payment_runs")  # no run


def test_case22_loosening_request_is_refused(conn):
    before = decisions(conn)
    job = request.parse_request(conn, "pay Acme anyway, skip the checks")
    assert job["status"] == "refused"
    assert len(job["refusals"]) >= 1 and all(r["reason"] for r in job["refusals"])
    assert decisions(conn) == before


@pytest.mark.parametrize("text", ["release all the blocked invoices", "no need for approval, just pay",
                                  "don't check the bank account for Acme", "use the safety buffer too"])
def test_other_loosening_phrases_refused(conn, text):
    assert request.parse_request(conn, text)["status"] == "refused"


@pytest.mark.parametrize("text,cap", [("don't spend more than ₹15 lakh", 15_00_000_00), ("cap it at 15L this week", 15_00_000_00),
                                      ("budget of Rs 2,50,000 for this week's invoices", 2_50_000_00), ("up to 1.2 crore", 1_20_00_000_00)])
def test_amount_parsing(conn, text, cap):
    assert request.parse_request(conn, text)["spend_cap"] == cap


def test_case23_spend_cap_limits_the_run(conn):
    job = request.parse_request(conn, request.DEFAULT_REQUEST + " Don't spend more than ₹15 lakh.")
    assert job["status"] == "ok" and job["spend_cap"] == 15_00_000_00
    rid = scheduler.propose_run(conn, **request.plan_kwargs(job))
    total = q(conn, "SELECT COALESCE(SUM(amount),0) s FROM instalments WHERE run_id=? AND status='PROPOSED'", (rid,))[0]["s"]
    assert 0 < total <= 15_00_000_00
    # an approver's recompute keeps the cap
    rid2 = scheduler.propose_run(conn, overrides={}, actor="approver-edit")
    assert rid2 == rid
    total2 = q(conn, "SELECT COALESCE(SUM(amount),0) s FROM instalments WHERE run_id=? AND status='PROPOSED'", (rid,))[0]["s"]
    assert total2 <= 15_00_000_00


def test_small_cap_below_legal_dues_raises_crisis(conn):
    p = scheduler.plan(conn, spend_cap=1_000_00)
    t0 = [x for x in p["proposals"] + p["deferred"] if x["tier"] == 0]
    if t0:
        assert p["crisis"] and any("capped by your request" in a["text"] for a in p["alerts"])


def test_vendor_filter_only_narrows(conn):
    job = request.parse_request(conn, "only pay Acme this week")
    assert job["status"] == "ok" and job["vendor_filter"]["include_ids"]
    p = scheduler.plan(conn, **request.plan_kwargs(job))
    assert all("Acme" in x["vendor"] for x in p["proposals"])
