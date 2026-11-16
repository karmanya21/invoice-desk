"""The invoice process: eight stages from a raw document to a reasoned decision.

    1 intake      receive the file, fingerprint it, detect scan vs digital
    2 extract     read the invoice into structured fields (AI or offline parser)
    3 validate    required fields, arithmetic, dates
    4 vendor      is this really one of our approved vendors?
    5 duplicate   have we seen this invoice before?
    6 po_match    find the purchase order (stated on the invoice, or inferred)
    7 amounts     3-way match: what is LEFT on the PO and what has been RECEIVED
    8 decide      APPROVED / HOLD / REJECTED, with reasons, owner and next step

Every stage records what it checked and what it found. Stages do not stop at the
first problem: a reviewer should see everything wrong with an invoice in one go.
The decision is simply the most severe finding.
"""
import hashlib
import re
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from pathlib import Path
from difflib import SequenceMatcher

from . import config, db
from .extract import ExtractionError, extract, normalise, pdf_text

R = config.RULES

STAGES = [
    ("intake", "Receive document"),
    ("extract", "Read invoice"),
    ("validate", "Validate fields and arithmetic"),
    ("vendor", "Verify vendor"),
    ("duplicate", "Check for duplicates"),
    ("po_match", "Match purchase order"),
    ("amounts", "3-way match: PO balance and goods received"),
    ("decide", "Decide and route"),
]

OK, WARN, FAIL, SKIP = "ok", "warn", "fail", "skipped"
INFO, HOLD, REJECT = "info", "hold", "reject"


# --- small helpers -------------------------------------------------------------
def money(x, cur="USD"):
    if x is None:
        return "not found"
    sym = "$" if (cur or "USD") == "USD" else f"{cur} "
    return f"{sym}{x:,.2f}"


def norm_invoice_no(s):
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


SUFFIXES = {"co", "inc", "ltd", "llc", "company", "corp", "corporation", "limited", "plc", "the"}


def norm_vendor(s):
    words = re.sub(r"[^a-z0-9 ]", " ", (s or "").lower().replace("&", " and ")).split()
    return " ".join(w for w in words if w not in SUFFIXES)


def digits(s):
    return re.sub(r"\D", "", s or "")


STOP = {"the", "and", "of", "for", "a", "an", "to", "in", "services", "service"}


def tokens(s):
    out = set()
    for w in re.sub(r"[^a-z0-9 ]", " ", (s or "").lower()).split():
        if w in STOP or w.isdigit():
            continue
        out.add(w[:-1] if len(w) > 4 and w.endswith("s") else w)
    return out


def similarity(a, b):
    """Share of the shorter description's words found in the other (0-1)."""
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


def parse_date(s):
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d %b %Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            pass
    return None


def d(label, value, status=None):
    return {"label": label, "value": value, "status": status}


class Ctx:
    """Everything known about one run as it moves through the stages."""

    def __init__(self, run_id, data, filename, mime, override=None, corrections=None):
        self.run_id, self.data, self.filename, self.mime = run_id, data, filename, mime
        self.findings = []
        self.override = override          # fields corrected by a person: skip the AI read
        self.corrections = corrections or []
        self.future = None                # the AI read, started the moment the file arrives
        self.file_hash = hashlib.sha256(data).hexdigest()
        self.text, self.pages = pdf_text(data) if mime == "application/pdf" else ("", 1)
        self.inv = None          # extracted fields
        self.meta = {}
        self.net = None          # amount before tax, the figure compared to the PO
        self.vendor = None
        self.vendor_match = None
        self.po = None
        self.po_match = None     # "stated" | "inferred"
        self.stop = None         # reason later stages are skipped

    def find(self, stage, code, severity, title, message, owner="AP analyst", vendor_msg=None):
        self.findings.append({"stage": stage, "code": code, "severity": severity, "title": title,
                              "message": message, "owner": owner, "vendor_msg": vendor_msg})

    def worst(self, stage):
        sev = [f["severity"] for f in self.findings if f["stage"] == stage]
        return FAIL if REJECT in sev else WARN if HOLD in sev else OK


# --- Stage 1: intake -------------------------------------------------------------
def stage_intake(c: Ctx):
    pages = c.pages or 1
    kind = "Digital PDF (machine-readable text)" if c.text else ("Scanned PDF (image only, no text layer)" if c.mime == "application/pdf" else "Image file")
    details = [
        d("File", c.filename),
        d("Size", f"{len(c.data) / 1024:.0f} KB, {pages} page{'s' if pages != 1 else ''}"),
        d("Document type", kind, None if c.text else "warn"),
        d("Fingerprint (SHA-256)", c.file_hash[:16] + "..."),
    ]
    db.update_run(c.run_id, file_hash=c.file_hash)
    summary = "Digital PDF received" if c.text else "Scanned document received, needs the vision model to read it"
    return OK, summary, details


# --- Stage 2: extract ------------------------------------------------------------
READ_CHECKS = [
    ("vendor name", "vendor_name", norm_vendor), ("vendor tax ID", "vendor_tax_id", digits),
    ("invoice number", "invoice_number", norm_invoice_no), ("invoice date", "invoice_date", lambda v: v or ""),
    ("PO number", "po_number", digits), ("total", "total", lambda v: v),
    ("pay-to account", "bank_account", lambda v: digits(v)[-4:]),
]


def read_differences(a, b):
    """Fields where two independent reads of the same document disagree."""
    return [f"{label}: '{a[k] if a[k] is not None else 'nothing'}' vs '{b[k] if b[k] is not None else 'nothing'}'"
            for label, k, norm in READ_CHECKS if norm(a.get(k)) != norm(b.get(k))]


def stage_extract(c: Ctx):
    if c.override is not None:
        last = c.corrections[-1]
        c.inv, c.meta = c.override, {"extractor": "corrected"}
        details = [d("Read by", f"Earlier read, corrected by {last['by']}")]
        details += [d(f"Corrected: {ch['field']}", f"{ch['from'] if ch['from'] is not None else 'nothing'} changed to {ch['to'] if ch['to'] is not None else 'nothing'}", "warn") for ch in last["changes"]]
        details.append(d("Reason given", last["note"]))
        db.update_run(c.run_id, extraction=c.inv)
    else:
        try:
            c.inv, c.meta = c.future.result()
        except ExtractionError as e:
            c.find("extract", "UNREADABLE", HOLD, "Document could not be read automatically",
                   f"{e} Key the fields in by hand below, or re-run once the model is available.")
            c.stop = "the invoice could not be read"
            return FAIL, "Could not read the document", [d("Reason", str(e), "fail")]
        extractor = f"Gemini vision model ({c.meta['model']})" if c.meta["extractor"] == "gemini" else "Offline text parser"
        details = [d("Read by", extractor)]
        if c.meta.get("fallback_reason"):
            details.append(d("Why not the AI model", c.meta["fallback_reason"], "warn"))
        db.update_run(c.run_id, extractor=c.meta["extractor"], extraction=c.inv)
    inv, meta = c.inv, c.meta

    if not inv["is_invoice"]:
        c.find("extract", "NOT_AN_INVOICE", HOLD, "This does not look like an invoice",
               "The document was read but is not a request for payment. Check what was sent and file or discard it.")
        c.stop = "the document is not an invoice"
        return FAIL, "Document is not an invoice", details

    # A scan has no text layer to check against, so two models read it independently.
    # If they disagree on anything that affects payment, a person looks at it.
    if meta.get("second"):
        diffs = read_differences(inv, meta["second"])
        if diffs:
            c.find("extract", "READ_DISAGREEMENT", HOLD, "Two independent reads of this scan disagree",
                   f"{meta['model']} and {meta['second_model']} read the scan differently: " + "; ".join(diffs)
                   + ". Check the document and correct the fields below before it can be paid.")
            details.append(d(f"Second read ({meta['second_model']})", "disagrees on " + "; ".join(diffs), "fail"))
        else:
            details.append(d(f"Second read ({meta['second_model']})", "agrees on vendor, invoice number, date, PO, total and pay-to account", "ok"))
    elif meta.get("second_wanted"):
        c.find("extract", "READ_UNCONFIRMED", INFO, "Scan was read by one model only",
               "A second model normally re-reads scanned invoices as a cross-check, but it was unavailable. The single read still had to pass every rule below.")
        details.append(d("Second read", "unavailable, single read used", "warn"))

    cur = inv["currency"]
    shown = [
        ("Vendor", inv["vendor_name"]), ("Vendor tax ID", inv["vendor_tax_id"]), ("Invoice number", inv["invoice_number"]),
        ("Invoice date", inv["invoice_date"]), ("Due date", inv["due_date"]), ("PO number", inv["po_number"]),
        ("Subtotal", money(inv["subtotal"], cur) if inv["subtotal"] is not None else None),
        ("Tax", money(inv["tax_amount"], cur) if inv["tax_amount"] is not None else None),
        ("Total", money(inv["total"], cur) if inv["total"] is not None else None),
        ("Pay-to account", inv["bank_account"]),
    ]
    for label, val in shown:
        details.append(d(label, val if val else "not on the document", None if val else "warn"))
    n = len(inv["line_items"])
    itemised = n and all(li["quantity"] is not None and li["unit_price"] is not None for li in inv["line_items"])
    details.append(d("Line items", f"{n} ({'itemised with quantity and price' if itemised else 'bundled, no quantity or unit price'})" if n else "none found", None if n else "warn"))
    if inv["notes"]:
        details.append(d("Unusual statements", inv["notes"], "warn"))

    # Grounding: when a text layer exists, the key values the model returned must
    # literally appear in it. This catches a model that "helpfully" invents a value.
    if meta["extractor"] == "gemini" and c.text:
        flat = re.sub(r"[\s,]", "", c.text)
        missing = []
        if inv["invoice_number"] and re.sub(r"\s", "", inv["invoice_number"]) not in flat:
            missing.append("invoice number")
        if inv["total"] is not None and f"{inv['total']:.2f}" not in flat:
            missing.append("total")
        if missing:
            c.find("extract", "NOT_GROUNDED", HOLD, "Extracted values could not be confirmed against the document",
                   f"The AI model returned a {' and '.join(missing)} that does not appear in the PDF's own text. Verify by eye before paying.")
            details.append(d("Cross-check against PDF text", f"{', '.join(missing)} not found in text", "fail"))
        else:
            details.append(d("Cross-check against PDF text", "invoice number and total both appear verbatim", "ok"))

    found = sum(1 for _, v in shown if v)
    return (c.worst("extract") if c.override is None else WARN if c.worst("extract") == WARN else OK), f"Read {found} of {len(shown)} header fields and {n} line item{'s' if n != 1 else ''}", details


# --- Stage 3: validate -----------------------------------------------------------
def stage_validate(c: Ctx):
    inv, details = c.inv, []
    cur = inv["currency"] or "USD"
    if not inv["currency"]:
        inv["currency"] = "USD"
        details.append(d("Currency", "not stated, assumed USD", "warn"))

    # Without these we cannot dedupe, age or pay the invoice. Only the vendor can fix that.
    required = {"vendor_name": "the vendor's name", "invoice_number": "an invoice number", "invoice_date": "an invoice date", "total": "a total amount"}
    missing = [label for k, label in required.items() if inv[k] in (None, "")]
    if missing:
        c.find("validate", "MISSING_FIELDS", REJECT, "Invoice is missing required information",
               f"The document has no {' and no '.join(m.split(' ', 1)[1] for m in missing)}. Without these it cannot be recorded, "
               "checked for duplicates or scheduled for payment, so it goes back to the vendor rather than into a queue.",
               owner="Vendor",
               vendor_msg="It is missing " + ", ".join(missing) + ". Please reissue it with these included.")
    details.append(d("Required fields", "all present" if not missing else "missing: " + ", ".join(m.split(" ", 1)[1] for m in missing), "ok" if not missing else "fail"))

    # Work out the amount before tax: that is what a PO is written in.
    sub, tax, total, rate = inv["subtotal"], inv["tax_amount"], inv["total"], inv["tax_rate"]
    if sub is not None:
        c.net, how = sub, "subtotal printed on the invoice"
    elif total is not None and tax is not None:
        c.net, how = round(total - tax, 2), "total minus the printed tax amount"
    elif total is not None and rate:
        c.net = round(total / (1 + rate / 100), 2)
        c.tax_derived = round(total - c.net, 2)
        how = f"tax is embedded in the total, backed out at the {rate:g}% printed on the invoice"
    elif total is not None:
        c.net, how = total, "no tax shown anywhere, total treated as the pre-tax amount"
    else:
        how = None
    if c.net is not None:
        details.append(d("Amount before tax", f"{money(c.net, cur)} ({how})", None if sub is not None else "warn"))

    # The invoice's own arithmetic.
    slack, problems, checked = R["arithmetic_slack"], [], 0
    for li in inv["line_items"]:
        if None not in (li["quantity"], li["unit_price"], li["amount"]):
            checked += 1
            if abs(li["quantity"] * li["unit_price"] - li["amount"]) > slack:
                problems.append(f"'{li['description']}': {li['quantity']:g} x {money(li['unit_price'], cur)} is not {money(li['amount'], cur)}")
    amounts = [li["amount"] for li in inv["line_items"] if li["amount"] is not None]
    if amounts and sub is not None:
        checked += 1
        if abs(sum(amounts) - sub) > slack:
            problems.append(f"line items add up to {money(sum(amounts), cur)} but the subtotal says {money(sub, cur)}")
    if None not in (sub, tax, total):
        checked += 1
        if abs(sub + tax - total) > slack:
            problems.append(f"subtotal + tax is {money(sub + tax, cur)} but the total says {money(total, cur)}")
    if problems:
        c.find("validate", "ARITHMETIC", HOLD, "The invoice's own numbers do not add up", "; ".join(problems).capitalize() + ". Confirm the correct amount with the vendor before paying.")
    details.append(d("Arithmetic", "; ".join(problems) if problems else (f"{checked} checks passed" if checked else "nothing to cross-check (single bundled amount)"),
                     "fail" if problems else ("ok" if checked else "warn")))

    # Dates.
    inv_date = parse_date(inv["invoice_date"])
    if inv["invoice_date"] and not inv_date:
        details.append(d("Invoice date", f"'{inv['invoice_date']}' could not be understood", "warn"))
    if inv_date:
        age = (date.today() - inv_date).days
        if age < 0:
            c.find("validate", "FUTURE_DATED", HOLD, "Invoice is dated in the future", f"Invoice date {inv_date.isoformat()} is {-age} days from now. Confirm the date with the vendor.")
            details.append(d("Invoice date", f"{inv_date.isoformat()} is in the future", "fail"))
        elif age > R["max_invoice_age_days"]:
            c.find("validate", "STALE", HOLD, "Invoice is unusually old", f"Dated {inv_date.isoformat()}, {age} days ago (limit {R['max_invoice_age_days']}). Old invoices are a common source of double payment, check the ledger.")
            details.append(d("Invoice date", f"{age} days old", "fail"))
        else:
            details.append(d("Invoice date", f"{inv_date.isoformat()}, {age} days old", "ok"))
    c.inv_date = inv_date

    status = c.worst("validate")
    summary = {OK: "Fields complete and arithmetic consistent", WARN: "Problems found in the invoice's figures or dates", FAIL: "Required information is missing"}[status]
    return status, summary, details


# --- Stage 4: vendor -------------------------------------------------------------
def stage_vendor(c: Ctx):
    inv = c.inv
    name = inv["vendor_name"]
    if not name:
        return SKIP, "No vendor name on the document to verify", []
    target = norm_vendor(name)
    best, best_score, exact = None, 0.0, False
    for v in db.vendors():
        for candidate in [v["name"], *v["aliases"]]:
            cn = norm_vendor(candidate)
            score = 1.0 if cn == target else SequenceMatcher(None, cn, target).ratio()
            if score > best_score:
                best, best_score, exact = v, score, cn == target

    details = [d("Name on invoice", name)]
    if not best or best_score < R["vendor_fuzzy_threshold"]:
        c.find("vendor", "UNKNOWN_VENDOR", HOLD, "Vendor is not in the approved vendor list",
               f"'{name}' does not match any approved vendor (closest: {best['name'] if best else 'none'}, {best_score:.0%} similar). "
               "Do not pay until the vendor has been onboarded.", owner="Vendor master team")
        details.append(d("Vendor master", "no match", "fail"))
        return WARN, "Vendor not found in the approved list", details

    c.vendor, c.vendor_match = best, "exact" if exact else "similar"
    details.append(d("Vendor master", f"{best['name']} ({best['id']})" + ("" if exact else f", name is only {best_score:.0%} similar"), "ok" if exact else "warn"))

    if best["status"] != "approved":
        c.find("vendor", "BLOCKED_VENDOR", REJECT, "Vendor is blocked for payment",
               f"{best['name']} is marked '{best['status']}' in the vendor master. Invoices from blocked vendors are not processed.",
               owner="Vendor master team", vendor_msg="Your vendor account is currently not active for payment. Please contact our procurement team.")
        details.append(d("Status", best["status"], "fail"))
    else:
        details.append(d("Status", "approved", "ok"))

    # Identity checks. A look-alike name with different tax or bank details is the
    # classic vendor-impersonation pattern, so any mismatch stops payment.
    signals = []
    inv_tax, master_tax = digits(inv["vendor_tax_id"]), digits(best["tax_id"])
    if inv_tax:
        same = inv_tax == master_tax
        details.append(d("Tax ID", f"{inv['vendor_tax_id']} " + ("matches vendor master" if same else f"does not match vendor master ({best['tax_id']})"), "ok" if same else "fail"))
        if not same:
            signals.append(f"tax ID {inv['vendor_tax_id']} differs from the {best['tax_id']} on file")
    else:
        details.append(d("Tax ID", "not on the invoice, cannot be confirmed", "warn"))
    inv_bank = digits(inv["bank_account"])[-4:]
    if inv_bank:
        same = inv_bank == best["bank_last4"]
        details.append(d("Pay-to account", f"ending {inv_bank} " + ("matches vendor master" if same else f"does not match vendor master (ending {best['bank_last4']})"), "ok" if same else "fail"))
        if not same:
            signals.append(f"payment is requested to an account ending {inv_bank}, not the {best['bank_last4']} on file")
    if not exact:
        signals.insert(0, f"the name '{name}' is close to but not the same as '{best['name']}'")
    if re.search(r"bank details|new account|changed", inv.get("notes") or "", re.I):
        signals.append("the invoice itself announces changed bank details")

    hard = any("tax ID" in s or "account ending" in s for s in signals)
    if hard or (not exact and not inv_tax):
        c.find("vendor", "VENDOR_IDENTITY", HOLD, "Vendor identity does not match our records, possible impersonation",
               "This invoice looks like it is from " + best["name"] + " but: " + "; ".join(signals) + ". "
               f"Do not pay and do not update bank details from this document. Call the vendor on the number already on file ({best['contact_email']} is the contact on record) to confirm.",
               owner="Vendor master team")
        return WARN, "Identity mismatch against the vendor master", details
    return c.worst("vendor"), f"Matched to {best['name']}", details


# --- Stage 5: duplicates ---------------------------------------------------------
def stage_duplicate(c: Ctx):
    inv, details = c.inv, []
    cur = inv["currency"]
    same_file = db.runs_with_hash(c.file_hash, c.run_id)
    if same_file:
        p = same_file[0]
        c.find("duplicate", "DUPLICATE_FILE", REJECT, "Exact same file was already submitted",
               f"This file is byte-for-byte identical to the one processed in run {p['id']} (currently {p['final_state']}). Nothing new to pay.",
               vendor_msg=f"This invoice ({p['invoice_number'] or 'no number'}) was already received and is being handled. No need to resend it.")
        details.append(d("Same file seen before", f"yes, run {p['id']}", "fail"))
    else:
        details.append(d("Same file seen before", "no", "ok"))

    if not c.vendor:
        details.append(d("Invoice history", "skipped, vendor not identified", "warn"))
        return c.worst("duplicate"), "Checked file fingerprint only", details

    prior = [p for p in db.prior_runs(c.vendor["id"], c.run_id) if p["id"] not in {s["id"] for s in same_file}]
    number = norm_invoice_no(inv["invoice_number"])
    exact = [p for p in prior if number and p["invoice_number_norm"] == number]
    if exact:
        p = exact[0]
        c.find("duplicate", "DUPLICATE_NUMBER", REJECT, "Duplicate invoice number",
               f"{c.vendor['name']} invoice {inv['invoice_number']} was already processed in run {p['id']} ({p['final_state']}, {money(p['total_amount'], cur)}). Paying it again would be a double payment.",
               vendor_msg=f"Invoice {inv['invoice_number']} has already been received. Please do not resubmit it.")
        details.append(d("Same invoice number", f"yes, run {p['id']}", "fail"))
    else:
        details.append(d("Same invoice number", "no", "ok"))

    # Near duplicates: a reissued or re-keyed invoice gets a new number, so number
    # matching alone misses it. Same vendor + same amount + close dates is the tell.
    near = []
    for p in prior:
        if p in exact or inv["total"] is None or p["total_amount"] is None:
            continue
        if abs(p["total_amount"] - inv["total"]) > 0.01:
            continue
        pd_, cd = parse_date(p["invoice_date"]), getattr(c, "inv_date", None)
        gap = abs((pd_ - cd).days) if pd_ and cd else None
        if gap is None or gap <= R["near_duplicate_window_days"]:
            near.append((p, gap))
    if near:
        p, gap = near[0]
        related = number and p["invoice_number_norm"] and (number in p["invoice_number_norm"] or p["invoice_number_norm"] in number)
        c.find("duplicate", "PROBABLE_DUPLICATE", HOLD, "Probable duplicate of an invoice already processed",
               f"Different invoice number ({inv['invoice_number']} vs {p['invoice_number']}) but same vendor, same total {money(inv['total'], cur)}"
               + (f" and dates {gap} day{'s' if gap != 1 else ''} apart" if gap is not None else "")
               + (", and the numbers are variants of each other" if related else "")
               + f". Looks like run {p['id']} reissued. Confirm with the vendor whether this replaces the earlier invoice or is a genuinely separate charge.")
        details.append(d("Same vendor, amount and date", f"yes, run {p['id']} ({p['invoice_number']})", "fail"))
    else:
        details.append(d("Same vendor, amount and date", f"no match in {len(prior)} earlier invoice{'s' if len(prior) != 1 else ''} from this vendor", "ok"))

    status = c.worst("duplicate")
    return status, {OK: "No duplicates found", WARN: "Probable duplicate found", FAIL: "Duplicate found"}[status], details


# --- Stage 6: PO match -----------------------------------------------------------
def _score_po(c: Ctx, po):
    """How well does an open PO explain this invoice? Half amount fit, half description fit."""
    tol = min(R["tolerance_abs"], po["amount"] * R["tolerance_pct"] / 100)
    if c.net is None or po["remaining"] <= 0.005 or c.net > po["remaining"] + tol:
        amount = 0.0
    elif abs(c.net - po["remaining"]) <= tol:
        amount = 1.0          # invoice is for exactly what is left
    else:
        amount = 0.5          # fits inside the PO as a part-invoice
    inv_text = " ".join(li["description"] or "" for li in c.inv["line_items"])
    po_text = po["description"] + " " + " ".join(l["description"] for l in po["lines"])
    text = similarity(inv_text, po_text)
    return round(0.5 * amount + 0.5 * text, 2), amount, text


def stage_po_match(c: Ctx):
    inv, details = c.inv, []
    ref_digits = digits(inv["po_number"])
    ref = f"PO-{ref_digits}" if ref_digits else None

    if ref:
        details.append(d("PO stated on invoice", ref))
        po = db.purchase_order(ref)
        if not po:
            c.find("po_match", "PO_NOT_FOUND", HOLD, "PO number on the invoice does not exist",
                   f"{ref} is not in the procurement system. It may be mistyped. Ask the buyer or vendor for the correct PO.")
            details.append(d("Procurement system", "no such PO", "fail"))
            return WARN, f"{ref} not found", details
        if c.vendor and po["vendor_id"] != c.vendor["id"]:
            c.find("po_match", "PO_VENDOR_MISMATCH", HOLD, "PO belongs to a different vendor",
                   f"{ref} was issued to another vendor, not {c.vendor['name']}. The invoice cannot be paid against it.", owner=f"Buyer ({po['buyer']})")
            details.append(d("Procurement system", "PO was issued to a different vendor", "fail"))
            return WARN, f"{ref} belongs to another vendor", details
        c.po, c.po_match = po, "stated"
        details += [d("Procurement system", f"{po['description']}", "ok"), d("Buyer", po["buyer"])]
        return OK, f"Matched to {ref} as stated on the invoice", details

    details.append(d("PO stated on invoice", "none", "warn"))
    if not c.vendor:
        c.find("po_match", "NO_PO", HOLD, "No PO reference and vendor not identified", "There is nothing to match this invoice against.")
        return WARN, "Cannot infer a PO without a verified vendor", details

    # No PO printed: try to infer it from this vendor's open POs.
    candidates = sorted(((po, *_score_po(c, po)) for po in db.purchase_orders(c.vendor["id"]) if po["state"] != "closed"), key=lambda t: -t[1])
    if not candidates:
        c.find("po_match", "NO_PO", HOLD, "No purchase order covers this invoice",
               f"The invoice quotes no PO and {c.vendor['name']} has no open POs. Non-PO spend needs the budget owner's approval.", owner="Budget owner")
        return WARN, "No open PO for this vendor", details
    for po, score, amt, txt in candidates:
        details.append(d(f"Candidate {po['po_number']}", f"{po['description']}: score {score:.2f} (amount fit {amt:.1f}, description match {txt:.0%}, {money(po['remaining'])} left)"))
    best, score = candidates[0][0], candidates[0][1]
    margin = score - (candidates[1][1] if len(candidates) > 1 else 0)
    if score < R["implied_po_min_score"] or margin < R["implied_po_min_margin"]:
        c.find("po_match", "PO_AMBIGUOUS", HOLD, "Could not confidently work out which PO this invoice is for",
               f"No PO is quoted. Best guess is {best['po_number']} (score {score:.2f}) but that is not a clear enough winner to pay against. "
               "Pick the right PO or ask the vendor to quote it.", owner=f"Buyer ({best['buyer']})")
        return WARN, "PO could not be inferred with confidence", details

    c.po, c.po_match = best, "inferred"
    details.append(d("Inferred PO", f"{best['po_number']}, clear winner by {margin:.2f}", "ok"))
    if c.net is not None and c.net > R["implied_po_auto_limit"]:
        c.find("po_match", "PO_INFERRED_OVER_LIMIT", HOLD, "PO was inferred, and the amount is above the auto-approval limit for inferred matches",
               f"The invoice quotes no PO. The process matched it to {best['po_number']} by vendor, amount and description, but inferred matches above "
               f"{money(R['implied_po_auto_limit'])} need a person to confirm.", owner=f"Buyer ({best['buyer']})")
        return WARN, f"Inferred {best['po_number']}, needs confirmation at this value", details
    c.find("po_match", "PO_INFERRED", INFO, "PO was inferred, not stated",
           f"The invoice quotes no PO number. Matched to {best['po_number']} ({best['description']}) because it is this vendor's only open PO that fits both the amount and the description.")
    return OK, f"No PO on the invoice, inferred {best['po_number']}", details


# --- Stage 7: amounts ------------------------------------------------------------
def _po_line_usage(po, exclude_run):
    """Quantity already invoiced per PO line, from earlier approved invoices on this PO."""
    used = {l["line_no"]: 0.0 for l in po["lines"]}
    for prior in po["invoices"]:
        if prior["id"] == exclude_run:
            continue
        run = db.get_run(prior["id"], with_stages=False)
        for li in (run.get("extraction") or {}).get("line_items", []):
            line = _best_po_line(li, po)
            if line and li.get("quantity"):
                used[line["line_no"]] += li["quantity"]
    return used


def _best_po_line(li, po):
    scored = [(similarity(li.get("description"), l["description"]), l) for l in po["lines"]]
    score, line = max(scored, key=lambda t: t[0], default=(0, None))
    return line if score >= 0.5 else None


def stage_amounts(c: Ctx):
    inv, po, details = c.inv, c.po, []
    cur = inv["currency"]
    if inv["currency"] != po["currency"]:
        c.find("amounts", "CURRENCY", HOLD, "Invoice currency differs from the PO", f"Invoice is in {inv['currency']}, {po['po_number']} is in {po['currency']}.", owner=f"Buyer ({po['buyer']})")
    buyer = f"Buyer ({po['buyer']})"
    remaining, net = po["remaining"], c.net
    tol = round(min(R["tolerance_abs"], po["amount"] * R["tolerance_pct"] / 100), 2)
    details += [
        d("PO value", money(po["amount"], cur)),
        d("Already invoiced", money(po["invoiced"], cur) + (f" across {len(po['invoices'])} invoice{'s' if len(po['invoices']) != 1 else ''}" if po["invoices"] else "")),
        d("Remaining on PO", money(remaining, cur)),
        d("This invoice (before tax)", money(net, cur)),
        d("Tolerance", f"{money(tol, cur)} (lower of {R['tolerance_pct']:g}% of PO and {money(R['tolerance_abs'], cur)})"),
    ]
    c.po_after = None
    if net is None:
        return SKIP, "No amount to compare", details

    over = round(net - remaining, 2)
    if remaining <= 0.005:
        c.find("amounts", "PO_EXHAUSTED", HOLD, "PO has already been fully invoiced",
               f"{po['po_number']} ({money(po['amount'], cur)}) is fully used by {len(po['invoices'])} earlier invoice{'s' if len(po['invoices']) != 1 else ''}. "
               f"This {money(net, cur)} invoice has nothing left to draw on, it is either a duplicate or needs a PO amendment.", owner=buyer)
        details.append(d("Result", "nothing left on the PO", "fail"))
    elif over > tol:
        c.find("amounts", "OVER_PO", HOLD, "Invoice exceeds what is left on the PO",
               f"Invoice is {money(net, cur)} before tax but only {money(remaining, cur)} remains on {po['po_number']} "
               f"({money(po['invoiced'], cur)} of {money(po['amount'], cur)} already invoiced). Over by {money(over, cur)} ({over / po['amount']:.1%} of the PO), "
               f"beyond the {money(tol, cur)} tolerance. The buyer must either amend the PO or dispute the excess.", owner=buyer)
        details.append(d("Result", f"over by {money(over, cur)}, beyond tolerance", "fail"))
    elif over > 0.005:
        c.find("amounts", "WITHIN_TOLERANCE", INFO, "Small overage accepted within tolerance",
               f"Invoice is {money(over, cur)} over the remaining PO balance, inside the {money(tol, cur)} tolerance, so it is paid without chasing.")
        c.po_after = 0.0
        details.append(d("Result", f"over by {money(over, cur)}, within tolerance", "ok"))
    else:
        c.po_after = round(remaining - net, 2)
        if c.po_after > tol:
            c.find("amounts", "PARTIAL", INFO, "Part-invoice against the PO",
                   f"This invoice uses {money(net, cur)} of the {money(remaining, cur)} remaining on {po['po_number']}. The PO stays open with {money(c.po_after, cur)} left for later invoices.")
            details.append(d("Result", f"part-invoice, {money(c.po_after, cur)} stays open", "ok"))
        else:
            details.append(d("Result", "invoice fully consumes the PO", "ok"))

    # Third leg of the match: has what is being billed actually arrived?
    received_open = round(po["received_value"] - po["invoiced"], 2)
    details.append(d("Received, not yet invoiced", f"{money(received_open, cur)} ({money(po['received_value'], cur)} received in total)"))
    not_received = remaining > 0.005 and net > received_open + tol   # an exhausted PO is already reported above
    if not_received:
        c.find("amounts", "NOT_RECEIVED", HOLD, "Invoice bills for more than has been received",
               f"Invoice is {money(net, cur)} before tax, but goods and services received and not yet invoiced on {po['po_number']} come to "
               f"{money(max(received_open, 0), cur)}. Paying now would mean paying for something that has not arrived. "
               "Confirm delivery with receiving, or pay only the received part.", owner=buyer)
        details.append(d("Goods received", f"billing is {money(round(net - received_open, 2), cur)} ahead of receipts", "fail"))
    else:
        details.append(d("Goods received", "everything billed has been received", "ok"))

    # Line level: unit prices and quantities, when the invoice is itemised.
    used = _po_line_usage(po, c.run_id)
    for li in inv["line_items"]:
        line = _best_po_line(li, po)
        if not line or li["unit_price"] is None:
            continue
        diff = (li["unit_price"] - line["unit_price"]) / line["unit_price"] * 100
        qty_left = line["qty"] - used[line["line_no"]]
        bits, bad = [], False
        if abs(diff) > R["unit_price_tolerance_pct"]:
            bad = True
            bits.append(f"price {money(li['unit_price'], cur)} vs {money(line['unit_price'], cur)} on PO ({diff:+.1f}%)")
            c.find("amounts", "PRICE_VARIANCE", HOLD, "Unit price differs from the PO",
                   f"'{li['description']}' is billed at {money(li['unit_price'], cur)} per unit, the PO agreed {money(line['unit_price'], cur)} ({diff:+.1f}%).", owner=buyer)
        else:
            bits.append("price matches PO")
        if li["quantity"] is not None:
            if li["quantity"] > qty_left + 1e-9:
                bad = True
                bits.append(f"{li['quantity']:g} billed but only {qty_left:g} of {line['qty']:g} still open on the PO")
                for f in c.findings:
                    if f["code"] == "OVER_PO":
                        f["message"] += f" At line level: {li['quantity']:g} x '{line['description']}' billed, but only {qty_left:g} of the {line['qty']:g} ordered are still un-invoiced."
            else:
                bits.append(f"{li['quantity']:g} of {qty_left:g} open units")
            rec_left = line["received"] - used[line["line_no"]]
            if li["quantity"] > rec_left + 1e-9:
                bad = True
                bits.append(f"only {rec_left:g} received and not yet invoiced")
                for f in c.findings:
                    if f["code"] == "NOT_RECEIVED":
                        f["message"] += f" At line level: {li['quantity']:g} x '{line['description']}' billed, {rec_left:g} received and not yet invoiced ({line['receipts'] or 'no receipt'})."
            else:
                bits.append(f"received ({line['receipts']})")
        details.append(d(f"Line: {li['description']}", ", ".join(bits), "fail" if bad else "ok"))

    # Tax sanity against the rate the buyer expects.
    tax = inv["tax_amount"] if inv["tax_amount"] is not None else getattr(c, "tax_derived", None)
    if tax is not None:
        expected = round(net * po["tax_rate"] / 100, 2)
        if abs(tax - expected) > max(1.0, expected * 0.01):
            c.find("amounts", "TAX_MISMATCH", HOLD, "Tax charged differs from the rate expected on this PO",
                   f"Invoice charges {money(tax, cur)} tax, {po['tax_rate']:g}% of {money(net, cur)} would be {money(expected, cur)}.")
            details.append(d("Tax", f"{money(tax, cur)} charged, {money(expected, cur)} expected at {po['tax_rate']:g}%", "fail"))
        else:
            details.append(d("Tax", f"{money(tax, cur)}, consistent with {po['tax_rate']:g}% on the PO", "ok"))

    status = c.worst("amounts")
    return status, {OK: "Within the PO balance, and everything billed has been received", WARN: "Invoice does not fit the PO or the goods received"}.get(status, "Amount problems"), details


# --- Stage 8: decide -------------------------------------------------------------
def stage_decide(c: Ctx):
    inv = c.inv or {}
    cur = inv.get("currency") or "USD"
    rejects = [f for f in c.findings if f["severity"] == REJECT]
    holds = [f for f in c.findings if f["severity"] == HOLD]
    notes = [f for f in c.findings if f["severity"] == INFO]

    if rejects:
        decision, lead = "REJECTED", rejects[0]
    elif holds:
        decision, lead = "HOLD", holds[0]
    else:
        decision, lead = "APPROVED", None

    result = {"decision": decision, "reasons": rejects + holds, "notes": notes, "messages": [], "corrections": c.corrections}
    vendor_name = (c.vendor or {}).get("name") or inv.get("vendor_name") or "the vendor"
    inv_label = inv.get("invoice_number") or "(no invoice number)"

    if decision == "APPROVED":
        pay = inv["total"]
        po = c.po
        headline = f"Cleared for payment: {money(pay, cur)} to {vendor_name}"
        owner = "Payments (no review needed)"
        passed = [
            f"Vendor verified against the vendor master ({c.vendor['id']}), tax ID and pay-to account consistent with our records.",
            "No duplicate: file, invoice number and vendor/amount/date all checked against history.",
            f"{po['po_number']} was {'stated on the invoice' if c.po_match == 'stated' else 'inferred by the process'}; "
            f"{money(c.net, cur)} before tax fits the {money(po['remaining'], cur)} remaining.",
            "3-way match: everything billed has been received and not invoiced before.",
        ]
        result.update(passed=passed, next_action=f"Schedule payment of {money(pay, cur)}" + (f" by the due date {inv['due_date']}." if inv.get("due_date") else " on standard terms."),
                      po_impact=f"{po['po_number']}: {money(po['remaining'], cur)} remaining before, {money(c.po_after or 0, cur)} after" + (" (PO closed)." if not c.po_after else " (PO stays open)."))
    else:
        headline, owner = lead["title"], lead["owner"]
        owners = list(dict.fromkeys(f["owner"] for f in rejects + holds))
        if decision == "HOLD":
            result["next_action"] = f"Routed to {owner} for review. Payment is blocked until someone approves or rejects it in the dashboard."
            body = (f"Invoice {inv_label} from {vendor_name} for {money(inv.get('total'), cur)} is on hold and needs your decision.\n\n"
                    + "\n".join(f"- {f['title']}: {f['message']}" for f in holds)
                    + "\n\nApprove or reject it in Invoice Desk. Nothing will be paid until you do.")
            result["messages"].append({"kind": "internal", "to": ", ".join(owners), "subject": f"On hold: {vendor_name} invoice {inv_label}", "body": body})
        else:
            result["next_action"] = "Not entered for payment." + (" A reply to the vendor has been drafted for a person to send." if any(f["vendor_msg"] for f in rejects) else "")
            vmsgs = [f["vendor_msg"] for f in rejects if f["vendor_msg"]]
            if vmsgs:
                to = (c.vendor or {}).get("contact_email") or "vendor contact (not on file)"
                ref = f"invoice {inv_label}" if inv.get("invoice_number") else f"the document '{c.filename}'" + (f" referencing {inv['po_number']}" if inv.get("po_number") else "")
                body = (f"Hello,\n\nThank you for sending {ref}"
                        + (f" for {money(inv.get('total'), cur)}" if inv.get("total") is not None else "")
                        + ". We could not process it:\n\n" + "\n".join(f"- {m}" for m in vmsgs)
                        + "\n\nOnce we have a corrected invoice we will process it straight away.\n\nAccounts Payable\nNorthfield Manufacturing Inc.")
                result["messages"].append({"kind": "vendor", "to": to, "subject": f"Unable to process {ref}", "body": body})

    details = [d("Decision", decision, {"APPROVED": "ok", "HOLD": "warn", "REJECTED": "fail"}[decision]), d("Owner", owner)]
    details += [d("Reason", f["title"], "fail" if f["severity"] == REJECT else "warn") for f in rejects + holds]
    details += [d("Note", f["title"]) for f in notes]

    db.update_run(
        c.run_id, decision=decision, final_state=decision, headline=headline, owner=owner,
        findings=c.findings, result=result, extraction=c.inv,
        vendor_id=(c.vendor or {}).get("id"), vendor_name=(c.vendor or {}).get("name") or inv.get("vendor_name"),
        invoice_number=inv.get("invoice_number"), invoice_number_norm=norm_invoice_no(inv.get("invoice_number")),
        invoice_date=inv.get("invoice_date"), po_number=(c.po or {}).get("po_number"), currency=cur,
        net_amount=c.net, tax_amount=inv.get("tax_amount") if inv.get("tax_amount") is not None else getattr(c, "tax_derived", None),
        total_amount=inv.get("total"),
    )
    status = {"APPROVED": OK, "HOLD": WARN, "REJECTED": FAIL}[decision]
    return status, headline, details


# --- Runner ----------------------------------------------------------------------
def run_pipeline(run_id, data, filename, mime, override=None, corrections=None):
    started = time.perf_counter()
    c = Ctx(run_id, data, filename, mime, override, corrections)
    pool = ThreadPoolExecutor(max_workers=1)
    if override is None:
        # Start the slow step (the AI read) immediately, so it overlaps with intake.
        c.future = pool.submit(extract, c.data, c.mime, c.text)
    fns = {"intake": stage_intake, "extract": stage_extract, "validate": stage_validate, "vendor": stage_vendor,
           "duplicate": stage_duplicate, "po_match": stage_po_match, "amounts": stage_amounts, "decide": stage_decide}
    for seq, (key, title) in enumerate(STAGES, 1):
        db.upsert_stage(run_id, seq, key, title, "pending")
    try:
        for seq, (key, title) in enumerate(STAGES, 1):
            if key != "decide" and c.stop:
                db.upsert_stage(run_id, seq, key, title, SKIP, f"Skipped because {c.stop}")
                continue
            if key == "amounts" and not c.po:
                db.upsert_stage(run_id, seq, key, title, SKIP, "Skipped because no PO was matched")
                continue
            db.upsert_stage(run_id, seq, key, title, "running")
            t0 = time.perf_counter()
            status, summary, details = fns[key](c)
            ms = int((time.perf_counter() - t0) * 1000)
            if key == "extract" and c.meta.get("ms") is not None:
                ms = c.meta["ms"]   # true read time, including the part that overlapped intake
            pause = config.STAGE_PACING_MS / 1000 - (time.perf_counter() - t0)
            if pause > 0:
                time.sleep(pause)
            db.upsert_stage(run_id, seq, key, title, status, summary, details, ms)
        db.update_run(run_id, status="done", duration_ms=int((time.perf_counter() - started) * 1000))
    except Exception as e:  # never leave a run spinning: fail loudly and visibly
        traceback.print_exc()
        db.upsert_stage(run_id, seq, key, title, FAIL, f"Unexpected error: {type(e).__name__}: {e}")
        db.update_run(run_id, status="error", decision="HOLD", final_state="HOLD", owner="AP analyst",
                      headline="Process error, handle this invoice manually",
                      result={"decision": "HOLD", "reasons": [{"title": "Process error", "message": f"{type(e).__name__}: {e}", "severity": HOLD, "owner": "AP analyst"}], "notes": [], "messages": [],
                              "next_action": "The process failed on this document. Handle it manually and report the error."},
                      duration_ms=int((time.perf_counter() - started) * 1000))
    finally:
        pool.shutdown(wait=False)


def review(run_id, action, reviewer, note):
    """A person resolves a held invoice. The process's own decision is kept for the audit trail."""
    run = db.get_run(run_id, with_stages=False)
    if not run or run["final_state"] != "HOLD":
        return None
    final = "APPROVED" if action == "approve" else "REJECTED"
    db.update_run(run_id, final_state=final, review={
        "action": action, "reviewer": reviewer or "Reviewer", "note": note or "",
        "at": datetime.now().isoformat(timespec="seconds"),
    })
    return db.get_run(run_id)


EDITABLE = {"vendor_name": "vendor name", "invoice_number": "invoice number", "invoice_date": "invoice date", "po_number": "PO number",
            "subtotal": "subtotal", "tax_amount": "tax", "total": "total"}


def rerun(run_id, fields, reviewer, note):
    """A person corrects what was read (or keys in what was missing) and the rules decide again.

    The AI read is not repeated. The correction, who made it and why are kept on the run.
    Returns the run id, or a string explaining why nothing was done.
    """
    run, f = db.get_run(run_id, with_stages=False), db.run_file(run_id)
    if not run or not f:
        return "Run not found"
    if run["status"] == "running" or run["review"] or run["final_state"] not in ("HOLD", "REJECTED"):
        return "Only invoices that are on hold or rejected, and not yet reviewed, can be corrected"
    base = normalise(run["extraction"] or {})
    proposed = normalise({**base, **{k: v for k, v in fields.items() if k in EDITABLE}, "is_invoice": True})
    changes = [{"field": EDITABLE[k], "from": base[k], "to": proposed[k]} for k in EDITABLE if base[k] != proposed[k]]
    if not changes:
        return "Nothing was changed"
    corrections = (run["corrections"] or []) + [{"by": reviewer, "note": note, "at": datetime.now().isoformat(timespec="seconds"), "changes": changes}]
    db.clear_stages(run_id)
    db.update_run(run_id, status="running", decision=None, final_state=None, headline=None, owner=None, findings=None, result=None, corrections=corrections)
    data = Path(f["file_path"]).read_bytes()
    threading.Thread(target=run_pipeline, args=(run_id, data, run["filename"], run["mime"], proposed, corrections), daemon=True).start()
    return None
