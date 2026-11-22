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
    sources._MOCK = None
    conn = connect(p)
    # Drop tables rather than deleting the file: on Windows a running UI keeps the file open.
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    conn.execute("PRAGMA foreign_keys=OFF")
    for t in tables:
        conn.execute(f'DROP TABLE IF EXISTS "{t}"')
    conn.execute("DELETE FROM sqlite_sequence") if conn.execute("SELECT 1 FROM sqlite_master WHERE name='sqlite_sequence'").fetchone() else None
    conn.execute("PRAGMA foreign_keys=ON")
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
