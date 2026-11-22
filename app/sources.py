"""External sources: Crustdata + IFSC, behind one wrapper that enforces:

- run ceilings (time + credits) checked BEFORE every call
- fault injection (checked BEFORE the cache, so a cached answer can't mask it)
- cache per key (each vendor costs credits once)
- retry x2 with backoff, then SourceUnavailable (never read silence as "no problem")
- every call -> `calls` row; every response -> `raw_responses`

Mode per source: "mock" serves fixtures from data/mock_sources.json shaped like
the real API responses; "live" calls the real API and appends each response to
data/live_recordings.json; "replay" serves those recorded live responses with
their original retrieved_at (labelled live + recorded, never _mock). Facts record which mode was
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
    recorded: bool = False     # replayed from data/live_recordings.json


@dataclass
class RunCtx:
    run_id: str | None = None
    started: float = field(default_factory=time.monotonic)
    invoice_id: str | None = None


_MOCK = None
_REC = None
RECORDINGS = config.DATA / "live_recordings.json"
# Recording hooks: a function that shrinks a response before it is written to the
# committed recordings file (privacy: person search keeps titles only).
REDACT: dict[str, object] = {}


def _recordings() -> dict:
    global _REC
    if _REC is None:
        _REC = json.loads(RECORDINGS.read_text(encoding="utf-8")) if RECORDINGS.exists() else {}
    return _REC


def _record(cache_key: str, endpoint: str, request: dict, status: int, data, credits: float, ts: str, tool: str):
    rec = _recordings()
    red = REDACT.get(tool)
    rec[cache_key] = {"endpoint": endpoint, "request": request, "status": status,
                      "response": red(data) if red else data, "retrieved_at": ts, "credits": credits}
    RECORDINGS.write_text(json.dumps(dict(sorted(rec.items())), indent=1, ensure_ascii=False), encoding="utf-8")


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
          request: dict, mock_fn, live_fn, mode: str, credits_fn=lambda d: 0.0, tool_key: str | None = None) -> Result:
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
        return Result(v.get("data"), v.get("raw_response_id"), hit["retrieved_at"], ref, mode, cached=True,
                      recorded=bool(v.get("recorded")))
    # 2b. replay a recorded live response (original retrieved_at, no credits spent)
    if mode == "replay":
        rec = _recordings().get(cache_key)
        if not rec:
            record_call(conn, tool=tool, endpoint=endpoint + " (replay)", run_id=ctx.run_id, invoice_id=ctx.invoice_id,
                        ok=False, error="no recording", mode=mode)
            raise SourceUnavailable(source, "no recorded live response for this lookup (run scripts/record_live.py)")
        status, data = rec["status"], rec["response"]
        raw_id = store_raw(conn, endpoint + " (recorded)", request, data, status)
        record_call(conn, tool=tool, endpoint=endpoint + " (replay)", run_id=ctx.run_id, invoice_id=ctx.invoice_id,
                    ok=status in (200, 404), http_status=status, mode=mode)
        if status not in (200, 404):
            raise SourceUnavailable(source, f"recorded response was HTTP {status}")
        payload = data if status == 200 else None
        conn.execute("INSERT OR REPLACE INTO cache(key,value,retrieved_at) VALUES (?,?,?)",
                     (cache_key, j({"data": payload, "raw_response_id": raw_id, "status": status, "recorded": True}),
                      rec["retrieved_at"]))
        return Result(payload, raw_id, rec["retrieved_at"], ref + f" (recorded {rec['retrieved_at'][:16]})",
                      mode, recorded=True)
    # 3. call with retry
    last_err = None
    for attempt in range(3):
        t0 = time.monotonic()
        try:
            status, data = (mock_fn() if mode == "mock" else live_fn())   # replay never reaches here
            latency = int((time.monotonic() - t0) * 1000)
            credits = credits_fn(data) if status == 200 else 0.0
            raw_id = store_raw(conn, endpoint, request, data, status)
            record_call(conn, tool=tool, endpoint=endpoint, run_id=ctx.run_id, invoice_id=ctx.invoice_id,
                        credits=credits, est_cost_inr=credits * config.CRUSTDATA_INR_PER_CREDIT,
                        latency_ms=latency, ok=status in (200, 404), http_status=status, mode=mode)
            if status == 402:   # out of credits: never retry
                raise SourceUnavailable(source, "credits exhausted (402)")
            if status in (401, 403):   # fail fast, no retry
                raise AuthError(source, f"not authorised ({status}): check CRUSTDATA_API_KEY")
            if status == 429:   # live: _crust_post already waited for x-ratelimit-reset
                last_err = "rate limited (429)"
                continue
            if status >= 500:
                last_err = f"server error {status}"
                time.sleep(0.5 * (attempt + 1))
                continue
            payload = data if status == 200 else None   # 404 = an explicit "not found" answer
            ts = now_iso()
            if mode == "live":
                _record(cache_key, endpoint, request, status, data, credits, ts, tool_key or tool)
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
# Every path / body / response field below is taken from docs.crustdata.com
# (API version 2025-11-01). See NOTES.md for where the docs differed from our
# first guesses. Live credits come from the X-Credits-Used response header; the
# pricing rules are only used to estimate mock calls.
_LAST_CREDITS: dict[str, float] = {}
ENRICH_FIELDS = ["basic_info", "headcount", "locations", "news"]
ENRICH_PATH = "[0].matches[0].company_data"


class AuthError(SourceUnavailable):
    pass


def _crust_headers():
    return {"authorization": f"Bearer {config.CRUSTDATA_API_KEY}",
            "x-api-version": config.CRUSTDATA_API_VERSION, "content-type": "application/json"}


def _crust_post(path: str, body: dict):
    import httpx
    if not config.CRUSTDATA_API_KEY:
        raise AuthError("crustdata", "CRUSTDATA_API_KEY not set")
    r = httpx.post(config.CRUSTDATA_BASE + path, headers=_crust_headers(), json=body, timeout=30)
    try:
        _LAST_CREDITS[path] = float(r.headers.get("x-credits-used", "0") or 0)
    except ValueError:
        _LAST_CREDITS[path] = 0.0
    if r.status_code == 429:  # wait for the rate-limit window before _call retries
        reset = r.headers.get("x-ratelimit-reset")
        try:
            wait = float(reset)
            wait = wait - time.time() if wait > 1e9 else wait   # epoch seconds or seconds-to-wait
        except (TypeError, ValueError):
            wait = 5.0
        time.sleep(max(1.0, min(wait, 60.0)))
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, r.text


def _credits(path: str, mode: str, estimate: float) -> float:
    return _LAST_CREDITS.pop(path, estimate) if mode == "live" else estimate


def _best_match(resp):
    """identify/enrich both return [{matched_on, match_type, matches:[{confidence_score, company_data}]}]."""
    if isinstance(resp, list) and resp and isinstance(resp[0], dict) and resp[0].get("matches"):
        return resp[0]["matches"][0]
    return None


def company_identify(conn, ctx: RunCtx, *, domain: str | None = None, name: str | None = None) -> Result:
    """POST /company/identify. Returns the best match {confidence_score, company_data}, or None.

    company_data has only crustdata_company_id, basic_info and social_profiles (no headcount)."""
    key_val = domain or name
    body = {"domains": [domain]} if domain else {"names": [name]}
    mode = config.CRUSTDATA_MODE

    def mock_fn():
        m = _mock().get("identify", {})
        hit = m.get("domain", {}).get(domain) if domain else m.get("name", {}).get((name or "").lower())
        return 200, [{"matched_on": key_val, "match_type": "domain" if domain else "name",
                      "matches": [hit] if hit else []}]

    res = _call(conn, ctx, source="crustdata", tool="crustdata", endpoint="POST /company/identify",
                cache_key=f"crust:identify:{key_val}", request=body, mock_fn=mock_fn,
                live_fn=lambda: _crust_post("/company/identify", body), mode=mode,
                credits_fn=lambda d: _credits("/company/identify", mode, 0.2 if _best_match(d) else 0.0))
    res.data = _best_match(res.data)
    return res


def company_enrich(conn, ctx: RunCtx, company_id: int) -> Result:
    """POST /company/enrich with crustdata_company_ids + fields. Returns company_data or None.

    Fields read: basic_info.{name,year_founded,primary_domain}, headcount.{total,growth_percent.yoy},
    locations.{headquarters,state,country}, news[].{article_title,article_url,article_publish_date}."""
    body = {"crustdata_company_ids": [company_id], "fields": ENRICH_FIELDS}
    mode = config.CRUSTDATA_MODE

    def mock_fn():
        d = _mock().get("enrich", {}).get(str(company_id))
        return 200, [{"matched_on": str(company_id), "match_type": "crustdata_company_id",
                      "matches": [{"confidence_score": 1, "company_data": d}] if d else []}]

    res = _call(conn, ctx, source="crustdata", tool="crustdata", endpoint="POST /company/enrich",
                cache_key=f"crust:enrich:{company_id}", request=body, mock_fn=mock_fn,
                live_fn=lambda: _crust_post("/company/enrich", body), mode=mode,
                credits_fn=lambda d: _credits("/company/enrich", mode, 1.0 if _best_match(d) else 0.0))
    m = _best_match(res.data)
    res.data = m["company_data"] if m else None
    return res


PERSON_FIELDS = ["basic_profile.name", "basic_profile.current_title",
                 "experience.employment_details.current.title", "experience.employment_details.current.company_id"]


def person_search(conn, ctx: RunCtx, company_id: int, person_name: str | None = None,
                  title: str | None = None, limit: int = 5) -> Result:
    """POST /person/search: current employees of company_id, by name (fuzzy '(.)') and/or title."""
    conds = [{"field": "experience.employment_details.current.company_id", "type": "=", "value": company_id}]
    if person_name:
        conds.append({"field": "basic_profile.name", "type": "(.)", "value": person_name})
    if title:
        conds.append({"field": "experience.employment_details.current.title", "type": "(.)", "value": title})
    body = {"filters": {"op": "and", "conditions": conds}, "fields": PERSON_FIELDS, "limit": limit}
    mode = config.CRUSTDATA_MODE

    def mock_fn():
        people = _mock().get("people", {}).get(str(company_id), [])
        hits = [p for p in people
                if (not person_name or p["basic_profile"]["name"].lower() == person_name.lower())
                and (not title or any(w in p["basic_profile"]["current_title"].lower() for w in title.lower().split("|")))]
        return 200, {"profiles": hits, "total_count": len(hits), "next_cursor": None}

    return _call(conn, ctx, source="crustdata", tool="crustdata", endpoint="POST /person/search", tool_key="person_search",
                 cache_key=f"crust:person:{company_id}:{(person_name or '').lower()}:{(title or '').lower()}", request=body,
                 mock_fn=mock_fn, live_fn=lambda: _crust_post("/person/search", body), mode=mode,
                 # docs pricing: 0.03/result + 0.1 experience filter + 0.1 experience received
                 credits_fn=lambda d: _credits("/person/search", mode,
                                               0.23 * len(d.get("profiles", [])) if isinstance(d, dict) else 0.0))


def person_titles(resp) -> list[dict]:
    """Privacy-reduced view of a person-search response: titles + company ids. No names, no URLs."""
    if isinstance(resp, dict) and "matches" in resp and "profiles" not in resp:
        return list(resp["matches"])          # already reduced (replay of a recording)
    out = []
    for p in (resp or {}).get("profiles", []) or []:
        cur = (((p.get("experience") or {}).get("employment_details") or {}).get("current") or [])
        cur = cur if isinstance(cur, list) else [cur]
        out.append({"title": (p.get("basic_profile") or {}).get("current_title") or (cur[0].get("title") if cur else None),
                    "current_company_id": next((c.get("company_id") for c in cur if c.get("company_id")), None)})
    return out


REDACT["person_search"] = lambda d: ({"count": len(person_titles(d)), "matches": person_titles(d)}
                                     if isinstance(d, dict) else d)

NEWS_TERMS = ["insolvency", "nclt", "cirp", "liquidation", "resolution professional", "default", "winding up", "fraud"]


def adverse_news(conn, ctx: RunCtx, company_name: str) -> Result:
    """POST /web/search/live, news source, India. Results: [{source,title,url,snippet,position}] (no date field)."""
    query = f'"{company_name}" insolvency OR NCLT OR fraud OR default OR "winding up"'
    body = {"query": query, "location": "IN", "sources": ["news"]}
    mode = config.CRUSTDATA_MODE

    def mock_fn():
        return 200, {"success": True, "query": query,
                     "results": _mock().get("news", {}).get(company_name.lower(), []), "metadata": {}}

    return _call(conn, ctx, source="crustdata", tool="crustdata", endpoint="POST /web/search/live",
                 cache_key=f"crust:news:{company_name.lower()}", request=body, mock_fn=mock_fn,
                 live_fn=lambda: _crust_post("/web/search/live", body), mode=mode,
                 credits_fn=lambda d: _credits("/web/search/live", mode, 1.0))   # 1 credit per page


def news_hits(items: list[dict], title_key="title", text_keys=("snippet",)) -> list[dict]:
    """Keep only items whose title/snippet mention an insolvency/fraud term."""
    out = []
    for h in items or []:
        text = " ".join(str(h.get(k) or "") for k in (title_key, *text_keys)).lower()
        if any(t in text for t in NEWS_TERMS):
            out.append(h)
    return out


def source_type(base: str, res: Result) -> str:
    """'live' and 'replay' are both real API data (replay = recorded earlier, dated); only mock is _mock."""
    return base if res.mode in ("live", "replay") else f"{base}_mock"


def account_credits() -> float | None:
    """GET /account/credits (free). None if no key / not reachable."""
    import httpx
    if not config.CRUSTDATA_API_KEY:
        return None
    try:
        r = httpx.get(config.CRUSTDATA_BASE + "/account/credits", headers=_crust_headers(), timeout=15)
        return float(r.json()["account"]["credits"]) if r.status_code == 200 else None
    except Exception:
        return None
