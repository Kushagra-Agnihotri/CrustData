"""Evidence & audit ledger: facts (with source + date), events, calls (cost)."""
from __future__ import annotations

from app.db import j, new_id, now_iso


def add_fact(conn, entity_type, entity_id, key, value, source_type, source_ref,
             source_field, retrieved_at=None, raw_response_id=None) -> str:
    fid = new_id()
    conn.execute(
        "INSERT INTO facts(id,entity_type,entity_id,key,value,source_type,source_ref,source_field,retrieved_at,raw_response_id)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (fid, entity_type, entity_id, key, j(value), source_type, source_ref, source_field,
         retrieved_at or now_iso(), raw_response_id),
    )
    return fid


def add_event(conn, entity_type, entity_id, from_state, to_state, actor, detail=None) -> None:
    conn.execute(
        "INSERT INTO events(entity_type,entity_id,from_state,to_state,actor,detail) VALUES (?,?,?,?,?,?)",
        (entity_type, entity_id, from_state, to_state, actor, j(detail or {})),
    )


def store_raw(conn, endpoint, request, response, status) -> str:
    rid = new_id()
    conn.execute(
        "INSERT INTO raw_responses(id,endpoint,request,response,status,retrieved_at) VALUES (?,?,?,?,?,?)",
        (rid, endpoint, j(request), j(response), status, now_iso()),
    )
    return rid


def record_call(conn, *, tool, endpoint, run_id=None, invoice_id=None, credits=0.0,
                input_tokens=None, output_tokens=None, est_cost_inr=0.0, latency_ms=0,
                ok=True, http_status=None, error=None, mode=None) -> None:
    conn.execute(
        "INSERT INTO calls(run_id,invoice_id,tool,endpoint,credits,input_tokens,output_tokens,est_cost_inr,"
        "latency_ms,ok,http_status,error,mode) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (run_id, invoice_id, tool, endpoint, credits, input_tokens, output_tokens, est_cost_inr,
         latency_ms, 1 if ok else 0, http_status, error, mode),
    )
    if run_id and credits:
        conn.execute("UPDATE pipeline_runs SET credits_used = credits_used + ? WHERE id=?", (credits, run_id))
