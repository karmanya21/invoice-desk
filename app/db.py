"""SQLite storage: reference data (vendors, POs), runs, and the stage trace of every run."""
import json
import shutil
import sqlite3
from contextlib import contextmanager

from . import config
from .seed_data import GOODS_RECEIPTS, PURCHASE_ORDERS, VENDORS

SCHEMA = """
CREATE TABLE IF NOT EXISTS vendors (
    id TEXT PRIMARY KEY, name TEXT, aliases TEXT, tax_id TEXT, bank_last4 TEXT,
    status TEXT, contact_email TEXT
);
CREATE TABLE IF NOT EXISTS purchase_orders (
    po_number TEXT PRIMARY KEY, vendor_id TEXT, description TEXT, currency TEXT,
    amount REAL, tax_rate REAL, buyer TEXT, issued_date TEXT
);
CREATE TABLE IF NOT EXISTS po_lines (
    po_number TEXT, line_no INTEGER, description TEXT, qty REAL, unit_price REAL
);
CREATE TABLE IF NOT EXISTS goods_receipts (
    po_number TEXT, line_no INTEGER, qty REAL, received_date TEXT, receipt_no TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY, created_at TEXT, filename TEXT, file_hash TEXT, file_path TEXT, mime TEXT,
    status TEXT,            -- running | done | error
    decision TEXT,          -- what the process decided: APPROVED | HOLD | REJECTED
    final_state TEXT,       -- decision after any human review
    headline TEXT, owner TEXT, extractor TEXT,
    extraction TEXT, findings TEXT, result TEXT, review TEXT, corrections TEXT,
    vendor_id TEXT, vendor_name TEXT, invoice_number TEXT, invoice_number_norm TEXT,
    invoice_date TEXT, po_number TEXT, currency TEXT,
    net_amount REAL, tax_amount REAL, total_amount REAL, duration_ms INTEGER
);
CREATE TABLE IF NOT EXISTS stages (
    run_id TEXT, seq INTEGER, key TEXT, title TEXT, status TEXT, summary TEXT,
    details TEXT, duration_ms INTEGER,
    PRIMARY KEY (run_id, seq)
);
"""

JSON_RUN_FIELDS = ("extraction", "findings", "result", "review", "corrections")


@contextmanager
def connect():
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init():
    config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    with connect() as c:
        c.executescript(SCHEMA)
        if c.execute("SELECT COUNT(*) FROM vendors").fetchone()[0] == 0:
            _seed(c)


def _seed(c):
    for v in VENDORS:
        c.execute(
            "INSERT INTO vendors VALUES (?,?,?,?,?,?,?)",
            (v["id"], v["name"], json.dumps(v["aliases"]), v["tax_id"], v["bank_last4"], v["status"], v["contact_email"]),
        )
    for po in PURCHASE_ORDERS:
        amount = round(sum(q * p for _, q, p in po["lines"]), 2)
        c.execute(
            "INSERT INTO purchase_orders VALUES (?,?,?,?,?,?,?,?)",
            (po["po_number"], po["vendor_id"], po["description"], po["currency"], amount, po["tax_rate"], po["buyer"], po["issued_date"]),
        )
        for i, (desc, qty, price) in enumerate(po["lines"], 1):
            c.execute("INSERT INTO po_lines VALUES (?,?,?,?,?)", (po["po_number"], i, desc, qty, price))
    c.executemany("INSERT INTO goods_receipts VALUES (?,?,?,?,?)", GOODS_RECEIPTS)


def reset():
    """Wipe all runs and restore the reference data to its starting state."""
    with connect() as c:
        for t in ("stages", "runs", "goods_receipts", "po_lines", "purchase_orders", "vendors"):
            c.execute(f"DELETE FROM {t}")
        _seed(c)
    if config.UPLOAD_DIR.exists():
        shutil.rmtree(config.UPLOAD_DIR)
    config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


# --- reference data ----------------------------------------------------------
def vendors():
    with connect() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM vendors ORDER BY id")]
    for r in rows:
        r["aliases"] = json.loads(r["aliases"] or "[]")
    return rows


def _po_dict(c, row, exclude_run=None):
    po = dict(row)
    po["lines"] = [dict(r) for r in c.execute("SELECT * FROM po_lines WHERE po_number=? ORDER BY line_no", (po["po_number"],))]
    for line in po["lines"]:
        rec = c.execute("SELECT COALESCE(SUM(qty),0), GROUP_CONCAT(DISTINCT receipt_no) FROM goods_receipts WHERE po_number=? AND line_no=?",
                        (po["po_number"], line["line_no"])).fetchone()
        line["received"], line["receipts"] = rec[0], rec[1] or ""
    po["received_value"] = round(sum(l["received"] * l["unit_price"] for l in po["lines"]), 2)
    q = "SELECT id, invoice_number, net_amount FROM runs WHERE po_number=? AND final_state='APPROVED'"
    args = [po["po_number"]]
    if exclude_run:
        q += " AND id<>?"
        args.append(exclude_run)
    po["invoices"] = [dict(r) for r in c.execute(q + " ORDER BY created_at", args)]
    po["invoiced"] = round(sum(i["net_amount"] or 0 for i in po["invoices"]), 2)
    po["remaining"] = round(po["amount"] - po["invoiced"], 2)
    po["state"] = "closed" if po["remaining"] <= 0.005 else ("partially invoiced" if po["invoiced"] > 0 else "open")
    return po


def purchase_orders(vendor_id=None, exclude_run=None):
    with connect() as c:
        if vendor_id:
            rows = c.execute("SELECT * FROM purchase_orders WHERE vendor_id=? ORDER BY po_number", (vendor_id,)).fetchall()
        else:
            rows = c.execute("SELECT * FROM purchase_orders ORDER BY po_number").fetchall()
        return [_po_dict(c, r, exclude_run) for r in rows]


def purchase_order(po_number, exclude_run=None):
    with connect() as c:
        row = c.execute("SELECT * FROM purchase_orders WHERE po_number=?", (po_number,)).fetchone()
        return _po_dict(c, row, exclude_run) if row else None


# --- runs --------------------------------------------------------------------
def create_run(run):
    cols = ",".join(run.keys())
    with connect() as c:
        c.execute(f"INSERT INTO runs ({cols}) VALUES ({','.join('?' * len(run))})", list(run.values()))


def update_run(run_id, **fields):
    for k in JSON_RUN_FIELDS:
        if k in fields and fields[k] is not None and not isinstance(fields[k], str):
            fields[k] = json.dumps(fields[k])
    sets = ",".join(f"{k}=?" for k in fields)
    with connect() as c:
        c.execute(f"UPDATE runs SET {sets} WHERE id=?", [*fields.values(), run_id])


def _run_dict(row):
    r = dict(row)
    for k in JSON_RUN_FIELDS:
        r[k] = json.loads(r[k]) if r.get(k) else None
    r.pop("file_path", None)
    return r


def get_run(run_id, with_stages=True):
    with connect() as c:
        row = c.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            return None
        run = _run_dict(row)
        if with_stages:
            run["stages"] = []
            for s in c.execute("SELECT * FROM stages WHERE run_id=? ORDER BY seq", (run_id,)):
                s = dict(s)
                s["details"] = json.loads(s["details"] or "[]")
                run["stages"].append(s)
    return run


def run_file(run_id):
    with connect() as c:
        row = c.execute("SELECT file_path, mime, filename FROM runs WHERE id=?", (run_id,)).fetchone()
    return dict(row) if row else None


def list_runs():
    with connect() as c:
        return [_run_dict(r) for r in c.execute("SELECT * FROM runs ORDER BY created_at DESC")]


def prior_runs(vendor_id, exclude_run):
    """Earlier invoices from this vendor that were not rejected (candidates for duplicate checks)."""
    with connect() as c:
        rows = c.execute(
            "SELECT * FROM runs WHERE vendor_id=? AND id<>? AND status='done' AND final_state<>'REJECTED' ORDER BY created_at",
            (vendor_id, exclude_run),
        ).fetchall()
    return [_run_dict(r) for r in rows]


def runs_with_hash(file_hash, exclude_run):
    with connect() as c:
        rows = c.execute(
            "SELECT * FROM runs WHERE file_hash=? AND id<>? AND status='done' AND final_state<>'REJECTED' ORDER BY created_at",
            (file_hash, exclude_run),
        ).fetchall()
    return [_run_dict(r) for r in rows]


def clear_stages(run_id):
    with connect() as c:
        c.execute("DELETE FROM stages WHERE run_id=?", (run_id,))


def upsert_stage(run_id, seq, key, title, status, summary="", details=None, duration_ms=None):
    with connect() as c:
        c.execute(
            "INSERT OR REPLACE INTO stages VALUES (?,?,?,?,?,?,?,?)",
            (run_id, seq, key, title, status, summary, json.dumps(details or []), duration_ms),
        )
