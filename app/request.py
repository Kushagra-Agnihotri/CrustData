"""Customer request text -> job description (CHANGES_TODO E1).

    parse_request(conn, text) -> {
        status: "ok" | "needs_clarification" | "refused",
        scope, actions[], spend_cap (paise | None), vendor_filter {include_ids, exclude_ids, msme_only},
        approval_required (always True), question, refusals[], understood[], method
    }

Rules (enforced here in code, whatever the LLM says):
- A request can TIGHTEN the desk's rules (spend cap, fewer vendors, MSME only), never LOOSEN them.
  "Pay Acme anyway" / "skip the bank check" is refused with a reason. Overrides go through the
  approval screens, with the approver's name logged.
- A vague request ("pay the vendors") gets exactly one clarifying question, and nothing runs.

Gemini (structured output) is used when a key is set and REQUEST_LLM != "off"; the keyword parser
always runs and its guardrails always apply, so the demo works offline and the rules can't be talked around.
"""
from __future__ import annotations

import json
import os
import re
from decimal import Decimal

from app import config
from app.db import q
from app.money import inr

DEFAULT_REQUEST = ("Here are this week's invoices from our accounts inbox. Match each one to its purchase order, "
                   "check the vendor and its bank account, pay each one once, according to the cash we have, "
                   "and hold anything unusual for my approval with the reason.")

BASE_ACTIONS = ["Read every document in the accounts inbox",
                "Match each invoice to its purchase order and goods receipt",
                "Check the vendor, its GSTIN and its bank account against our records and live sources",
                "Plan payments around the cash we have, each invoice paid once",
                "Hold anything unusual with the reason, and wait for a named approver"]

CLARIFY_QUESTION = ("Which invoices should I work on, and is there a spending limit? "
                    "For example: “this week's invoices from the inbox, don't spend more than ₹15 lakh”.")

# Phrases that try to LOOSEN the rules. Each maps to the reason we give back.
_LOOSEN = [
    (r"\banyway\b|\bregardless\b|\bno matter what\b|\beven if\b",
     "The desk can't be told to pay regardless of its checks. Release a held invoice on the Invoice Desk with your name and a note."),
    (r"\b(skip|ignore|bypass|disable|turn off|without|no)\b[^.]{0,30}\b(check|checks|checking|verification|verify|validation|match|matching|review)\b",
     "Checks can't be skipped by request. Every invoice is matched and verified; you can release a held one with your name and a note."),
    (r"\b(don'?t|do not|never)\s+(check|verify|match|validate|hold|block)\b",
     "Checks can't be switched off by request. Every invoice is matched and verified before it can be paid."),
    (r"\b(release|unblock|unhold|pay)\b[^.]{0,20}\b(blocked|held|on hold|everything|all of them|all invoices|every invoice)\b",
     "Held and blocked invoices stay held until a named approver releases them on the Invoice Desk. Blocked ones can't be released."),
    (r"\b(no|without|skip)\b[^.]{0,15}\bapproval\b|\bauto[- ]?(approve|pay)\b|\bdon'?t (ask|wait)\b",
     "Nothing irreversible happens without a person. The payment run always waits for a named approver."),
    (r"\b(change|update|switch)\b[^.]{0,30}\bbank (account|details)\b",
     "Bank details change only through the vendor's bank-change checklist (call the number on file, maker-checker), not by request."),
    (r"\b(dip into|use|touch|ignore|spend)\b[^.]{0,15}\b(safety )?buffer\b|\bspend (it )?all\b|\bmore than (we have|the cash)\b",
     "The safety buffer is never spent by request. A request can lower the spend limit, never raise it."),
]

_CAP_CONTEXT = r"(?:don'?t spend more than|do not spend more than|no more than|not more than|up to|upto|at most|max(?:imum)?|cap(?:ped)?(?: it)?(?: at| of)?|limit(?:ed)?(?: to| of)?|under|below|within|budget(?: of| is)?|not (?:to )?exceed|ceiling(?: of)?)"
_AMOUNT = r"(?:₹|rs\.?|inr)?\s*([\d][\d,]*(?:\.\d+)?)\s*(lakhs?|lacs?|l\b|crores?|cr\b|k\b|thousand)?"
_UNITS = {"lakh": 100_000, "lakhs": 100_000, "lac": 100_000, "lacs": 100_000, "l": 100_000,
          "crore": 10_000_000, "crores": 10_000_000, "cr": 10_000_000, "k": 1_000, "thousand": 1_000}

# Words that show the request says what to work on (so it isn't vague).
_SCOPE_WORDS = r"\b(invoice|invoices|inbox|bill|bills|purchase order|po|match|check|verify|hold|week|this week|today|msme|cash|liquidity|due)\b"


def _amount_to_paise(num: str, unit: str | None) -> int | None:
    try:
        d = Decimal(num.replace(",", ""))
    except Exception:  # noqa: BLE001
        return None
    mult = _UNITS.get((unit or "").lower().strip(), 1)
    return int(d * mult * 100)


def _spend_caps(text: str) -> list[int]:
    caps = []
    for m in re.finditer(_CAP_CONTEXT + r"\s*(?:of\s+|to\s+)?" + _AMOUNT, text, re.I):
        v = _amount_to_paise(m.group(1), m.group(2))
        if v and v > 0:
            caps.append(v)
    return caps


def _vendor_tokens(v: dict) -> list[str]:
    stop = {"pvt", "ltd", "private", "limited", "llp", "the", "and", "co", "company", "india", "industries", "traders", "supplies"}
    names = [v["legal_name"]] + (json.loads(v.get("aliases") or "[]") if v.get("aliases") else [])
    out = []
    for n in names:
        words = [w for w in re.findall(r"[a-z0-9]+", n.lower()) if w not in stop and len(w) > 2]
        if words:
            out.append(words[0])
    return list(dict.fromkeys(out))


def _vendor_filter(conn, text: str) -> tuple[dict, list[str]]:
    low = text.lower()
    vf = {"include_ids": [], "exclude_ids": [], "msme_only": False}
    notes = []
    vendors = q(conn, "SELECT id, legal_name, aliases, msme_category FROM vendors") if conn is not None else []
    for v in vendors:
        for tok in _vendor_tokens(v):
            if not re.search(rf"\b{re.escape(tok)}\b", low):
                continue
            if re.search(rf"\b(except|exclude|excluding|not|skip|leave out|without|don'?t pay)\b[^.]{{0,25}}\b{re.escape(tok)}\b", low):
                vf["exclude_ids"].append(v["id"])
                notes.append(f"Leave out {v['legal_name']}")
            elif re.search(rf"\b(only|just|solely)\b[^.]{{0,25}}\b{re.escape(tok)}\b", low):
                vf["include_ids"].append(v["id"])
                notes.append(f"Only {v['legal_name']}")
            break
    if re.search(r"\b(only|just)\b[^.]{0,15}\bmsme", low):
        vf["msme_only"] = True
        notes.append("Only MSME vendors")
    return vf, notes


def _refusals(text: str) -> list[dict]:
    low = text.lower()
    out = []
    for pat, why in _LOOSEN:
        m = re.search(pat, low)
        if m and why not in [r["reason"] for r in out]:
            out.append({"phrase": text[m.start():m.end()], "reason": why})
    return out


def _gemini(text: str) -> dict | None:
    """Optional: ask Gemini for the same structure. Returns None on any problem."""
    if not config.GEMINI_API_KEY or os.environ.get("REQUEST_LLM", "on").lower() == "off":
        return None
    try:
        import httpx
        prompt = ("You read a finance head's request to an accounts-payable desk. Return JSON only: "
                  '{"spend_cap_rupees": number|null, "only_vendors": [names], "exclude_vendors": [names], "msme_only": bool, '
                  '"loosens_rules": bool, "loosen_phrase": string|null, "vague": bool}. '
                  "loosens_rules is true if it asks to skip checks, pay regardless, release held/blocked invoices, skip approval, "
                  "change bank details or spend the safety buffer. vague is true if it doesn't say which invoices or what to do.\n\nREQUEST:\n")
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{config.LLM_MODEL}:generateContent"
        body = {"contents": [{"parts": [{"text": prompt + text}]}],
                "generationConfig": {"responseMimeType": "application/json", "temperature": 0}}
        r = httpx.post(url, headers={"x-goog-api-key": config.GEMINI_API_KEY}, json=body, timeout=8)
        r.raise_for_status()
        return json.loads(r.json()["candidates"][0]["content"]["parts"][0]["text"])
    except Exception:  # noqa: BLE001
        return None


def parse_request(conn, text: str | None) -> dict:
    text = (text or "").strip()
    job = {"text": text, "scope": "This week's documents in the accounts inbox", "actions": list(BASE_ACTIONS),
           "spend_cap": None, "vendor_filter": {"include_ids": [], "exclude_ids": [], "msme_only": False},
           "approval_required": True, "status": "ok", "question": None, "refusals": [], "understood": [], "method": "keyword"}
    if not text:
        job.update(status="needs_clarification", question=CLARIFY_QUESTION)
        return job

    job["refusals"] = _refusals(text)
    caps = _spend_caps(text)
    vf, notes = _vendor_filter(conn, text)
    llm = _gemini(text)
    if llm:
        job["method"] = "gemini + keyword rules"
        if llm.get("loosens_rules") and not job["refusals"]:
            job["refusals"].append({"phrase": llm.get("loosen_phrase") or text,
                                    "reason": "This asks the desk to relax a check. A request can tighten the rules, never loosen them."})
        try:
            if llm.get("spend_cap_rupees"):
                caps.append(int(Decimal(str(llm["spend_cap_rupees"])) * 100))
        except Exception:  # noqa: BLE001
            pass
        if llm.get("msme_only") and not vf["msme_only"]:
            vf["msme_only"] = True
            notes.append("Only MSME vendors")

    if caps:
        job["spend_cap"] = min(caps)  # several limits -> the tightest one wins
        job["understood"].append(f"Spend at most {inr(job['spend_cap'])} in this run")
        job["actions"].insert(3, f"Keep this run's payments within {inr(job['spend_cap'])}")
    job["vendor_filter"] = vf
    job["understood"] += notes
    if vf["include_ids"] or vf["exclude_ids"] or vf["msme_only"]:
        job["scope"] += " · " + "; ".join(notes)

    if job["refusals"]:
        job["status"] = "refused"
        return job

    words = re.findall(r"\w+", text.lower())
    has_scope = re.search(_SCOPE_WORDS, text.lower()) or caps or notes
    if (not has_scope and len(words) < 12) or (llm and llm.get("vague") and not caps and not notes and len(words) < 12):
        job.update(status="needs_clarification", question=CLARIFY_QUESTION)
    return job


def plan_kwargs(job: dict) -> dict:
    """What a job description passes to scheduler.plan / propose_run."""
    return {"spend_cap": job.get("spend_cap"), "vendor_filter": job.get("vendor_filter")}
