"""Turn an invoice document into structured fields.

Design choice: the AI model only READS the document. It never decides anything.
Every approve / hold / reject decision is made by explicit rules in pipeline.py,
so a decision can always be explained and reproduced.

Two extractors:
  1. Gemini (vision + text) - handles any layout, including scanned images.
  2. Offline text parser    - regex over the PDF text layer. Used when no API key
     is configured, or as an automatic fallback if the model call fails mid-demo.
"""
import base64
import io
import json
import re
import time

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

import httpx
from pypdf import PdfReader

from . import config

FIELDS = (
    "vendor_name", "vendor_tax_id", "bank_account", "invoice_number", "invoice_date", "due_date",
    "po_number", "currency", "subtotal", "tax_amount", "tax_rate", "total",
)

PROMPT = """You are reading a vendor invoice for an accounts-payable team.
Extract exactly what is printed on the document into JSON with these keys:

  is_invoice            boolean - false if this is not an invoice / bill / statement requesting payment
  vendor_name           the company that ISSUED the invoice (not the customer being billed)
  vendor_tax_id         the vendor's tax ID / registration number
  bank_account          the bank account number or "ending NNNN" digits the vendor asks to be paid into
  invoice_number        the invoice number / reference
  invoice_date          the invoice issue date as YYYY-MM-DD
  due_date              payment due date as YYYY-MM-DD
  po_number             the customer's purchase order number, only if one is printed
  currency              3-letter ISO code
  line_items            array of {description, quantity, unit_price, amount}
  subtotal              amount before tax, only if printed separately
  tax_amount            tax amount, only if printed as its own figure
  tax_rate              tax rate in percent, if printed anywhere (e.g. 8)
  tax_included_in_total true if the only total shown already includes tax
  total                 the final amount payable
  notes                 any unusual statements on the document (e.g. changed bank details, "revised copy")

Rules:
- Use null for anything that is not printed. NEVER guess, infer or invent a value.
- A stamp such as "RECEIVED 05 NOV" is not the invoice date.
- Numbers must be plain numbers without currency symbols or thousands separators.
- Return only the JSON object."""


class ExtractionError(Exception):
    pass


def pdf_text(data: bytes) -> tuple[str, int]:
    """Return (text layer, page count). Empty text means the PDF is a scan."""
    try:
        reader = PdfReader(io.BytesIO(data))
        text = "\n".join((p.extract_text(extraction_mode="layout") or "") for p in reader.pages)
        return text.strip(), len(reader.pages)
    except Exception:
        return "", 0


def _num(v):
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return round(float(v), 2)
    s = re.sub(r"[^\d.\-]", "", str(v))
    try:
        return round(float(s), 2)
    except ValueError:
        return None


def _clean(v):
    if v is None:
        return None
    s = str(v).strip()
    return s if s and s.lower() not in ("null", "none", "n/a", "-") else None


def normalise(raw: dict) -> dict:
    """Coerce whatever the extractor returned into one predictable shape."""
    out = {"is_invoice": raw.get("is_invoice", True) is not False}
    for k in ("vendor_name", "vendor_tax_id", "bank_account", "invoice_number", "invoice_date", "due_date", "po_number", "currency", "notes"):
        out[k] = _clean(raw.get(k))
    for k in ("subtotal", "tax_amount", "tax_rate", "total"):
        out[k] = _num(raw.get(k))
    out["tax_included_in_total"] = bool(raw.get("tax_included_in_total"))
    items = []
    for li in raw.get("line_items") or []:
        if not isinstance(li, dict):
            continue
        item = {"description": _clean(li.get("description")), "quantity": _num(li.get("quantity")),
                "unit_price": _num(li.get("unit_price")), "amount": _num(li.get("amount"))}
        if item["description"] or item["amount"] is not None:
            items.append(item)
    out["line_items"] = items
    if out["currency"]:
        out["currency"] = out["currency"].upper()
    return out


# --- Extractor 1: Gemini -------------------------------------------------------
_preferred = []  # the model that answered last time is tried first next time


def _call(client, model, data, mime, thinking_off):
    gen = {"temperature": 0, "responseMimeType": "application/json"}
    if thinking_off:
        # Reading fields off a page needs no deliberation; this cuts ~10 s to ~2 s.
        gen["thinkingConfig"] = {"thinkingBudget": 0}
    body = {
        "contents": [{"parts": [
            {"inline_data": {"mime_type": mime, "data": base64.b64encode(data).decode()}},
            {"text": PROMPT},
        ]}],
        "generationConfig": gen,
    }
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    return client.post(url, json=body, headers={"x-goog-api-key": config.GEMINI_API_KEY})


def _read(model, data, mime) -> dict:
    """One model reads the document once. Raises ExtractionError with the reason on any failure."""
    try:
        with httpx.Client(timeout=httpx.Timeout(config.GEMINI_TIMEOUT_S, connect=5)) as client:
            r = _call(client, model, data, mime, thinking_off=True)
            if r.status_code == 400:  # some models reject the thinking setting
                r = _call(client, model, data, mime, thinking_off=False)
    except httpx.HTTPError as e:
        raise ExtractionError(f"{model}: no answer within {config.GEMINI_TIMEOUT_S:g} s ({type(e).__name__})")
    if r.status_code != 200:
        try:
            msg = r.json().get("error", {}).get("message", "")[:160]
        except Exception:
            msg = r.text[:160]
        raise ExtractionError(f"{model}: HTTP {r.status_code} {msg}")
    try:
        text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
        text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
        parsed = json.loads(text)
        return normalise(parsed[0] if isinstance(parsed, list) else parsed)
    except Exception as e:
        raise ExtractionError(f"{model}: unreadable response ({type(e).__name__})")


def gemini_extract(data: bytes, mime: str, want_second: bool = False):
    """Ask two models at the same time.

    - Digital PDF: take whichever answers first (one slow or overloaded model no
      longer stalls the run). The answer is cross-checked against the PDF text later.
    - Scan (want_second): wait for both, so the two independent reads can be compared.

    Returns (fields, model, second_fields_or_None, second_model_or_None).
    """
    models = _preferred + [m for m in config.GEMINI_MODELS if m not in _preferred]
    if not models:
        raise ExtractionError("no model configured")
    errors, good, deadline = [], [], None   # good: [(model, fields)] in order of arrival
    pool = ThreadPoolExecutor(max_workers=2)
    try:
        queue = list(models)
        running = {}
        while (queue or running) and len(good) < (2 if want_second else 1):
            while queue and len(running) < 2:
                m = queue.pop(0)
                running[pool.submit(_read, m, data, mime)] = m
            # Once one read is in, the second opinion gets a short grace period, not the full timeout.
            left = None if deadline is None else max(0.0, deadline - time.perf_counter())
            done, _ = wait(running, timeout=left, return_when=FIRST_COMPLETED)
            if not done:
                break
            for fut in done:
                m = running.pop(fut)
                try:
                    good.append((m, fut.result()))
                    deadline = deadline or time.perf_counter() + config.SECOND_READ_GRACE_S
                except ExtractionError as e:
                    errors.append(str(e))
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    if not good:
        raise ExtractionError("; ".join(errors) or "no model answered")
    _preferred[:] = [m for m, _ in good]
    (model, fields), second = good[0], (good[1] if len(good) > 1 else (None, None))
    return fields, model, second[1], second[0]


# --- Extractor 2: offline text parser -----------------------------------------
AMT = r"\$?\s*([\d,]+\.\d{2})"
TOTAL_LABELS = r"\b(?:amount due|total payable|balance due|total due|grand total|total)"
SKIP_LINE = re.compile(r"subtotal|net amount|tax|total|balance|amount due", re.I)


def _find(pattern, text, group=1, flags=re.I | re.M):
    m = re.search(pattern, text, flags)
    return m.group(group).strip() if m else None


def text_extract(text: str) -> dict:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    vendor = re.split(r"\s{2,}", lines[0].strip())[0] if lines else None

    inv = _find(r"(?:invoice\s*(?:#|no\.?|number)|inv\.?\s*no\.?)\s*:?\s+([A-Z0-9][A-Z0-9/\-]*\d[A-Z0-9/\-]*)", text)
    po = _find(r"\b(PO[-\s]?\d{3,})\b", text)
    tax_m = re.search(r"(?:sales tax|tax at|vat|tax)\s*\(?\s*([\d.]+)\s*%\)?\s+" + AMT, text, re.I)

    items = []
    for ln in lines:
        m = re.match(r"^\s*(.+?)\s{2,}(\d+(?:\.\d+)?)\s{2,}" + AMT + r"\s{2,}" + AMT + r"\s*$", ln)
        if m:
            items.append({"description": m.group(1), "quantity": m.group(2), "unit_price": m.group(3), "amount": m.group(4)})
            continue
        m = re.match(r"^\s*(.+?)\s{2,}" + AMT + r"\s*$", ln)
        if m and not SKIP_LINE.search(m.group(1)):
            items.append({"description": m.group(1), "quantity": None, "unit_price": None, "amount": m.group(2)})

    notes = []
    for pat in (r"(notice:.*)", r"(revised copy)", r"(bank details have changed[^.\n]*)"):
        hit = _find(pat, text)
        if hit and hit not in " ".join(notes):
            notes.append(hit)

    return normalise({
        "is_invoice": bool(re.search(r"invoice|statement|amount due|balance due", text, re.I)),
        "vendor_name": vendor,
        "vendor_tax_id": _find(r"tax\s*(?:id|registration no\.?)\s*:?\s*([\d\-]{6,})", text),
        "bank_account": _find(r"account\s+(?:ending|no\.?|number)\s*:?\s*(\d{4,})", text),
        "invoice_number": inv,
        "invoice_date": _find(r"(?:invoice date|issue date|date)\s*:?\s+(\d{4}-\d{2}-\d{2})", text),
        "due_date": _find(r"(?:due date|payment due)\s*:?\s+(\d{4}-\d{2}-\d{2})", text),
        "po_number": po,
        "currency": "USD" if re.search(r"\$|USD", text) else None,
        "line_items": items,
        "subtotal": _find(r"(?:subtotal|net amount)\s+" + AMT, text),
        "tax_amount": tax_m.group(2) if tax_m else None,
        "tax_rate": tax_m.group(1) if tax_m else None,
        "total": _find(TOTAL_LABELS + r"[^\n$\d]*" + AMT, text),
        "notes": "; ".join(notes) or None,
    })


# --- Orchestration --------------------------------------------------------------
def extract(data: bytes, mime: str, text: str) -> tuple[dict, dict]:
    """Return (fields, meta). meta says which extractor ran, how long it took and why."""
    started = time.perf_counter()
    meta = {"extractor": None, "model": None, "fallback_reason": None, "second": None, "second_model": None, "second_wanted": False}
    done = lambda: meta.update(ms=int((time.perf_counter() - started) * 1000))
    if config.GEMINI_API_KEY:
        try:
            # A scan has no text layer to check the model against, so get two independent reads.
            meta["second_wanted"] = not text
            fields, model, second, second_model = gemini_extract(data, mime, want_second=not text)
            meta.update(extractor="gemini", model=model, second=second, second_model=second_model)
            done()
            return fields, meta
        except ExtractionError as e:
            meta["fallback_reason"] = f"AI model unavailable ({e})"
    else:
        meta["fallback_reason"] = "No GEMINI_API_KEY configured"

    if text:
        meta["extractor"] = "text-parser"
        fields = text_extract(text)
        done()
        return fields, meta
    raise ExtractionError(
        f"{meta['fallback_reason']}. This document is a scanned image with no text layer, "
        "so the offline parser cannot read it."
    )
