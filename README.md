# Invoice Desk — Zamp ASA case study, PS-1 (invoice processing)

Takes a vendor invoice (PDF or image) and produces a reasoned decision:
**Approved**, **On hold** (routed to a named owner), or **Rejected** (with a drafted reply to the vendor).
Every stage is visible live, and every run is kept in a dashboard.

## Run it

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env        # paste a free Gemini key from https://aistudio.google.com/apikey
.venv/bin/uvicorn app.main:app --port 8000
```

Open http://localhost:8000. Tests: `.venv/bin/pip install -r requirements-dev.txt && .venv/bin/python -m pytest -q`.

Without a key the app still runs: digital PDFs are read by an offline parser, scanned invoices are put on hold.

## Process map

| # | Stage | What it does | Can cause |
|---|-------|--------------|-----------|
| 1 | Receive document | Fingerprint the file, detect digital vs scanned | – |
| 2 | Read invoice | Gemini reads the document into fields. Cross-checked against the PDF's own text | Hold (unreadable, not an invoice, value not in document) |
| 3 | Validate | Required fields, the invoice's own arithmetic, dates, back out embedded tax | Reject (missing info), Hold (maths, dates) |
| 4 | Verify vendor | Match name to vendor master, compare tax ID and bank account | Hold (unknown, identity mismatch), Reject (blocked) |
| 5 | Duplicates | Same file, same invoice number, or same vendor + amount + date under a new number | Reject (exact), Hold (probable) |
| 6 | Match PO | Use the PO printed on the invoice, or infer it from the vendor's open POs | Hold (not found, wrong vendor, ambiguous) |
| 7 | Amounts | Compare with what is *left* on the PO, unit prices, quantities, tax | Hold (over PO, price, tax) |
| 8 | Decide | Most severe finding wins. Names an owner and the next step | – |

Stages do not stop at the first problem, so a reviewer sees everything wrong in one go.

## Design decisions

- **The AI reads, rules decide.** The model only turns a document into fields. Every decision is a plain rule in `app/pipeline.py`, with thresholds in `app/config.py`. Same invoice, same answer, and the reason is always explainable.
- **Don't trust the read blindly.** For digital PDFs the invoice number and total returned by the model must appear verbatim in the PDF text. Arithmetic is re-checked.
- **Three outcomes, not two.** Reject is for things only the vendor can fix or that must never be paid. Hold is for things a person inside the company can resolve. Each hold names who.
- **Balance, not PO value.** Invoices are compared with the remaining PO balance, which is what makes split POs work.
- **Held invoices do not consume PO balance** until a named person approves with a reason. The process's decision and the human's are both kept.
- **Nothing is sent.** Vendor replies are drafts for a person to send.
- **It degrades instead of breaking.** Model down → offline parser for digital PDFs → hold for manual keying. A crash in any stage becomes a visible hold.

## Edge cases (the four I designed)

1. **Split PO.** `02` bills 10 of 16 units → approved as a part-invoice, PO stays open. `03` then bills 7 when 6 remain → held for the buyer with the exact overage.
2. **Scanned invoice, no PO number, tax buried in the total.** `04` → vision model reads it, tax is backed out, PO is inferred from the vendor's two open POs by amount and description. Auto-approves only under $10,000 and with a clear winner.
3. **Reissued duplicate.** `05` is the happy-path invoice with a new number (`INV-20831-R`). Number matching misses it; vendor + amount + date catches it.
4. **Look-alike vendor with new bank details.** `06` has a near-identical name, transposed tax ID and a different account → held as possible impersonation, with a call-back instruction.

Also handled: missing invoice number/date (`07`, rejected with a drafted reply), exact re-upload, blocked vendor, unknown PO, PO of another vendor, price variance, small overage within tolerance.

## Assumptions

- PO amounts are net of tax; one currency per PO.
- Tolerance is the lower of 2% of the PO and $250.
- No goods-receipt data, so this is a 2-way match (invoice vs PO). A 3-way match is the next thing I would add.
- The live view holds each stage on screen for at least 0.45 s (`STAGE_PACING_MS`) so it can be followed; the real compute time is the figure shown per stage.
- Demo data (vendors, POs) is seeded in `app/seed_data.py`. "Reset demo data" restores it.

## Suggested demo order (5 minutes)

1. `01` Happy path → approved. Open a stage to show the checks.
2. `02` then `03` Split PO → approved part-invoice, then held. Show the PO balance on the reference tab.
3. `05` Reissued duplicate → held, points at the original run.
4. `06` Look-alike vendor → held for the vendor master team.
5. `04` Scanned, no PO → approved with the PO inferred (needs the Gemini key).
6. Dashboard: KPIs, filter "Needs review", approve `03` as the buyer with a reason.

Press "Reset demo data" before each rehearsal.

## Deploy (Render, free)

Push this folder to a GitHub repo, create a Render Blueprint from it (`render.yaml` is included), and set `GEMINI_API_KEY` in the service's environment. Free instances sleep when idle and lose run history on restart, so open the link a minute before a demo.

## Built with

FastAPI, SQLite, vanilla JS, Gemini API for reading documents. Built with Claude Code as the coding assistant.
