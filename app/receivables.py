"""Receivables forecast (thin): confidence a customer pays by the due date.

confidence = 0.7 × customer's past on-time rate + 0.3 × health (Crustdata headcount trend + adverse news)
Overdue receivables are never counted as expected cash.
"""
from __future__ import annotations

from app import scoring, sources
from app.db import j, q, tx
from app.ledger import add_src_fact


def forecast_all(conn, ctx=None) -> None:
    ctx = ctx or sources.RunCtx()
    for c in q(conn, "SELECT * FROM customers"):
        with tx(conn):
            breakdown, health, fids = [], 0.0, []
            try:
                idr = sources.company_identify(conn, ctx, domain=c["domain"])
                if idr.data:
                    cd = idr.data["company_data"]
                    conn.execute("UPDATE customers SET crustdata_company_id=? WHERE id=?", (cd["crustdata_company_id"], c["id"]))
                    er = sources.company_enrich(conn, ctx, cd["crustdata_company_id"])
                    yoy, field = scoring._yoy(er.data or {})
                    fids.append(add_src_fact(conn, "customer", c["id"], "headcount_yoy_pct", yoy, "crustdata", er,
                                             "[0].matches[0].company_data." + (field or "headcount")))
                    hc_score = 1.0 if (yoy or 0) >= 0 else max(0.0, 1 + yoy / 30)
                    qname = sources.news_name(c["legal_name"])
                    hits, nf, n = scoring.news_check(conn, ctx, "customer", c["id"], qname,
                                                     [c["legal_name"], qname, (cd.get("basic_info") or {}).get("name")], fetch_dates=False)
                    fids += nf
                    health = 0.5 * hc_score + 0.5 * (0.0 if hits else 1.0)
                    breakdown = [{"signal": "headcount trend", "value": round(hc_score, 2),
                                  "note": f"{yoy:+.1f}% in 12 months" if yoy is not None else "can't verify: no headcount series"},
                                 {"signal": "adverse news", "value": 0.0 if hits else 1.0,
                                  "note": "; ".join(h["title"] for h in hits) or f"none in {n} news results"}]
                else:
                    breakdown = [{"signal": "company", "value": 0, "note": "can't verify: not found in Crustdata"}]
            except sources.SourceUnavailable as e:
                breakdown = [{"signal": "health", "value": 0, "note": f"can't verify: {e}"}]
            conf = round(0.7 * (c["on_time_rate"] or 0) + 0.3 * health, 3)
            breakdown.insert(0, {"signal": "past on-time rate", "value": c["on_time_rate"], "note": f"{c['on_time_rate']:.0%} of past invoices paid on time"})
            conn.execute("UPDATE customers SET health_score=?, health_breakdown=? WHERE id=?", (health, j(breakdown), c["id"]))
            conn.execute("UPDATE receivables SET confidence=?, confidence_breakdown=? WHERE customer_id=?",
                         (conf, j({"confidence": conf, "breakdown": breakdown, "fact_ids": fids}), c["id"]))
