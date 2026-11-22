"""Record live Crustdata + IFSC responses for replay mode.

    python scripts/record_live.py --pick [domain ...]   identify candidate domains only (A1), print a table
    python scripts/record_live.py                       record everything the test set needs

Default mode runs the real pipeline once with CRUSTDATA_MODE=live and IFSC_MODE=live
on a throwaway database, so every lookup the desk makes (vendor + customer identify,
enrich, news search, signatory person search, IFSC for every account) is called once
and appended to data/live_recordings.json, keyed by the same cache_key replay uses.
RUN_CREDIT_LIMIT is enforced by the pipeline's run ceiling. Person-search responses are
reduced to titles before they are written (no names, no profile URLs).
"""
from __future__ import annotations

import csv
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

CANDIDATES = [  # edit freely; --pick prints what Crustdata knows about each
    "tcs.com", "infosys.com", "wipro.com", "bluedart.com", "deepakfertilisers.com", "aartiindustries.com",
    "bharatforge.com", "havells.com", "uflexltd.com", "tcpl.in", "allcargologistics.com", "gati.com",
    "byjus.com",
]


def _ctx_conn():
    from app import pipeline
    from app.db import connect, init_schema
    db = Path(tempfile.gettempdir()) / "payables_record_live.db"
    for suffix in ("", "-wal", "-shm"):
        f = Path(str(db) + suffix)
        if f.exists():
            f.unlink()
    conn = connect(db)
    init_schema(conn)
    return conn, pipeline.start_run(conn)


def pick(domains: list[str]):
    from app import config, sources
    config.CRUSTDATA_MODE = "live"
    conn, ctx = _ctx_conn()
    print(f"{'domain':28} {'match':5} {'name':38} {'founded':7} {'employees':10} hq")
    print("-" * 110)
    fails = 0
    for d in domains:
        try:
            r = sources.company_identify(conn, ctx, domain=d)
        except sources.SourceUnavailable as e:
            print(f"{d:28} ERROR {e}")
            fails += 1
            if isinstance(e, sources.AuthError) or fails >= 2:
                print("stopping: live call failed twice / not authorised")
                return
            continue
        if not r.data:
            print(f"{d:28} no")
            continue
        bi = r.data["company_data"].get("basic_info", {})
        hq = bi.get("headquarters") or bi.get("hq_location") or bi.get("location") or ""
        print(f"{d:28} {'yes':5} {str(bi.get('name'))[:38]:38} {str(bi.get('year_founded') or ''):7} "
              f"{str(bi.get('employee_count_range') or ''):10} {hq} (id {r.data['company_data']['crustdata_company_id']}, "
              f"conf {r.data.get('confidence_score')})")
    conn.commit()


def record():
    os.environ["CRUSTDATA_MODE"] = os.environ["IFSC_MODE"] = "live"
    from app import config, importer, pipeline, receivables, sources
    config.CRUSTDATA_MODE = config.IFSC_MODE = "live"
    if not config.CRUSTDATA_API_KEY:
        sys.exit("CRUSTDATA_API_KEY is not set in .env")
    before = sources.account_credits()
    print(f"credits before: {before}")
    conn, ctx = _ctx_conn()
    importer.load(conn, importer.CsvImporter())
    receivables.forecast_all(conn)                       # customers: identify -> enrich -> news
    out = pipeline.run_pipeline(conn, actor="record_live")   # vendors, signatories, IFSC on invoices
    # IFSC for every account in the seed (incl. ones no invoice uses)
    with (config.DATA / "seed" / "vendor_bank_accounts.csv").open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            try:
                sources.ifsc_lookup(conn, ctx, row["ifsc"])
            except sources.SourceUnavailable as e:
                print(f"IFSC {row['ifsc']}: {e}")
    conn.commit()
    after = sources.account_credits()
    rec = sources._recordings()
    used = sum(r.get("credits") or 0 for r in rec.values())
    print(f"pipeline: {out}")
    print(f"recordings: {len(rec)} → {sources.RECORDINGS}")
    print(f"credits after: {after}  (difference {None if before is None or after is None else round(before - after, 2)}; "
          f"sum of X-Credits-Used in recordings {used:.2f})")
    fails = [r for r in conn.execute("SELECT endpoint, error FROM calls WHERE ok=0").fetchall()]
    if fails:
        print(f"{len(fails)} failed calls:")
        for f in fails[:20]:
            print("  ", f[0], f[1])


if __name__ == "__main__":
    if "--pick" in sys.argv:
        extra = [a for a in sys.argv[1:] if a != "--pick"]
        pick(extra or CANDIDATES)
    else:
        record()
