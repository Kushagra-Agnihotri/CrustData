"""External sources: Crustdata + IFSC, behind one wrapper that enforces:

- run ceilings (time + credits) checked BEFORE every call
- fault injection (checked BEFORE the cache, so a cached answer can't mask it)
- cache per key (each vendor costs credits once)
- retry x2 with backoff, then SourceUnavailable (never read silence as "no problem")
- every call -> `calls` row; every response -> `raw_responses`

Mode per source: "mock" serves fixtures from data/mock_sources.json shaped like
the real API responses; "live" calls the real API. Facts record which mode was
used (`source_type` = 'crustdata' vs 'crustdata_mock'), so the UI never passes a
fixture off as live data.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from app import config
from app.db import j, now_iso, q1, unj
from app.ledger import record_call, store_raw

FAULTS: set[str] = set()  # e.g. {"crustdata", "ifsc"} — used by the test runner


class SourceUnavailable(Exception):
    def __init__(self, source: str, detail: str):
        super().__init__(f"{source}: {detail}")
        self.source, self.detail = source, detail


class CeilingReached(SourceUnavailable):
    pass


@dataclass
class Result:
    data: object               # parsed payload (None = the source answered "no match")
    raw_response_id: str | None
    retrieved_at: str
    source_ref: str
    mode: str
    cached: bool = False
    credits: float = 0.0


@dataclass
class RunCtx:
    run_id: str | None = None
    started: float = field(default_factory=time.monotonic)
    invoice_id: str | None = None


_MOCK = None


def _mock() -> dict:
    global _MOCK
    if _MOCK is None:
        p = config.DATA / "mock_sources.json"
        _MOCK = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    return _MOCK


def _check_ceiling(conn, ctx: RunCtx):
    if not ctx.run_id:
        return
    run = q1(conn, "SELECT * FROM pipeline_runs WHERE id=?", (ctx.run_id,))
    if not run:
        return
    if time.monotonic() - ctx.started > (run["time_limit_s"] or config.RUN_TIME_LIMIT_S):
        raise CeilingReached("run", f"time limit {run['time_limit_s']}s reached")
    if run["credits_used"] >= (run["credit_limit"] or config.RUN_CREDIT_LIMIT):
        raise CeilingReached("run", f"credit limit {run['credit_limit']} reached")


def _call(conn, ctx: RunCtx, *, source: str, tool: str, endpoint: str, cache_key: str,
          request: dict, mock_fn, live_fn, mode: str, credits_fn=lambda d: 0.0) -> Result:
    _check_ceiling(conn, ctx)
    ref = f"{endpoint} {json.dumps(request, sort_keys=True)}"
    # 1. fault injection comes before the cache
    if source in FAULTS:
        for attempt in range(3):
            record_call(conn, tool=tool, endpoint=endpoint, run_id=ctx.run_id, invoice_id=ctx.invoice_id,
                        ok=False, error=f"timeout (injected) attempt {attempt + 1}", latency_ms=5000, mode=mode)
        raise SourceUnavailable(source, "timed out after 3 attempts")
    # 2. cache
    hit = q1(conn, "SELECT value, retrieved_at FROM cache WHERE key=?", (cache_key,))
    if hit:
        v = unj(hit["value"], {})
        record_call(conn, tool=tool, endpoint=endpoint + " (cache)", run_id=ctx.run_id,
                    invoice_id=ctx.invoice_id, ok=True, mode=mode)
        return Result(v.get("data"), v.get("raw_response_id"), hit["retrieved_at"], ref, mode, cached=True)
    # 3. call with retry
    last_err = None
    for attempt in range(3):
        t0 = time.monotonic()
        try:
            status, data = (mock_fn() if mode == "mock" else live_fn())
            latency = int((time.monotonic() - t0) * 1000)
            credits = credits_fn(data) if status == 200 else 0.0
            raw_id = store_raw(conn, endpoint, request, data, status)
            record_call(conn, tool=tool, endpoint=endpoint, run_id=ctx.run_id, invoice_id=ctx.invoice_id,
                        credits=credits, est_cost_inr=credits * config.CRUSTDATA_INR_PER_CREDIT,
                        latency_ms=latency, ok=status in (200, 404), http_status=status, mode=mode)
            if status == 402:
                raise SourceUnavailable(source, "credits exhausted (402)")
            if status in (401, 403):
                raise SourceUnavailable(source, f"not authorised ({status})")
            if status == 429:
                time.sleep(min(2 ** attempt, 5))
                last_err = "rate limited (429)"
                continue
            if status >= 500:
                last_err = f"server error {status}"
                time.sleep(0.5 * (attempt + 1))
                continue
            payload = data if status == 200 else None   # 404 = an explicit "not found" answer
            ts = now_iso()
            conn.execute("INSERT OR REPLACE INTO cache(key,value,retrieved_at) VALUES (?,?,?)",
                         (cache_key, j({"data": payload, "raw_response_id": raw_id, "status": status}), ts))
            return Result(payload, raw_id, ts, ref, mode, credits=credits)
        except SourceUnavailable:
            raise
        except Exception as e:  # timeouts, connection errors
            last_err = f"{type(e).__name__}: {e}"
            record_call(conn, tool=tool, endpoint=endpoint, run_id=ctx.run_id, invoice_id=ctx.invoice_id,
                        ok=False, error=last_err, latency_ms=int((time.monotonic() - t0) * 1000), mode=mode)
            time.sleep(0.5 * (attempt + 1))
    raise SourceUnavailable(source, last_err or "no response")


# --------------------------------------------------------------------------- IFSC
def ifsc_lookup(conn, ctx: RunCtx, ifsc: str) -> Result:
    def mock_fn():
        d = _mock().get("ifsc", {}).get(ifsc)
        return (200, d) if d else (404, "Not Found")

    def live_fn():
        import httpx
        r = httpx.get(f"https://ifsc.razorpay.com/{ifsc}", timeout=8)
        return r.status_code, (r.json() if r.status_code == 200 else r.text)

    return _call(conn, ctx, source="ifsc", tool="ifsc", endpoint=f"GET https://ifsc.razorpay.com/{ifsc}",
                 cache_key=f"ifsc:{ifsc}", request={"ifsc": ifsc}, mock_fn=mock_fn, live_fn=live_fn,
                 mode=config.IFSC_MODE)


# --------------------------------------------------------------------------- Crustdata
def _crust_headers():
    return {"authorization": f"Bearer {config.CRUSTDATA_API_KEY}",
            "x-api-version": config.CRUSTDATA_API_VERSION, "content-type": "application/json"}


def _crust_post(path: str, body: dict):
    import httpx
    if not config.CRUSTDATA_API_KEY:
        raise SourceUnavailable("crustdata", "CRUSTDATA_API_KEY not set")
    r = httpx.post(config.CRUSTDATA_BASE + path, headers=_crust_headers(), json=body, timeout=20)
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, r.text


def company_identify(conn, ctx: RunCtx, *, domain: str | None = None, name: str | None = None) -> Result:
    """POST /company/identify. Returns the best match's company_data, or None."""
    key_val = domain or name
    body = {"domains": [domain]} if domain else {"names": [name]}

    def pick(resp):
        # handbook shape: [{matched_on, match_type, matches:[{confidence_score, company_data:{...}}]}]
        if isinstance(resp, list) and resp and resp[0].get("matches"):
            return resp
        return []

    def mock_fn():
        m = _mock().get("identify", {})
        hit = m.get("domain", {}).get(domain) if domain else m.get("name", {}).get((name or "").lower())
        if not hit:
            return 200, [{"matched_on": key_val, "match_type": "domain" if domain else "name", "matches": []}]
        return 200, [{"matched_on": key_val, "match_type": "domain" if domain else "name",
                      "matches": [{"confidence_score": hit.get("confidence", 0.95), "company_data": hit["company_data"]}]}]

    def live_fn():
        return _crust_post("/company/identify", body)

    res = _call(conn, ctx, source="crustdata", tool="crustdata", endpoint="POST /company/identify",
                cache_key=f"crust:identify:{key_val}", request=body, mock_fn=mock_fn, live_fn=live_fn,
                mode=config.CRUSTDATA_MODE,
                credits_fn=lambda d: 0.2 if pick(d) else 0.0)   # 0.2 credits per matched identifier
    data = res.data
    best = None
    if isinstance(data, list) and data and data[0].get("matches"):
        best = data[0]["matches"][0]
    res.data = best
    return res


def company_enrich(conn, ctx: RunCtx, company_id: int) -> Result:
    """Company Enrich. Live path: verify the endpoint + field names in the docs (see NOTES.md)."""
    body = {"crustdata_company_ids": [company_id]}

    def mock_fn():
        d = _mock().get("enrich", {}).get(str(company_id))
        return (200, d) if d else (404, "Not Found")

    def live_fn():
        return _crust_post("/company/enrich", body)

    return _call(conn, ctx, source="crustdata", tool="crustdata", endpoint="POST /company/enrich",
                 cache_key=f"crust:enrich:{company_id}", request=body, mock_fn=mock_fn, live_fn=live_fn,
                 mode=config.CRUSTDATA_MODE, credits_fn=lambda d: 1.0)


def person_search(conn, ctx: RunCtx, company_id: int, person_name: str) -> Result:
    """People Search: is `person_name` a current employee of the vendor?"""
    body = {"filters": [{"field": "current_employer_id", "type": "=", "value": company_id},
                        {"field": "name", "type": "=", "value": person_name}], "limit": 5}

    def mock_fn():
        people = _mock().get("people", {}).get(str(company_id), [])
        hits = [p for p in people if p["name"].lower() == person_name.lower()]
        return 200, {"profiles": hits}

    def live_fn():
        return _crust_post("/person/search", body)

    return _call(conn, ctx, source="crustdata", tool="crustdata", endpoint="POST /person/search",
                 cache_key=f"crust:person:{company_id}:{person_name.lower()}", request=body,
                 mock_fn=mock_fn, live_fn=live_fn, mode=config.CRUSTDATA_MODE,
                 credits_fn=lambda d: 1.0 if (isinstance(d, dict) and d.get("profiles")) else 0.0)


def adverse_news(conn, ctx: RunCtx, company_name: str) -> Result:
    """Web Search for insolvency / fraud / NCLT / default news."""
    query = f'"{company_name}" (insolvency OR NCLT OR fraud OR default OR "winding up")'
    body = {"query": query, "limit": 5}

    def mock_fn():
        return 200, {"results": _mock().get("news", {}).get(company_name.lower(), [])}

    def live_fn():
        return _crust_post("/web/search", body)

    return _call(conn, ctx, source="crustdata", tool="crustdata", endpoint="POST /web/search",
                 cache_key=f"crust:news:{company_name.lower()}", request=body, mock_fn=mock_fn,
                 live_fn=live_fn, mode=config.CRUSTDATA_MODE, credits_fn=lambda d: 1.0)


def source_type(base: str, res: Result) -> str:
    return base if res.mode == "live" else f"{base}_mock"
