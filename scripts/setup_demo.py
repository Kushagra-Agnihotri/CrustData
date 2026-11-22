"""Reset the mock database and load everything: python scripts/setup_demo.py [--run]

--run  also runs the pipeline over data/inbox and proposes this week's payment run.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def reset(db_path: Path | None = None, run: bool = False, regenerate: bool = True) -> dict:
    from app import config, importer, pipeline, receivables, scheduler, sources
    from app.db import connect, init_schema
    if regenerate:
        import scripts.generate_mock_data as gen
        gen.main()
    p = Path(db_path or config.DB_PATH)
    for suffix in ("", "-wal", "-shm"):
        f = Path(str(p) + suffix)
        if f.exists():
            f.unlink()
    sources._MOCK = None
    conn = connect(p)
    init_schema(conn)
    out = importer.load(conn, importer.CsvImporter())
    receivables.forecast_all(conn)
    if run:
        out["pipeline"] = pipeline.run_pipeline(conn, actor="setup")
        out["payment_run"] = scheduler.propose_run(conn)
    conn.close()
    return out


if __name__ == "__main__":
    print(reset(run="--run" in sys.argv))
