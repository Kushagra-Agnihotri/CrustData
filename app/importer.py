"""AccountingSource interface + CsvImporter (our CSVs + a Tally-shaped voucher export).

Tally export mapping (tally_export_sample.csv):
  Voucher Type = "Purchase Order" → purchase_orders + po_lines
      Voucher No → po_number, Date → po_date, Party Ledger/GSTIN → vendor (matched on GSTIN),
      Item → item, Qty "50 trips" → qty_ordered + unit, Rate → unit_price, GST % → gst_rate, Due on → promised_date
  Voucher Type = "Receipt Note"   → grns   (Voucher No → grn_number, Due on → PO number, Qty → qty, Date → received_date)
Live TallyPrime (XML over HTTP) / Busy exports would implement the same Protocol.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
from pathlib import Path
from typing import Protocol

from app import config
from app.db import j, new_id, q1, tx
from app.ledger import add_event
from app.validators import normalize_name


class AccountingSource(Protocol):
    def vendors(self) -> list[dict]: ...
    def bank_accounts(self) -> list[dict]: ...
    def purchase_orders(self) -> list[dict]: ...
    def po_lines(self) -> list[dict]: ...
    def grns(self) -> list[dict]: ...
    def customers(self) -> list[dict]: ...
    def receivables(self) -> list[dict]: ...


def _read(p: Path) -> list[dict]:
    if not p.exists():
        return []
    with p.open(encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


class CsvImporter:
    def __init__(self, folder: Path | None = None):
        self.f = folder or (config.DATA / "seed")

    def vendors(self):
        return _read(self.f / "vendors.csv")

    def bank_accounts(self):
        return _read(self.f / "vendor_bank_accounts.csv")

    def _tally(self):
        return _read(self.f / "tally_export_sample.csv")

    def purchase_orders(self):
        out = [dict(r, source="csv") for r in _read(self.f / "purchase_orders.csv")]
        seen = set()
        for r in self._tally():
            if r["Voucher Type"] == "Purchase Order" and r["Voucher No"] not in seen:
                seen.add(r["Voucher No"])
                out.append({"po_number": r["Voucher No"], "vendor_gstin": r["GSTIN"],
                            "po_date": dt.datetime.strptime(r["Date"], "%d-%b-%Y").date().isoformat(),
                            "promised_date": r["Due on"], "source": "tally_csv"})
        return out

    def po_lines(self):
        out = _read(self.f / "po_lines.csv")
        n = {}
        for r in self._tally():
            if r["Voucher Type"] == "Purchase Order":
                n[r["Voucher No"]] = n.get(r["Voucher No"], 0) + 1
                qty, unit = (r["Qty"].split(" ", 1) + [""])[:2]
                out.append({"po_number": r["Voucher No"], "line_no": n[r["Voucher No"]], "item": r["Item"], "unit": unit,
                            "qty_ordered": qty, "unit_price": r["Rate"], "gst_rate": r["GST %"]})
        return out

    def grns(self):
        out = _read(self.f / "grns.csv")
        for r in self._tally():
            if r["Voucher Type"] == "Receipt Note":
                out.append({"grn_number": r["Voucher No"], "po_number": r["Due on"], "line_no": 1, "qty": r["Qty"].split(" ")[0],
                            "received_date": dt.datetime.strptime(r["Date"], "%d-%b-%Y").date().isoformat()})
        return out

    def customers(self):
        return _read(self.f / "customers.csv")

    def receivables(self):
        return _read(self.f / "receivables.csv")


def load(conn, src: AccountingSource) -> dict:
    ids = {}
    with tx(conn):
        for v in src.vendors():
            vid = new_id()
            ids[v["vendor_code"]] = vid
            conn.execute(
                "INSERT INTO vendors(id,legal_name,name_normalized,aliases,gstin,pan,udyam_no,msme_category,is_trader,has_written_agreement,domain,"
                "phone_on_file,city,state,credit_days,discount_pct,discount_days,is_critical,relationship_since,hist_invoices,hist_clean,"
                "hist_on_time,hist_deliveries,hist_disputes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (vid, v["legal_name"], normalize_name(v["legal_name"]), v.get("aliases") or "[]", v["gstin"], v["pan"], v["udyam_no"] or None, v["msme_category"],
                 int(v["is_trader"]), int(v["has_written_agreement"]), v["domain"], v["phone_on_file"], v["city"], v["state"],
                 int(v["credit_days"]), float(v["discount_pct"]), int(v["discount_days"]), int(v["is_critical"]), v["relationship_since"],
                 int(v["hist_invoices"]), int(v["hist_clean"]), int(v["hist_on_time"]), int(v["hist_deliveries"]), int(v["hist_disputes"])))
        by_gstin = {v["gstin"]: ids[v["vendor_code"]] for v in src.vendors()}
        for b in src.bank_accounts():
            conn.execute("INSERT INTO vendor_bank_accounts(id,vendor_id,account_no,ifsc,beneficiary_name,valid_from,status,proposed_by,approved_by,verification_note)"
                         " VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (new_id(), ids[b["vendor_code"]], b["account_no"], b["ifsc"], b["beneficiary_name"], b["valid_from"], b["status"],
                          "onboarding", b["approved_by"], "Verified at onboarding (seed)"))
        po_ids = {}
        for p in src.purchase_orders():
            pid = new_id()
            po_ids[p["po_number"]] = pid
            vid = ids.get(p.get("vendor_code")) or by_gstin.get(p.get("vendor_gstin"))
            conn.execute("INSERT INTO purchase_orders(id,po_number,vendor_id,po_date,promised_date,status,source) VALUES (?,?,?,?,?,'OPEN',?)",
                         (pid, p["po_number"], vid, p["po_date"], p["promised_date"], p["source"]))
        for ln in src.po_lines():
            conn.execute("INSERT INTO po_lines(id,po_id,line_no,item,unit,qty_ordered,unit_price,gst_rate) VALUES (?,?,?,?,?,?,?,?)",
                         (new_id(), po_ids[ln["po_number"]], int(ln["line_no"]), ln["item"], ln["unit"], float(ln["qty_ordered"]),
                          int(round(float(ln["unit_price"]) * 100)), float(ln["gst_rate"])))
        for g in src.grns():
            conn.execute("INSERT INTO grns(id,po_id,line_no,qty,received_date,grn_number) VALUES (?,?,?,?,?,?)",
                         (new_id(), po_ids[g["po_number"]], int(g["line_no"]), float(g["qty"]), g["received_date"], g["grn_number"]))
            conn.execute("UPDATE po_lines SET qty_received = qty_received + ? WHERE po_id=? AND line_no=?",
                         (float(g["qty"]), po_ids[g["po_number"]], int(g["line_no"])))
        for p in src.purchase_orders():
            r = q1(conn, "SELECT COUNT(*) n, SUM(qty_received < qty_ordered) part, SUM(qty_received > 0) any_r FROM po_lines WHERE po_id=?", (po_ids[p["po_number"]],))
            if r["any_r"] and r["part"]:
                conn.execute("UPDATE purchase_orders SET status='PARTIALLY_RECEIVED' WHERE id=?", (po_ids[p["po_number"]],))
        cids = {}
        for c in src.customers():
            cid = new_id()
            cids[c["customer_code"]] = cid
            conn.execute("INSERT INTO customers(id,legal_name,domain,on_time_rate) VALUES (?,?,?,?)",
                         (cid, c["legal_name"], c["domain"], float(c["on_time_rate"])))
        for r in src.receivables():
            conn.execute("INSERT INTO receivables(id,customer_id,invoice_no,amount,due_date,status) VALUES (?,?,?,?,?,?)",
                         (new_id(), cids[r["customer_code"]], r["invoice_no"], int(r["amount"]) * 100, r["due_date"], r["status"]))
        add_event(conn, "import", None, None, "LOADED", "importer", {"vendors": len(ids), "pos": len(po_ids)})
    return {"vendors": len(ids), "purchase_orders": len(po_ids)}
