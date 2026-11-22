"""External sources: Crustdata + IFSC, behind one wrapper that enforces:

- run ceilings (time + credits) checked BEFORE every call
- fault injection (checked BEFORE the cache, so a cached answer can't mask it)
- cache per key (each vendor costs credits once)
- retry with backoff (429 waits for x-ratelimit-reset), then SourceUnavailable
  (never read silence as "no problem"); 401 and 402 fail fast, no retry
- every call -> `calls` row; every response -> `raw_responses`

Mode per source (CRUSTDATA_MODE / IFSC_MODE):
  live    call the real API, and append the response to data/live_recordings.json
  replay  serve data/live_recordings.json — REAL responses with their original
          retrieved_at; facts are labelled live (source_type 'crustdata') + recorded
  mock    offline synthetic fixtures (data/mock_sources.json); facts labelled *_mock

Endpoints, bodies and response fields follow docs.crustdata.com (api-version 2025-11-01).
See NOTES.md for every place the live API differed from our first assumptions.
"""
from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field

from app import config
from app.db import j, now_iso, q1, unj
from app.ledger import record_call, store_raw

FAULTS: set[str] = set()  # e.g. {"crustdata", "ifsc"} — used by the test runner
RECORDINGS = config.DATA / "live_recordings.json"
_LOCK = threading.Lock()


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
    recorded: bool = False     # True when served from data/live_recordings.json


@dataclass
class RunCtx:
    run_id: str | None = None
    started: float = field(default_factory=time.monotonic)
    invoice_id: str | None = None


_MOCK = None
_REC = None


def _mock() -> dict:
    global _MOCK
    if _MOCK is None:
        p = config.DATA / "mock_sources.json"
        _MOCK = json.loads(p.read_text(encoding="utf-8")).get("entries", {}) if p.exists() else {}
    return _MOCK


def recordings() -> dict:
    global _REC
    if _REC is None:
        _REC = json.loads(RECORDINGS.read_text(encoding="utf-8")) if RECORDINGS.exists() else {"entries": {}}
        _REC.setdefault("entries", {})
    return _REC


def _append_recording(key: str, entry: dict) -> None:
    """live mode: keep the response so the demo can replay it without Wi-Fi."""
    with _LOCK:
        doc = recordings()
        doc["entries"][key] = entry
        RECORDINGS.write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")


def reset_caches() -> None:
    global _MOCK, _REC
    _MOCK, _REC = None, None


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
          request: dict, live_fn, mode: str, credits_fn=lambda d: 0.0, reduce_fn=None) -> Result:
    """live_fn() -> (status, data, headers). reduce_fn(data) -> what may be written to the committed recordings file."""
    _check_ceiling(conn, ctx)
    ref = f"{endpoint} {json.dumps(request, sort_keys=True, ensure_ascii=False)}"
    # 1. fault injection comes before the cache (and before replay)
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

    def _store(status, data, credits, retrieved_at, recorded, latency=0, http=None):
        raw_id = store_raw(conn, endpoint, request, data, status)
        record_call(conn, tool=tool, endpoint=endpoint, run_id=ctx.run_id, invoice_id=ctx.invoice_id,
                    credits=credits, est_cost_inr=credits * config.CRUSTDATA_INR_PER_CREDIT if source != "ifsc" else 0.0,
                    latency_ms=latency, ok=status in (200, 404), http_status=http or status, mode=mode)
        payload = data if status == 200 else None   # 404 = an explicit "not found" answer
        conn.execute("INSERT OR REPLACE INTO cache(key,value,retrieved_at) VALUES (?,?,?)",
                     (cache_key, j({"data": payload, "raw_response_id": raw_id, "status": status, "recorded": recorded}), retrieved_at))
        return Result(payload, raw_id, retrieved_at, ref, mode, credits=credits, recorded=recorded)

    # 3a. offline fixtures / recordings
    if mode in ("mock", "replay"):
        book = _mock() if mode == "mock" else recordings()["entries"]
        e = book.get(cache_key)
        if e is None:
            record_call(conn, tool=tool, endpoint=endpoint, run_id=ctx.run_id, invoice_id=ctx.invoice_id, ok=False,
                        error=f"no {mode} entry for {cache_key}", mode=mode)
            raise SourceUnavailable(source, f"no {'recording' if mode == 'replay' else 'fixture'} for this lookup "
                                            f"({cache_key}); run scripts/record_live.py with a key")
        st = e.get("status", 200)
        if mode == "replay":
            return _store(st, e.get("response"), float(e.get("credits") or 0), e.get("retrieved_at") or now_iso(), True)
        return _store(st, e.get("response"), credits_fn(e.get("response")) if st == 200 else 0.0, now_iso(), False)
    # 3b. live, with retry
    last_err = None
    for attempt in range(3):
        t0 = time.monotonic()
        try:
            status, data, headers = live_fn()
        except SourceUnavailable:
            raise
        except Exception as e:  # timeouts, connection errors
            last_err = f"{type(e).__name__}: {e}"
            record_call(conn, tool=tool, endpoint=endpoint, run_id=ctx.run_id, invoice_id=ctx.invoice_id,
                        ok=False, error=last_err, latency_ms=int((time.monotonic() - t0) * 1000), mode=mode)
            time.sleep(0.5 * (attempt + 1))
            continue
        latency = int((time.monotonic() - t0) * 1000)
        hdr = {k.lower(): v for k, v in (headers or {}).items()}
        if status in (401, 402, 403, 429) or status >= 500:
            store_raw(conn, endpoint, request, data, status)
            record_call(conn, tool=tool, endpoint=endpoint, run_id=ctx.run_id, invoice_id=ctx.invoice_id, ok=False,
                        http_status=status, latency_ms=latency, error=str(data)[:200], mode=mode)
        if status == 401:
            raise SourceUnavailable(source, "API key rejected (401) — check CRUSTDATA_API_KEY in .env")
        if status == 402:
            raise SourceUnavailable(source, "out of credits (402) — not retried")
        if status == 403:
            raise SourceUnavailable(source, f"not on this plan (403): {str(data)[:120]}")
        if status == 429:
            wait = min(float(hdr.get("x-ratelimit-reset") or 2 ** attempt), 30.0)
            last_err = f"rate limited (429), waited {wait:.0f}s"
            time.sleep(wait)
            continue
        if status >= 500:
            last_err = f"server error {status}"
            time.sleep(0.5 * (attempt + 1))
            continue
        try:
            credits = float(hdr["x-credits-used"]) if "x-credits-used" in hdr else (credits_fn(data) if status == 200 else 0.0)
        except ValueError:
            credits = credits_fn(data) if status == 200 else 0.0
        res = _store(status, data, credits, now_iso(), False, latency)
        if config.RECORD_LIVE:
            _append_recording(cache_key, {"source": source, "endpoint": endpoint, "request": request, "status": status,
                                          "response": reduce_fn(data) if (reduce_fn and status == 200) else data,
                                          "retrieved_at": res.retrieved_at, "credits": credits})
        return res
    raise SourceUnavailable(source, last_err or "no response")


# --------------------------------------------------------------------------- IFSC
def ifsc_lookup(conn, ctx: RunCtx, ifsc: str) -> Result:
    def live_fn():
        import httpx
        r = httpx.get(f"https://ifsc.razorpay.com/{ifsc}", timeout=8)
        return r.status_code, (r.json() if r.status_code == 200 else r.text), dict(r.headers)

    return _call(conn, ctx, source="ifsc", tool="ifsc", endpoint=f"GET https://ifsc.razorpay.com/{ifsc}",
                 cache_key=f"ifsc:{ifsc}", request={"ifsc": ifsc}, live_fn=live_fn, mode=config.IFSC_MODE)


# --------------------------------------------------------------------------- Crustdata
def _crust_headers():
    return {"authorization": f"Bearer {config.CRUSTDATA_API_KEY}",
            "x-api-version": config.CRUSTDATA_API_VERSION, "content-type": "application/json"}


def _crust_post(path: str, body: dict):
    import httpx
    if not config.CRUSTDATA_API_KEY:
        raise SourceUnavailable("crustdata", "CRUSTDATA_API_KEY not set (use CRUSTDATA_MODE=replay to run offline)")
    r = httpx.post(config.CRUSTDATA_BASE + path, headers=_crust_headers(), json=body, timeout=30)
    try:
        return r.status_code, r.json(), dict(r.headers)
    except ValueError:
        return r.status_code, r.text, dict(r.headers)


def _first_match(resp):
    """identify/enrich both return [{matched_on, match_type, matches:[{confidence_score, company_data}]}]."""
    if isinstance(resp, list) and resp and isinstance(resp[0], dict) and resp[0].get("matches"):
        return resp[0]["matches"][0]
    return None


IDENTIFY_FIELDS = ["crustdata_company_id", "basic_info"]
ENRICH_FIELDS = ["crustdata_company_id", "basic_info.name", "basic_info.primary_domain", "basic_info.year_founded",
                 "basic_info.employee_count_range", "basic_info.company_type", "headcount.total", "headcount.timeseries",
                 "headcount.growth_percent", "locations.headquarters", "locations.state", "locations.country"]


def company_identify(conn, ctx: RunCtx, *, domain: str | None = None, name: str | None = None) -> Result:
    """POST /company/identify. res.data = best match {confidence_score, company_data:{crustdata_company_id, basic_info}} or None.
    Identify returns only crustdata_company_id, basic_info and social_profiles — headcount comes from enrich."""
    kind, val = ("domain", domain.lower()) if domain else ("name", name or "")
    body = {"domains": [val], "fields": IDENTIFY_FIELDS} if domain else {"names": [val], "fields": IDENTIFY_FIELDS}
    res = _call(conn, ctx, source="crustdata", tool="crustdata", endpoint="POST /company/identify",
                cache_key=f"crust:identify:{kind}:{val.lower()}", request=body,
                live_fn=lambda: _crust_post("/company/identify", body), mode=config.CRUSTDATA_MODE,
                credits_fn=lambda d: 0.2 * sum(1 for x in (d or []) if isinstance(x, dict) and x.get("matches")))
    res.data = _first_match(res.data)
    return res


def company_enrich(conn, ctx: RunCtx, company_id: int) -> Result:
    """POST /company/enrich by id (2 credits per returned match). res.data = company_data of the match, or None."""
    body = {"crustdata_company_ids": [company_id], "fields": ENRICH_FIELDS}
    res = _call(conn, ctx, source="crustdata", tool="crustdata", endpoint="POST /company/enrich",
                cache_key=f"crust:enrich:{company_id}", request=body,
                live_fn=lambda: _crust_post("/company/enrich", body), mode=config.CRUSTDATA_MODE,
                credits_fn=lambda d: 2.0 * sum(len(x.get("matches") or []) for x in (d or []) if isinstance(x, dict)))
    m = _first_match(res.data)
    res.data = m["company_data"] if m else None
    return res


PERSON_FIELDS = ["crustdata_person_id", "basic_profile.name", "basic_profile.current_title",
                 "experience.employment_details.current.crustdata_company_id", "experience.employment_details.current.title"]


def people_view(resp) -> dict:
    """Normalise a /person/search response (raw {profiles:[...]}, or the privacy-reduced recording
    {count, matches:[{title, current_company_id}]}) to {count, matches:[{title, current_company_id, name?}]}."""
    if not isinstance(resp, dict):
        return {"count": 0, "matches": []}
    if "matches" in resp:
        return {"count": resp.get("count", len(resp["matches"])), "matches": resp["matches"]}
    out = []
    for p in resp.get("profiles") or []:
        cur = ((p.get("experience") or {}).get("employment_details") or {}).get("current") or [{}]
        bp = p.get("basic_profile") or {}
        out.append({"title": bp.get("current_title") or cur[0].get("title"),
                    "current_company_id": cur[0].get("crustdata_company_id"), "name": bp.get("name")})
    return {"count": resp.get("total_count", len(out)), "matches": out}


def _private(resp) -> dict:
    """What may be committed: titles only — no names, no profile URLs."""
    v = people_view(resp)
    return {"count": v["count"], "matches": [{"title": m.get("title"), "current_company_id": m.get("current_company_id")} for m in v["matches"]]}


def _person_credits(d) -> float:
    return round(0.23 * len((d or {}).get("profiles") or []), 2) if isinstance(d, dict) else 0.0


def person_search(conn, ctx: RunCtx, company_id: int, person_name: str) -> Result:
    """POST /person/search: is `person_name` a current employee of the vendor? res.data = people_view(...)."""
    body = {"filters": {"op": "and", "conditions": [
        {"field": "experience.employment_details.current.company_id", "type": "=", "value": company_id},
        {"field": "basic_profile.name", "type": "=", "value": person_name}]}, "limit": 5, "fields": PERSON_FIELDS}
    res = _call(conn, ctx, source="crustdata", tool="crustdata", endpoint="POST /person/search",
                cache_key=f"crust:person:{company_id}:{person_name.lower()}", request=body,
                live_fn=lambda: _crust_post("/person/search", body), mode=config.CRUSTDATA_MODE,
                credits_fn=_person_credits, reduce_fn=_private)
    res.data = people_view(res.data)
    return res


def finance_contacts(conn, ctx: RunCtx, company_id: int) -> Result:
    """POST /person/search for finance/accounts titles at the vendor — someone ELSE to confirm a bank change with."""
    body = {"filters": {"op": "and", "conditions": [
        {"field": "experience.employment_details.current.company_id", "type": "=", "value": company_id},
        {"op": "or", "conditions": [{"field": "experience.employment_details.current.title", "type": "(.)", "value": t}
                                    for t in ("finance", "accounts", "CFO")]}]}, "limit": 3, "fields": PERSON_FIELDS}
    res = _call(conn, ctx, source="crustdata", tool="crustdata", endpoint="POST /person/search",
                cache_key=f"crust:finance_contacts:{company_id}", request=body,
                live_fn=lambda: _crust_post("/person/search", body), mode=config.CRUSTDATA_MODE,
                credits_fn=_person_credits, reduce_fn=_private)
    res.data = people_view(res.data)
    return res


NEWS_TERMS = ' insolvency OR NCLT OR CIRP OR liquidation OR "resolution professional" OR default'


def news_name(legal_name: str, aliases: list | None = None) -> str:
    """Name used in the news query: the first alias, else the legal name without its suffix."""
    if aliases:
        return aliases[0]
    return re.sub(r"\s+(pvt\.?\s+ltd\.?|private\s+limited|ltd\.?|limited)$", "", legal_name or "", flags=re.I)


def adverse_news(conn, ctx: RunCtx, name: str) -> Result:
    """POST /web/search/live, sources=[news] (1 credit per page; 0 when nothing is found). res.data = {results:[...]}."""
    body = {"query": f'"{name}"' + NEWS_TERMS, "sources": ["news"]}
    return _call(conn, ctx, source="crustdata", tool="crustdata", endpoint="POST /web/search/live",
                 cache_key=f"crust:news:{name.lower()}", request=body,
                 live_fn=lambda: _crust_post("/web/search/live", body), mode=config.CRUSTDATA_MODE,
                 credits_fn=lambda d: 1.0 if isinstance(d, dict) and d.get("results") else 0.0)


_PUB = [(r"article:published_time[\"'\s]+content=[\"']([^\"']+)", "article:published_time"),
        (r"\"datePublished\"\s*:\s*\"([^\"]+)\"", "ld+json datePublished"),
        (r"Published On ([A-Z][a-z]{2} \d{2}, \d{4} at \d{1,2}:\d{2} [AP]M) IST", "page text 'Published On'")]


def _page_view(resp) -> list[dict]:
    """Normalise /web/enrich/live: raw [{success,url,timestamp,title,content}] or the reduced recording
    [{success,url,timestamp,title,published,published_source}] → the reduced form."""
    import datetime as dt
    out = []
    for p in resp if isinstance(resp, list) else []:
        if "content" not in p:
            out.append(p)
            continue
        pub, src, c = None, None, p.get("content") or ""
        for pat, name in _PUB:
            m = re.search(pat, c)
            if m:
                pub, src = m.group(1), name
                if name.startswith("page text"):
                    pub = dt.datetime.strptime(pub, "%b %d, %Y at %I:%M %p").strftime("%Y-%m-%dT%H:%M:00+05:30")
                break
        out.append({"success": p.get("success"), "url": p.get("url"), "timestamp": p.get("timestamp"),
                    "title": p.get("title"), "published": pub, "published_source": src})
    return out


def web_fetch(conn, ctx: RunCtx, url: str) -> Result:
    """POST /web/enrich/live (1 credit/page): read a news article to get its publish date. res.data = reduced page or None."""
    body = {"urls": [url]}
    res = _call(conn, ctx, source="crustdata", tool="crustdata", endpoint="POST /web/enrich/live",
                cache_key=f"crust:fetch:{url}", request=body,
                live_fn=lambda: _crust_post("/web/enrich/live", body), mode=config.CRUSTDATA_MODE,
                credits_fn=lambda d: float(sum(1 for p in (d or []) if isinstance(p, dict) and p.get("success"))),
                reduce_fn=_page_view)
    pages = _page_view(res.data)
    res.data = pages[0] if pages and pages[0].get("success") else None
    return res


def source_type(base: str, res: Result) -> str:
    """'crustdata' for live AND replay (replay = real recorded data); '<base>_mock' for offline fixtures."""
    return f"{base}_mock" if res.mode == "mock" else base
