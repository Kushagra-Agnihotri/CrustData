"""Record REAL Crustdata + IFSC responses into data/live_recordings.json, so the demo
replays real, dated data without Wi-Fi or a key (CRUSTDATA_MODE=replay, IFSC_MODE=replay).

  python scripts/record_live.py --pick astralpipes.com gacl.com ...   # identify candidates only (A1)
  python scripts/record_live.py --ifsc-only                           # free: every IFSC in seed + inbox
  python scripts/record_live.py                                       # full run (needs CRUSTDATA_API_KEY)

Full run = the real pipeline in live mode on a scratch database: every vendor and
customer gets identify (by domain) → enrich → news search (+ article fetch for the
publish date), case 07/19 signatories get a people search, the case 19 vendor gets a
finance-contact search, and every IFSC is looked up. Because it is the same code path,
it records exactly the cache keys that replay will ask for. RUN_CREDIT_LIMIT applies.

Privacy: people-search responses are written as {count, matches:[{title, current_company_id}]}
(no names, no profile URLs). The full response stays in the scratch DB's raw_responses only.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")


def credits_balance():
    from app import config
    import httpx
    if not config.CRUSTDATA_API_KEY:
        return None
    r = httpx.get(config.CRUSTDATA_BASE + "/account/credits", timeout=15,
                  headers={"authorization": f"Bearer {config.CRUSTDATA_API_KEY}", "x-api-version": config.CRUSTDATA_API_VERSION})
    if r.status_code == 401:
        sys.exit("401 from Crustdata: the API key in .env was rejected.")
    return (r.json().get("account") or {}).get("credits") if r.status_code == 200 else None


def pick(domains: list[str]) -> None:
    from app import config, sources
    from app.db import connect, init_schema
    config.CRUSTDATA_MODE, config.RECORD_LIVE = "live", False
    conn = connect(Path(tempfile.gettempdir()) / "payables_pick.db")
    init_schema(conn)
    conn.execute("DELETE FROM cache")
    print(f"{'domain':28} {'matched':7} {'name':44} {'founded':7} {'staff range':11} id")
    for d in domains:
        try:
            r = sources.company_identify(conn, sources.RunCtx(), domain=d)
        except sources.SourceUnavailable as e:
            print(f"{d:28} error   {e}")
            continue
        if not r.data:
            print(f"{d:28} no")
            continue
        b = r.data["company_data"]["basic_info"]
        print(f"{d:28} yes     {str(b.get('name'))[:44]:44} {str(b.get('year_founded') or '—'):7} {str(b.get('employee_count_range') or '—'):11} "
              f"{r.data['company_data']['crustdata_company_id']}")
    print("\nHeadcount and HQ need enrich (2 credits each): run the full recording for the vendors you pick.")


def all_ifsc_codes() -> list[str]:
    codes = set()
    for folder in ("seed", "inbox", "inbox_followup", "inbox_followup/case20b", "inbox_variants"):
        for f in (ROOT / "data" / folder).glob("*"):
            if f.is_file():
                codes |= set(re.findall(r"\b([A-Z]{4}0[A-Z0-9]{6})\b", f.read_text(encoding="utf-8", errors="ignore")))
    return sorted(codes)


def ifsc_only() -> None:
    from app import config, sources
    from app.db import connect, init_schema
    config.IFSC_MODE, config.RECORD_LIVE = "live", True
    conn = connect(Path(tempfile.gettempdir()) / "payables_ifsc.db")
    init_schema(conn)
    conn.execute("DELETE FROM cache")
    for code in all_ifsc_codes():
        try:
            r = sources.ifsc_lookup(conn, sources.RunCtx(), code)
            print(f"{code}  {'NOT FOUND (404)' if r.data is None else ', '.join(str(r.data.get(k)) for k in ('BANK', 'BRANCH', 'CITY', 'STATE'))}")
        except sources.SourceUnavailable as e:
            print(f"{code}  error: {e}")


def full() -> None:
    os.environ["DB_PATH"] = str(Path(tempfile.gettempdir()) / "payables_record.db")
    from app import config, pipeline, sources, vendor_admin
    from app.db import connect, q
    from scripts import setup_demo
    if not config.CRUSTDATA_API_KEY:
        sys.exit("CRUSTDATA_API_KEY is not set in .env. (Use --ifsc-only for the free IFSC part.)")
    config.CRUSTDATA_MODE = config.IFSC_MODE = "live"
    config.RECORD_LIVE = True
    config.DB_PATH = Path(os.environ["DB_PATH"])
    before = credits_balance()
    print(f"credits before: {before}")
    setup_demo.reset(config.DB_PATH, run=False, regenerate=True)        # customers: identify → enrich → news
    conn = connect()
    ctx = pipeline.start_run(conn)
    pipeline.ingest(conn)
    pipeline.ingest(conn, inbox=config.DATA / "inbox_followup")
    for inv in q(conn, "SELECT id FROM invoices ORDER BY rowid"):
        pipeline.process_one(conn, inv["id"], ctx, actor="record")
    for a in q(conn, "SELECT id FROM vendor_bank_accounts WHERE status='pending_verification'"):
        vendor_admin.suggest_second_contact(conn, a["id"], ctx)
    for code in all_ifsc_codes():
        try:
            sources.ifsc_lookup(conn, ctx, code)
        except sources.SourceUnavailable:
            pass
    used = q(conn, "SELECT COALESCE(SUM(credits),0) c, COUNT(*) n FROM calls WHERE mode='live' AND ok=1")[0]
    after = credits_balance()
    print(f"credits after: {after}  (this run: {used['c']:.2f} credits over {used['n']} calls; balance diff "
          f"{(before - after) if before is not None and after is not None else 'n/a'})")
    print(f"recordings → {sources.RECORDINGS}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--pick", nargs="+", metavar="DOMAIN")
    ap.add_argument("--ifsc-only", action="store_true")
    a = ap.parse_args()
    if a.pick:
        pick(a.pick)
    elif a.ifsc_only:
        ifsc_only()
    else:
        full()
