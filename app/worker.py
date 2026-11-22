"""Restartable worker: advances invoice state machines, then executes approved payments.

python -m app.worker          # loop forever (poll every 2 s)
python -m app.worker --once   # drain everything once and exit
"""
from __future__ import annotations

import argparse
import time

from app import payments, pipeline
from app.db import connect, init_schema


def drain(conn) -> dict:
    res = pipeline.run_pipeline(conn, actor="worker")
    res["payments"] = payments.execute_approved(conn, actor="worker")
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args()
    conn = connect()
    init_schema(conn)
    if a.once:
        print(drain(conn), flush=True)
        return
    print("worker started", flush=True)
    while True:
        r = drain(conn)
        if r["processed"] or any(r["payments"].values()):
            print(r, flush=True)
        time.sleep(2)


if __name__ == "__main__":
    main()
