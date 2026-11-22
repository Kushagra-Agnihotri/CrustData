"""Two SEPARATE scores (BUILD_PROMPT §9).

Trust (0-1)    = "is it safe to pay at all?"  -> a GATE (< 0.4 => HOLD)
Priority (0-1) = "who first?"                 -> an ORDER

A missing signal contributes 0 and is listed as "can't verify" — never imputed.
"""
from __future__ import annotations

import datetime as dt
import re

from app import config, sources
from app.db import j, q, q1, unj
from app.ledger import add_fact, add_src_fact
from app.validators import gstin_checksum_ok, gstin_format_ok

TRUST_WEIGHTS = {"identity": 0.25, "company_age": 0.15, "headcount_stability": 0.15,
                 "domain_consistency": 0.15, "bank_stability": 0.15, "no_adverse_news": 0.15}
PRIORITY_WEIGHTS = {"critical_supplier": 0.25, "relationship_years": 0.15, "clean_match_rate": 0.20,
                    "on_time_delivery": 0.20, "low_dispute_rate": 0.10, "msme_small_supplier": 0.10}


def _sig(signal, value, weight, note, fact_ids=None, verified=True, **extra):
    r = {"signal": signal, "value": round(value, 3), "weight": weight,
         "contribution": round(value * weight, 3), "note": note,
         "fact_ids": fact_ids or [], "verified": verified}
    r.update(extra)
    return r


# A news result counts as ADVERSE only if it names the company AND uses an insolvency term.
# "NCLT" alone is not adverse (NCLT also approves mergers/demergers — Tata Communications,
# Maruti Suzuki, Thermax, Kirloskar Ferrous in the recorded data), and bare "default" is not
# used (it matched an unrelated "Thermax Group" in Bangladesh). See NOTES.md.
ADVERSE_TERMS = ["insolvency", "cirp", "corporate insolvency resolution", "liquidation", "liquidator",
                 "resolution professional", "winding up", "winding-up"]


def _news_norm(s: str) -> str:
    s = (s or "").lower().replace("’", "'").replace("‘", "'")
    return " " + re.sub(r"[^a-z0-9']+", " ", s).strip() + " "


def adverse_hits(results: list[dict], names: list[str]) -> list[dict]:
    keys = {_news_norm(n).strip() for n in names if n and len(n.strip()) >= 3}
    terms = [_news_norm(t).strip() for t in ADVERSE_TERMS]
    out = []
    for r in results or []:
        text = _news_norm(f"{r.get('title', '')} {r.get('snippet', '')}")
        name_hit = next((k for k in keys if f" {k} " in text), None)
        term = next((t for t in terms if f" {t} " in text), None)
        if name_hit and term:
            out.append({**r, "matched_name": name_hit, "matched_term": term})
    return out


def _yoy(enrich: dict):
    """Headcount change over 12 months, in %: headcount.growth_percent.yoy, else first→last of the timeseries."""
    hc = enrich.get("headcount") or {}
    yoy = (hc.get("growth_percent") or {}).get("yoy")
    if yoy is not None:
        return float(yoy), "headcount.growth_percent.yoy"
    ts = [p for p in hc.get("timeseries") or [] if p.get("employee_count")]
    if len(ts) >= 2:
        return (ts[-1]["employee_count"] / ts[0]["employee_count"] - 1) * 100, "headcount.timeseries[].employee_count"
    return None, None


def vendor_names(vendor: dict, company: dict | None = None) -> list[str]:
    aliases = unj(vendor.get("aliases"), [])
    names = [vendor["legal_name"], sources.news_name(vendor["legal_name"], aliases)] + aliases
    if company:
        names.append((company.get("basic_info") or {}).get("name"))
    return [n for n in names if n]


def news_check(conn, ctx, entity_type: str, entity_id: str, query_name: str, names: list[str], fetch_dates=True):
    """Live news search → adverse hits (name + insolvency term) → read the first direct article for its publish date.
    Returns (hits_with_dates, fact_ids, n_results)."""
    nr = sources.adverse_news(conn, ctx, query_name)
    results = (nr.data or {}).get("results", [])
    hits = adverse_hits(results, names)
    f_n = add_src_fact(conn, entity_type, entity_id, "news_search", {"query": query_name, "results": len(results), "adverse": len(hits)},
                       "crustdata_web", nr, "results[].title,snippet")
    fids, dated = [f_n], []
    for h in hits[:3]:
        art = {"title": h.get("title"), "url": h.get("url"), "matched_term": h.get("matched_term"), "published": None}
        src_res = nr
        direct = h.get("url", "").startswith("http") and "google.com/goto" not in h["url"]
        if fetch_dates and direct and not any(a["published"] for a in dated):
            try:
                fr = sources.web_fetch(conn, ctx, h["url"])
                if fr.data and fr.data.get("published"):
                    art.update(published=fr.data["published"], published_source=fr.data.get("published_source"))
                    src_res = fr
            except sources.SourceUnavailable:
                pass
        field = f"results[{(h.get('position') or 1) - 1}].title" + ("; page publish date" if art["published"] else "")
        art["fact_id"] = add_src_fact(conn, entity_type, entity_id, "adverse_news_article", art, "crustdata_web", src_res, field)
        fids.append(art["fact_id"])
        dated.append(art)
    return dated, fids, len(results)


def compute_trust(conn, ctx: sources.RunCtx, vendor: dict) -> tuple[float, list[dict]]:
    vid = vendor["id"]
    rows: list[dict] = []
    E = "[0].matches[0].company_data."
    # identity — GSTIN checksum + PAN consistency + Crustdata identify by official domain
    identity_ok = bool(vendor["gstin"]) and gstin_format_ok(vendor["gstin"]) and gstin_checksum_ok(vendor["gstin"]) \
        and vendor["gstin"][2:12] == (vendor["pan"] or "")
    company = None
    fid_ident = []
    try:
        res = sources.company_identify(conn, ctx, domain=vendor["domain"])
        if res.data:
            company = res.data["company_data"]
            fid_ident.append(add_src_fact(conn, "vendor", vid, "crustdata_company_id",
                                          {"id": company["crustdata_company_id"], "name": company["basic_info"].get("name")},
                                          "crustdata", res, E + "crustdata_company_id"))
            conn.execute("UPDATE vendors SET crustdata_company_id=? WHERE id=?", (company["crustdata_company_id"], vid))
        else:
            fid_ident.append(add_src_fact(conn, "vendor", vid, "crustdata_identify", "no match", "crustdata", res, "[0].matches"))
    except sources.SourceUnavailable as e:
        rows.append(_sig("identity", 0, TRUST_WEIGHTS["identity"], f"can't verify: {e}", verified=False, source_error=True))
    if not any(r["signal"] == "identity" for r in rows):
        v = 1.0 if (identity_ok and company) else (0.5 if identity_ok or company else 0.0)
        note = (f"GSTIN checksum ✓, PAN ✓, {vendor['domain']} → {company['basic_info'].get('name')} on Crustdata" if v == 1 else
                "can't verify: company not found in Crustdata" if identity_ok else "GSTIN/PAN inconsistent")
        rows.append(_sig("identity", v, TRUST_WEIGHTS["identity"], note, fid_ident, verified=v > 0))

    if company:
        try:
            er = sources.company_enrich(conn, ctx, company["crustdata_company_id"])
            enrich = er.data
            if enrich:
                bi = enrich.get("basic_info") or {}
                yf = bi.get("year_founded")
                yoy, yoy_field = _yoy(enrich)
                total = (enrich.get("headcount") or {}).get("total")
                f_age = add_src_fact(conn, "vendor", vid, "year_founded", yf, "crustdata", er, E + "basic_info.year_founded")
                f_hc = add_src_fact(conn, "vendor", vid, "headcount", {"total": total, "yoy_pct": yoy}, "crustdata", er,
                                    E + "headcount.total, " + E + (yoy_field or "headcount"))
                f_dom = add_src_fact(conn, "vendor", vid, "official_domain", bi.get("primary_domain"), "crustdata", er,
                                     E + "basic_info.primary_domain")
                loc = enrich.get("locations") or {}
                if loc.get("headquarters"):
                    add_src_fact(conn, "vendor", vid, "hq_location", loc.get("headquarters"), "crustdata", er, E + "locations.headquarters")
                if yf:
                    age = config.RUN_DATE.year - int(yf)
                    rows.append(_sig("company_age", min(1.0, max(0.0, age / 10)), TRUST_WEIGHTS["company_age"],
                                     f"founded {yf} ({age} years)", [f_age]))
                if yoy is not None:
                    val = 1.0 if yoy >= 0 else max(0.0, 1 + yoy / 30)
                    rows.append(_sig("headcount_stability", val, TRUST_WEIGHTS["headcount_stability"],
                                     f"headcount {total} ({yoy:+.1f}% in 12 months)", [f_hc]))
                off = (bi.get("primary_domain") or "").lower()
                same = off == (vendor["domain"] or "").lower()
                rows.append(_sig("domain_consistency", 1.0 if same else 0.0, TRUST_WEIGHTS["domain_consistency"],
                                 f"vendor master domain {vendor['domain']} {'==' if same else '≠'} official {off}", [f_dom]))
        except sources.SourceUnavailable as e:
            rows.append(_sig("company_age", 0, TRUST_WEIGHTS["company_age"], f"can't verify: {e}", verified=False, source_error=True))
    # bank stability (internal record)
    cur = q1(conn, "SELECT * FROM vendor_bank_accounts WHERE vendor_id=? AND status='current'", (vid,))
    pend = q1(conn, "SELECT COUNT(*) n FROM vendor_bank_accounts WHERE vendor_id=? AND status IN ('pending_verification','rejected')", (vid,))["n"]
    if cur:
        months = (config.RUN_DATE - dt.date.fromisoformat(cur["valid_from"][:10])).days / 30.4
        val = 0.0 if pend else min(1.0, max(0.0, months) / 6)
        f_b = add_fact(conn, "vendor", vid, "bank_account_since", cur["valid_from"][:10], "internal",
                       "vendor_bank_accounts", f"id={cur['id']}.valid_from")
        note = (f"{pend} unverified bank-change request(s)" if pend else f"unchanged since {cur['valid_from'][:10]}")
        rows.append(_sig("bank_stability", val, TRUST_WEIGHTS["bank_stability"], note, [f_b]))
    # adverse news — live web search, then read the article for its publish date
    try:
        qname = sources.news_name(vendor["legal_name"], unj(vendor.get("aliases"), []))
        dated, fids, n = news_check(conn, ctx, "vendor", vid, qname, vendor_names(vendor, company))
        note = (f"no adverse news ({n} news results checked for insolvency terms)" if not dated else
                "; ".join(f"{a['title']} ({(a['published'] or 'publish date n/a')[:10]})" for a in dated))
        rows.append(_sig("no_adverse_news", 0.0 if dated else 1.0, TRUST_WEIGHTS["no_adverse_news"], note, fids, articles=dated))
    except sources.SourceUnavailable as e:
        rows.append(_sig("no_adverse_news", 0, TRUST_WEIGHTS["no_adverse_news"], f"can't verify: {e}", verified=False, source_error=True))
    # fill in signals we could not compute
    have = {r["signal"] for r in rows}
    for s_, w in TRUST_WEIGHTS.items():
        if s_ not in have:
            rows.append(_sig(s_, 0, w, "can't verify: no record", verified=False))
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
    # A score computed while a source was down is provisional: recompute it on the next use.
    if v["trust_score"] is None or any(r.get("source_error") for r in unj(v["trust_breakdown"], [])):
        compute_trust(conn, ctx, v)
    if v["priority_score"] is None:
        compute_priority(conn, v)
    return q1(conn, "SELECT * FROM vendors WHERE id=?", (vendor_id,))


def rescore_all(conn, ctx) -> None:
    for v in q(conn, "SELECT * FROM vendors"):
        compute_trust(conn, ctx, v)
        compute_priority(conn, q1(conn, "SELECT * FROM vendors WHERE id=?", (v["id"],)))
