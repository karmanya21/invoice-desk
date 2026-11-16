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


# ---------------------------------------------------------------------------------
# Rule bugs found by stress-testing, and the "ask, resume, learn" behaviours.
# ---------------------------------------------------------------------------------
import threading
import time

MERIDIAN = dict(vendor_name="Meridian Office Supply Co.", vendor_tax_id="47-2819934", bank_account="4410",
                invoice_date="2026-11-03", currency="USD", po_number="PO-4471")
DESKS = [{"description": "Height-adjustable desk", "quantity": 10, "unit_price": 640, "amount": 6400}]


def simulate(monkeypatch, fields, name="sim.png"):
    """Process an invoice whose read is given, so only the rules are under test."""
    global _n
    from app import extract as ex
    _n += 1
    rid = f"S-{_n:03d}"
    meta = {"extractor": "gemini", "model": "sim", "fallback_reason": None, "second": None, "second_model": None, "second_wanted": False}
    monkeypatch.setattr(pipeline, "extract", lambda *a: (ex.normalise(fields), meta))
    db.create_run({"id": rid, "created_at": f"2026-11-16T10:00:{_n:02d}+00:00", "filename": name, "file_path": "", "mime": "image/png", "status": "running"})
    pipeline.run_pipeline(rid, rid.encode(), name, "image/png")
    return db.get_run(rid)


def settle(run_id, timeout=5):
    """Wait for background re-checks to finish and return the run."""
    end = time.time() + timeout
    while time.time() < end:
        time.sleep(0.15)
        r = db.get_run(run_id)
        if r["status"] != "running" and not any(t.name.startswith("Thread") and t.is_alive() for t in threading.enumerate() if t is not threading.main_thread()):
            return r
    return db.get_run(run_id)


def test_goods_not_on_the_po_are_held(monkeypatch):
    r = simulate(monkeypatch, {**MERIDIAN, "invoice_number": "A1", "subtotal": 5000, "tax_amount": 400, "total": 5400,
                               "line_items": [{"description": "Strategy consulting retainer", "quantity": 1, "unit_price": 5000, "amount": 5000}]})
    assert r["decision"] == "HOLD" and "NOT_ON_PO" in codes(r)


def test_credit_note_is_never_approved_as_a_payment(monkeypatch):
    r = simulate(monkeypatch, {**MERIDIAN, "invoice_number": "CN-1", "subtotal": -640, "tax_amount": -51.2, "total": -691.2, "line_items": []})
    assert r["decision"] == "HOLD" and codes(r) == {"CREDIT_NOTE"} and r["po_number"] is None


def test_zero_value_invoice_is_held(monkeypatch):
    r = simulate(monkeypatch, {**MERIDIAN, "invoice_number": "Z1", "subtotal": 0, "tax_amount": 0, "total": 0, "line_items": []})
    assert r["decision"] == "HOLD" and "ZERO_VALUE" in codes(r)


def test_more_units_than_ordered_is_held_even_when_the_value_fits(monkeypatch):
    r = simulate(monkeypatch, {**MERIDIAN, "invoice_number": "Q1", "subtotal": 2520, "tax_amount": 201.6, "total": 2721.6,
                               "line_items": [{"description": "Dual monitor arm", "quantity": 30, "unit_price": 84, "amount": 2520}]})
    assert r["decision"] == "HOLD" and {"QTY_OVER_PO", "NOT_RECEIVED"} <= codes(r)


def test_single_tax_inclusive_total_is_not_a_false_alarm(monkeypatch):
    r = simulate(monkeypatch, {**MERIDIAN, "invoice_number": "T2", "total": 17550, "line_items": [{"description": "Office furniture as per PO", "amount": 17550}]})
    assert r["decision"] == "APPROVED" and r["net_amount"] == 16250.00 and "TAX_ASSUMED_INCLUDED" in codes(r)


def test_small_overage_and_price_difference_pass_within_tolerance(monkeypatch):
    r = simulate(monkeypatch, {**MERIDIAN, "invoice_number": "T1", "subtotal": 16370, "tax_amount": 1309.6, "total": 17679.6,
                               "line_items": [{"description": "Office furniture as per PO", "amount": 16370}]})
    assert r["decision"] == "APPROVED" and "WITHIN_TOLERANCE" in codes(r)
    db.reset()
    r = simulate(monkeypatch, {**MERIDIAN, "invoice_number": "T3", "subtotal": 6450, "tax_amount": 516, "total": 6966,
                               "line_items": [{"description": "Height-adjustable desk", "quantity": 10, "unit_price": 645, "amount": 6450}]})
    assert r["decision"] == "APPROVED" and "PRICE_WITHIN_TOLERANCE" in codes(r)


def test_two_invoices_at_once_cannot_both_draw_on_the_same_po(monkeypatch):
    from app import extract as ex
    meta = {"extractor": "gemini", "model": "sim", "fallback_reason": None, "second": None, "second_model": None, "second_wanted": False}

    def read(data, mime, text):
        i = data[0]
        return ex.normalise(dict(vendor_name="Halden Industrial Parts Ltd", vendor_tax_id="83-1150276", bank_account="9027", invoice_number=f"HIP-90{i}",
                                 invoice_date=f"2026-10-{10 + i * 9}", currency="USD", po_number="PO-4480", subtotal=25000 + i, tax_amount=2000, total=27000 + i,
                                 line_items=[{"description": "Spindle assembly HX-200", "quantity": 10, "unit_price": 2500 + i / 10, "amount": 25000 + i}])), meta
    monkeypatch.setattr(pipeline, "extract", read)
    monkeypatch.setattr(config, "STAGE_PACING_MS", 100)

    def go(i):
        db.create_run({"id": f"C-{i}", "created_at": f"2026-11-16T10:00:0{i}+00:00", "filename": f"{i}.png", "file_path": "", "mime": "image/png", "status": "running"})
        pipeline.run_pipeline(f"C-{i}", bytes([i]) + b"x", f"{i}.png", "image/png")
    threads = [threading.Thread(target=go, args=(i,)) for i in (1, 2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(db.get_run(f"C-{i}")["decision"] for i in (1, 2)) == ["APPROVED", "HOLD"]
    assert db.purchase_order("PO-4480")["remaining"] >= 0


def test_clean_invoice_above_the_approval_limit_needs_a_signature(monkeypatch):
    config.RULES["auto_approve_limit"] = 5000
    r = simulate(monkeypatch, {**MERIDIAN, "invoice_number": "L1", "subtotal": 6400, "tax_amount": 512, "total": 6912, "line_items": DESKS})
    assert r["decision"] == "HOLD" and codes(r) == {"ABOVE_APPROVAL_LIMIT", "PARTIAL"} and r["owner"] == "Finance controller"
    assert r["result"]["ask"]["options"][0]["action"] == "approve"
    done, problem = pipeline.answer(r["id"], 0, "Maya")
    assert problem is None and done["final_state"] == "APPROVED" and "Answer: Approve payment" in done["review"]["note"]


def test_every_hold_asks_a_question_with_answers():
    process("01_happy_path.pdf")
    r = process("05_reissued_duplicate.pdf")
    ask = r["result"]["ask"]
    assert "INV-20831-R" in ask["question"] and ask["recommend"] and [o["action"] for o in ask["options"]] == ["reject", "approve"]
    assert r["result"]["related_run"]


def test_held_invoice_resumes_by_itself_when_the_receipt_is_recorded():
    r = process("08_billed_before_receipt.pdf")
    assert r["decision"] == "HOLD" and codes(r) == {"NOT_RECEIVED", "PARTIAL"}
    assert r["result"]["ask"]["options"][0]["action"] == "receipt"
    db.add_receipt("PO-4503", 1, 1, "2026-11-16", "GRN-TEST")
    pipeline.recheck_holds("receipt GRN-TEST recorded").join()
    r = db.get_run(r["id"])
    assert r["decision"] == "APPROVED" and r["corrections"][-1]["kind"] == "recheck"
    assert db.purchase_order("PO-4503")["remaining"] == 4000.00


def test_recheck_that_changes_nothing_leaves_no_trace():
    process("02_split_po_shipment_1.pdf")
    r = process("03_split_po_shipment_2_over.pdf")
    pipeline.recheck_holds("unrelated change").join()
    after = db.get_run(r["id"])
    assert after["decision"] == "HOLD" and codes(after) == codes(r) and not after["corrections"]


def test_answering_yes_to_a_trading_name_is_remembered():
    r = process("09_trading_name.pdf")
    assert r["decision"] == "HOLD" and "VENDOR_NAME_UNRECOGNISED" in codes(r) and r["vendor_id"] == "V-004"
    assert r["result"]["ask"]["options"][0]["action"] == "learn_alias"
    _, problem = pipeline.answer(r["id"], 0, "Asha")
    assert problem is None
    r = settle(r["id"])
    assert r["decision"] == "APPROVED" and r["corrections"][-1]["kind"] == "answer"
    assert "BW Hosting" in next(v for v in db.vendors() if v["id"] == "V-004")["aliases"]
    assert "BW Hosting" in db.learned()[0]["what"]


def test_changing_a_rule_rechecks_holds_and_reset_restores_it(monkeypatch):
    r = simulate(monkeypatch, {**MERIDIAN, "invoice_number": "U1", "subtotal": 6720, "tax_amount": 537.6, "total": 7257.6,
                               "line_items": [{"description": "Height-adjustable desk", "quantity": 10, "unit_price": 672, "amount": 6720}]})
    assert r["decision"] == "HOLD" and "PRICE_VARIANCE" in codes(r)      # +5% against a 2% tolerance
    db.set_rule("unit_price_tolerance_pct", 6, "Controller", pipeline.now())
    pipeline.recheck_holds("rule changed").join()
    assert db.get_run(r["id"])["decision"] == "APPROVED"
    db.reset()
    assert config.RULES["unit_price_tolerance_pct"] == 2.0 and db.rule_changes() == []
