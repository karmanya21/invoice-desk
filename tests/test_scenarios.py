"""End-to-end check of every demo scenario, using the offline parser (no API key needed).

    pytest -q
"""
import os
import tempfile
from pathlib import Path

os.environ["DATA_DIR"] = tempfile.mkdtemp()
os.environ["GEMINI_API_KEY"] = ""
os.environ["STAGE_PACING_MS"] = "0"

from app import config, db, pipeline  # noqa: E402

config.GEMINI_API_KEY = ""
SAMPLES = Path(__file__).resolve().parent.parent / "samples"
_n = 0


def process(name):
    global _n
    _n += 1
    run_id = f"T-{_n:03d}"
    data = (SAMPLES / name).read_bytes()
    db.create_run({"id": run_id, "created_at": f"2026-11-16T10:00:{_n:02d}", "filename": name, "file_path": "", "mime": "application/pdf", "status": "running"})
    pipeline.run_pipeline(run_id, data, name, "application/pdf")
    return db.get_run(run_id)


def codes(run):
    return {f["code"] for f in run["findings"] or []}


def setup_function():
    db.init()
    db.reset()


def test_happy_path_is_approved_and_closes_po():
    r = process("01_happy_path.pdf")
    assert r["decision"] == "APPROVED", r["headline"]
    assert r["po_number"] == "PO-4471" and r["net_amount"] == 16250.00 and r["total_amount"] == 17550.00
    assert db.purchase_order("PO-4471")["state"] == "closed"


def test_split_po_part_invoice_then_overbilling():
    first = process("02_split_po_shipment_1.pdf")
    assert first["decision"] == "APPROVED" and "PARTIAL" in codes(first)
    assert db.purchase_order("PO-4480")["remaining"] == 15000.00
    second = process("03_split_po_shipment_2_over.pdf")
    assert second["decision"] == "HOLD" and "OVER_PO" in codes(second)
    assert "Daniel Okafor" in second["owner"]
    # A held invoice must not consume PO balance until a person approves it.
    assert db.purchase_order("PO-4480")["remaining"] == 15000.00
    pipeline.review(second["id"], "approve", "Daniel", "PO amended")
    assert db.purchase_order("PO-4480")["remaining"] == -2500.00


def test_reissued_invoice_is_caught_as_probable_duplicate():
    process("01_happy_path.pdf")
    r = process("05_reissued_duplicate.pdf")
    assert r["decision"] == "HOLD" and {"PROBABLE_DUPLICATE", "PO_EXHAUSTED"} <= codes(r)


def test_same_file_twice_is_rejected():
    process("01_happy_path.pdf")
    r = process("01_happy_path.pdf")
    assert r["decision"] == "REJECTED" and "DUPLICATE_FILE" in codes(r)


def test_lookalike_vendor_is_held_for_identity():
    r = process("06_lookalike_vendor_new_bank.pdf")
    assert r["decision"] == "HOLD" and "VENDOR_IDENTITY" in codes(r)
    assert r["owner"] == "Vendor master team"


def test_missing_fields_goes_back_to_vendor_with_a_draft():
    r = process("07_missing_invoice_number.pdf")
    assert r["decision"] == "REJECTED" and "MISSING_FIELDS" in codes(r)
    assert r["result"]["messages"][0]["kind"] == "vendor"


def test_scanned_invoice_without_ai_is_held_not_crashed():
    r = process("04_scanned_no_po.pdf")
    assert r["status"] == "done" and r["decision"] == "HOLD" and "UNREADABLE" in codes(r)


def test_inferred_po_and_embedded_tax(monkeypatch):
    """Simulate what the vision model returns for the scanned invoice, then let the rules run."""
    from app import extract as ex
    fields = ex.normalise({
        "vendor_name": "Kestrel Logistics Inc.", "vendor_tax_id": "36-7704112", "bank_account": "ending 1185",
        "invoice_number": "KL/2026/1187", "invoice_date": "2026-11-02", "currency": "USD",
        "line_items": [{"description": "Regional freight distribution, October 2026", "amount": 9072.00}],
        "tax_rate": 8, "tax_included_in_total": True, "total": 9072.00,
    })
    monkeypatch.setattr(pipeline, "extract", lambda *a: (fields, {"extractor": "gemini", "model": "test", "fallback_reason": None}))
    r = process("04_scanned_no_po.pdf")
    assert r["decision"] == "APPROVED", r["headline"]
    assert r["po_number"] == "PO-4502" and r["net_amount"] == 8400.00 and "PO_INFERRED" in codes(r)


def _fake_read(monkeypatch, fields, second=None):
    from app import extract as ex
    meta = {"extractor": "gemini", "model": "model-a", "fallback_reason": None, "second": ex.normalise(second) if second else None,
            "second_model": "model-b" if second else None, "second_wanted": True}
    monkeypatch.setattr(pipeline, "extract", lambda *a: (ex.normalise(fields), meta))


KESTREL = {"vendor_name": "Kestrel Logistics Inc.", "vendor_tax_id": "36-7704112", "bank_account": "ending 1185",
           "invoice_number": "KL/2026/1187", "invoice_date": "2026-11-02", "currency": "USD",
           "line_items": [{"description": "Regional freight distribution, October 2026", "amount": 9072.00}],
           "tax_rate": 8, "tax_included_in_total": True, "total": 9072.00}


def test_scan_is_held_when_two_reads_disagree(monkeypatch):
    _fake_read(monkeypatch, KESTREL, {**KESTREL, "total": 9027.00})
    r = process("04_scanned_no_po.pdf")
    assert r["decision"] == "HOLD" and "READ_DISAGREEMENT" in codes(r)


def test_scan_is_approved_when_two_reads_agree(monkeypatch):
    _fake_read(monkeypatch, KESTREL, {**KESTREL, "vendor_name": "KESTREL LOGISTICS INC.", "bank_account": "1185"})
    r = process("04_scanned_no_po.pdf")
    assert r["decision"] == "APPROVED" and "READ_DISAGREEMENT" not in codes(r)


def test_billing_ahead_of_goods_received_is_held():
    # 10 of 16 spindles are invoiced, then 7 are billed although only 6 more were received.
    process("02_split_po_shipment_1.pdf")
    r = process("03_split_po_shipment_2_over.pdf")
    assert {"OVER_PO", "NOT_RECEIVED"} <= codes(r)
    # Cloud hosting: 10 of 12 months confirmed. An invoice for all 12 fits the PO but not the receipts.
    from app import extract as ex
    fields = ex.normalise({"vendor_name": "Brightwave Cloud Services", "vendor_tax_id": "91-3348820", "invoice_number": "BW-1", "invoice_date": "2026-11-05",
                           "po_number": "PO-4515", "currency": "USD", "subtotal": 23400, "tax_amount": 0, "total": 23400,
                           "line_items": [{"description": "Managed cloud hosting - monthly", "quantity": 12, "unit_price": 1950, "amount": 23400}]})
    c = pipeline.Ctx("T-X", b"x", "x.pdf", "image/png")
    c.inv, c.net, c.po = fields, 23400.0, db.purchase_order("PO-4515")
    pipeline.stage_amounts(c)
    assert [f["code"] for f in c.findings] == ["NOT_RECEIVED"]


def test_correcting_fields_reruns_the_rules(tmp_path):
    import time
    r = process("07_missing_invoice_number.pdf")
    assert r["decision"] == "REJECTED"
    f = tmp_path / "x.pdf"
    f.write_bytes((SAMPLES / "07_missing_invoice_number.pdf").read_bytes())
    db.update_run(r["id"], file_path=str(f))
    assert pipeline.rerun(r["id"], {"invoice_number": "OFM-5512", "invoice_date": "2026-11-12"}, "Asha", "Vendor confirmed by phone") is None
    for _ in range(50):
        r = db.get_run(r["id"])
        if r["status"] != "running":
            break
        time.sleep(0.1)
    assert r["decision"] == "APPROVED", r["headline"]
    assert r["invoice_number"] == "OFM-5512" and r["corrections"][0]["by"] == "Asha" and len(r["corrections"][0]["changes"]) == 2
    assert pipeline.rerun(r["id"], {"total": 1}, "Asha", "x") is not None   # approved runs cannot be edited
