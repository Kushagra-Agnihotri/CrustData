"""Two SEPARATE scores (BUILD_PROMPT §9).

Trust (0-1)    = "is it safe to pay at all?"  -> a GATE (< 0.4 => HOLD)
Priority (0-1) = "who first?"                 -> an ORDER

A missing signal contributes 0 and is listed as "can't verify" — never imputed.
"""
from __future__ import annotations

import datetime as dt

from app import config, sources
from app.db import j, q, q1, unj
from app.ledger import add_fact
from app.validators import gstin_checksum_ok, gstin_format_ok

TRUST_WEIGHTS = {"identity": 0.25, "company_age": 0.15, "headcount_stability": 0.15,
                 "domain_consistency": 0.15, "bank_stability": 0.15, "no_adverse_news": 0.15}
PRIORITY_WEIGHTS = {"critical_supplier": 0.25, "relationship_years": 0.15, "clean_match_rate": 0.20,
                    "on_time_delivery": 0.20, "low_dispute_rate": 0.10, "msme_small_supplier": 0.10}


def _sig(signal, value, weight, note, fact_ids=None, verified=True, unavailable=False):
    r = {"signal": signal, "value": round(value, 3), "weight": weight,
         "contribution": round(value * weight, 3), "note": note,
         "fact_ids": fact_ids or [], "verified": verified}
    if unavailable:  # a source outage, not an answer: the score is provisional and is recomputed next time
        r["unavailable"] = True
    return r


def compute_trust(conn, ctx: sources.RunCtx, vendor: dict) -> tuple[float, list[dict]]:
    vid = vendor["id"]
    rows: list[dict] = []
    # identity — GSTIN checksum + PAN consistency + Crustdata identify by official domain
    identity_ok = bool(vendor["gstin"]) and gstin_format_ok(vendor["gstin"]) and gstin_checksum_ok(vendor["gstin"]) \
        and vendor["gstin"][2:12] == (vendor["pan"] or "")
    company = None
    fid_ident = []
    try:
        res = sources.company_identify(conn, ctx, domain=vendor["domain"])
        if res.data:
            company = res.data["company_data"]
            fid_ident.append(add_fact(conn, "vendor", vid, "crustdata_company_id", company["crustdata_company_id"],
                                      sources.source_type("crustdata", res), res.source_ref,
                                      "[0].matches[0].company_data.crustdata_company_id", res.retrieved_at,
                                      res.raw_response_id))
            conn.execute("UPDATE vendors SET crustdata_company_id=? WHERE id=?", (company["crustdata_company_id"], vid))
        else:
            fid_ident.append(add_fact(conn, "vendor", vid, "crustdata_identify", "no match",
                                      sources.source_type("crustdata", res), res.source_ref, "[0].matches",
                                      res.retrieved_at, res.raw_response_id))
    except sources.SourceUnavailable as e:
        rows.append(_sig("identity", 0, TRUST_WEIGHTS["identity"], f"can't verify: {e}", verified=False, unavailable=True))
    if not any(r["signal"] == "identity" for r in rows):
        v = 1.0 if (identity_ok and company) else (0.5 if identity_ok or company else 0.0)
        note = ("GSTIN checksum ✓, PAN ✓, company found in Crustdata" if v == 1 else
                "can't verify: company not found in Crustdata" if identity_ok else "GSTIN/PAN inconsistent")
        rows.append(_sig("identity", v, TRUST_WEIGHTS["identity"], note, fid_ident, verified=v > 0))

    enrich = None
    if company:
        try:
            er = sources.company_enrich(conn, ctx, company["crustdata_company_id"])
            enrich = er.data
            if enrich:
                P = sources.ENRICH_PATH
                bi = enrich.get("basic_info") or {}
                hc = enrich.get("headcount") or {}
                yf = bi.get("year_founded")
                total = hc.get("total")
                yoy = (hc.get("growth_percent") or {}).get("yoy")
                st = sources.source_type("crustdata", er)
                f_age = add_fact(conn, "vendor", vid, "year_founded", yf, st,
                                 er.source_ref, f"{P}.basic_info.year_founded", er.retrieved_at, er.raw_response_id)
                f_hc = add_fact(conn, "vendor", vid, "headcount", {"total": total, "growth_percent_yoy": yoy}, st,
                                er.source_ref, f"{P}.headcount.total + headcount.growth_percent.yoy", er.retrieved_at, er.raw_response_id)
                f_dom = add_fact(conn, "vendor", vid, "official_domain", bi.get("primary_domain"),
                                 st, er.source_ref, f"{P}.basic_info.primary_domain",
                                 er.retrieved_at, er.raw_response_id)
                loc = enrich.get("locations") or {}
                if loc.get("headquarters") or loc.get("state"):
                    add_fact(conn, "vendor", vid, "hq_location", {k: loc.get(k) for k in ("headquarters", "state", "country")},
                             st, er.source_ref, f"{P}.locations.headquarters", er.retrieved_at, er.raw_response_id)
                if yf:
                    age = config.RUN_DATE.year - int(yf)
                    rows.append(_sig("company_age", min(1.0, max(0.0, age / 10)), TRUST_WEIGHTS["company_age"],
                                     f"founded {yf} ({age} years)", [f_age]))
                if yoy is not None:
                    chg = float(yoy) / 100
                    val = 1.0 if chg >= 0 else max(0.0, 1 + chg / 0.3)
                    rows.append(_sig("headcount_stability", val, TRUST_WEIGHTS["headcount_stability"],
                                     f"headcount {total} ({chg:+.0%} in 12 mo)", [f_hc]))
                off = (bi.get("primary_domain") or "").lower()
                same = off == (vendor["domain"] or "").lower()
                rows.append(_sig("domain_consistency", 1.0 if same else 0.0, TRUST_WEIGHTS["domain_consistency"],
                                 f"vendor master domain {vendor['domain']} {'==' if same else '≠'} official {off}", [f_dom]))
        except sources.SourceUnavailable as e:
            rows.append(_sig("company_age", 0, TRUST_WEIGHTS["company_age"], f"can't verify: {e}", verified=False, unavailable=True))
    # bank stability (internal record)
    cur = q1(conn, "SELECT * FROM vendor_bank_accounts WHERE vendor_id=? AND status='current'", (vid,))
    pend = q1(conn, "SELECT COUNT(*) n FROM vendor_bank_accounts WHERE vendor_id=? AND status IN ('pending_verification','rejected')", (vid,))["n"]
    if cur:
        months = (config.RUN_DATE - dt.date.fromisoformat(cur["valid_from"][:10])).days / 30.4
        val = 0.0 if pend else min(1.0, months / 6)
        f_b = add_fact(conn, "vendor", vid, "bank_account_since", cur["valid_from"][:10], "internal",
                       "vendor_bank_accounts", f"id={cur['id']}.valid_from")
        note = (f"{pend} unverified bank-change request(s)" if pend else f"unchanged since {cur['valid_from'][:10]}")
        rows.append(_sig("bank_stability", val, TRUST_WEIGHTS["bank_stability"], note, [f_b]))
    # adverse news
    try:
        nr = sources.adverse_news(conn, ctx, vendor["legal_name"])
        hits = sources.news_hits((nr.data or {}).get("results", []))
        f_n = add_fact(conn, "vendor", vid, "adverse_news", [{k: h.get(k) for k in ("title", "url")} for h in hits],
                       sources.source_type("crustdata_web", nr), nr.source_ref, "results[].{title,url,snippet}",
                       nr.retrieved_at, nr.raw_response_id)
        note = "no adverse news found" if not hits else "; ".join(h.get("title", "") for h in hits[:3])
        rows.append(_sig("no_adverse_news", 0.0 if hits else 1.0, TRUST_WEIGHTS["no_adverse_news"], note, [f_n]))
    except sources.SourceUnavailable as e:
        rows.append(_sig("no_adverse_news", 0, TRUST_WEIGHTS["no_adverse_news"], f"can't verify: {e}", verified=False, unavailable=True))
    # fill in signals we could not compute
    have = {r["signal"] for r in rows}
    for s, w in TRUST_WEIGHTS.items():
        if s not in have:
            rows.append(_sig(s, 0, w, "can't verify: no record", verified=False))
    rows.sort(key=lambda r: list(TRUST_WEIGHTS).index(r["signal"]))
    score = round(sum(r["contribution"] for r in rows), 3)
    conn.execute("UPDATE vendors SET trust_score=?, trust_breakdown=? WHERE id=?", (score, j(rows), vid))
    return score, rows


def compute_priority(conn, vendor: dict) -> tuple[float, list[dict]]:
    vid = vendor["id"]
    src = lambda field: add_fact(conn, "vendor", vid, field, vendor.get(field), "internal", "vendors (vendor master)", field)
    rows = []
    rows.append(_sig("critical_supplier", 1.0 if vendor["is_critical"] else 0.0, PRIORITY_WEIGHTS["critical_supplier"],
                     "critical / sole supplier" if vendor["is_critical"] else "not critical", [src("is_critical")]))
    yrs = 0.0
    if vendor["relationship_since"]:
        yrs = (config.RUN_DATE - dt.date.fromisoformat(vendor["relationship_since"])).days / 365.25
    rows.append(_sig("relationship_years", min(1.0, yrs / 5), PRIORITY_WEIGHTS["relationship_years"],
                     f"supplier for {yrs:.1f} years", [src("relationship_since")]))
    n = vendor["hist_invoices"] or 0
    if n:
        rows.append(_sig("clean_match_rate", vendor["hist_clean"] / n, PRIORITY_WEIGHTS["clean_match_rate"],
                         f"{vendor['hist_clean']}/{n} past invoices matched cleanly", [src("hist_clean")]))
        rows.append(_sig("low_dispute_rate", max(0.0, 1 - 5 * vendor["hist_disputes"] / n), PRIORITY_WEIGHTS["low_dispute_rate"],
                         f"{vendor['hist_disputes']} disputes in {n} invoices", [src("hist_disputes")]))
    else:
        rows.append(_sig("clean_match_rate", 0, PRIORITY_WEIGHTS["clean_match_rate"], "no history", verified=False))
        rows.append(_sig("low_dispute_rate", 0, PRIORITY_WEIGHTS["low_dispute_rate"], "no history", verified=False))
    d = vendor["hist_deliveries"] or 0
    rows.append(_sig("on_time_delivery", (vendor["hist_on_time"] / d) if d else 0, PRIORITY_WEIGHTS["on_time_delivery"],
                     f"{vendor['hist_on_time']}/{d} deliveries on time" if d else "no history", [src("hist_on_time")], verified=bool(d)))
    msme = vendor["msme_category"] in ("micro", "small") and not vendor["is_trader"]
    rows.append(_sig("msme_small_supplier", 1.0 if msme else 0.0, PRIORITY_WEIGHTS["msme_small_supplier"],
                     f"MSME {vendor['msme_category']}{' (trader)' if vendor['is_trader'] else ''}", [src("msme_category")]))
    score = round(sum(r["contribution"] for r in rows), 3)
    conn.execute("UPDATE vendors SET priority_score=?, priority_breakdown=? WHERE id=?", (score, j(rows), vid))
    return score, rows


def ensure_scores(conn, ctx, vendor_id: str) -> dict:
    v = q1(conn, "SELECT * FROM vendors WHERE id=?", (vendor_id,))
    provisional = any(r.get("unavailable") for r in unj(v["trust_breakdown"], []))
    if v["trust_score"] is None or provisional:
        compute_trust(conn, ctx, v)
    if v["priority_score"] is None:
        compute_priority(conn, v)
    return q1(conn, "SELECT * FROM vendors WHERE id=?", (vendor_id,))


def rescore_all(conn, ctx) -> None:
    for v in q(conn, "SELECT * FROM vendors"):
        compute_trust(conn, ctx, v)
        compute_priority(conn, q1(conn, "SELECT * FROM vendors WHERE id=?", (v["id"],)))
