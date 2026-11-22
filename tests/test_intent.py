"""Prompt parser tests. Run: python tests/test_intent.py  (or pytest, if installed)."""
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, intent  # noqa: E402
from app.intent import Rejection, RunRequest  # noqa: E402

config.GEMINI_API_KEY = ""  # tests never call the LLM
TODAY = dt.date(2026, 11, 22)  # a Sunday
VENDORS = [{"legal_name": "Kirloskar Ferrous Industries Ltd", "aliases": "[]"},
           {"legal_name": "Tata Communications Ltd", "aliases": "[\"TCL\"]"}]


def p(text):
    return intent.parse(text, TODAY, VENDORS)


def test_sample_prompt_this_week():
    r = p("Here are this week's invoices from our accounts inbox. Match each one to its purchase order, check the vendor and "
          "its bank account, pay each one once, and hold anything unusual for my approval with the reason.")
    assert isinstance(r, RunRequest)
    assert r.action == "pay" and r.date_from is None and r.scope_label == "this week's inbox"
    assert "pay each invoice once" in r.acknowledged and "hold anything unusual for your approval" in r.acknowledged
    assert not r.refused


def test_past_two_months():
    r = p("here are the invoices for the past two months, check them and pay what is safe")
    assert isinstance(r, RunRequest)
    assert r.date_from == dt.date(2026, 9, 22) and r.date_to == TODAY


def test_last_30_days():
    r = p("review invoices from the last 30 days")
    assert r.date_from == TODAY - dt.timedelta(days=30) and r.action == "evaluate"


def test_past_week_is_rolling():
    r = p("check the past week's bills")
    assert r.date_from == TODAY - dt.timedelta(days=7)


def test_last_week_is_calendar():
    r = p("pay last week's invoices")
    assert r.date_from == dt.date(2026, 11, 9) and r.date_to == dt.date(2026, 11, 15)


def test_month_name():
    r = p("process the October invoices")
    assert r.date_from == dt.date(2026, 10, 1) and r.date_to == dt.date(2026, 10, 31)


def test_last_month():
    r = p("pay last month's bills")
    assert r.date_from == dt.date(2026, 10, 1) and r.date_to == dt.date(2026, 10, 31)


def test_since_date():
    r = p("check invoices since 1 Nov 2026")
    assert r.date_from == dt.date(2026, 11, 1) and r.date_to == TODAY


def test_vendor_filter():
    r = p("pay the Kirloskar invoices")
    assert r.vendors == ["Kirloskar Ferrous Industries Ltd"]


def test_reject_off_topic():
    assert isinstance(p("what's the weather in Pune tomorrow?"), Rejection)


def test_reject_too_short():
    assert isinstance(p("hi"), Rejection)


def test_reject_no_finance_noun():
    assert isinstance(p("write me a poem about the sea"), Rejection)


def test_refuse_skip_bank_check():
    r = p("pay all invoices this week and skip the bank check")
    assert isinstance(r, RunRequest) and r.refused


def test_refuse_just_pay_everything():
    r = p("just pay everything in the inbox, no holds")
    assert isinstance(r, RunRequest) and len(r.refused) >= 1


if __name__ == "__main__":
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            try:
                fn()
                print("ok  ", name)
            except AssertionError as e:
                fails += 1
                print("FAIL", name, e)
    sys.exit(1 if fails else 0)
