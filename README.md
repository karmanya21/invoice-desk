# Invoice Desk — Zamp ASA case study, PS-1 (invoice processing)

Takes a vendor invoice (PDF or image) and produces a reasoned decision:
**Approved**, **On hold** (a specific question to a named owner, with a recommended answer), or **Rejected** (with a drafted reply to the vendor).
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
| 7 | 3-way match | Compare with what is *left* on the PO and what has been *received*, unit prices, quantities, tax | Hold (over PO, not received, price, tax) |
| 8 | Decide | Most severe finding wins. A hold becomes a question for a named owner | Hold (above approval limit) |

After the decision, three things can move a held invoice without anyone re-submitting it:
a person **answers the question**, a person **corrects a field**, or the **data changes** (a goods receipt is recorded, a rule is changed, a vendor name is confirmed) and the process re-checks every open hold by itself.

Stages do not stop at the first problem, so a reviewer sees everything wrong in one go.

## Design decisions

- **The AI reads, rules decide.** The model only turns a document into fields. Every decision is a plain rule in `app/pipeline.py`, with thresholds in `app/config.py`. Same invoice, same answer, and the reason is always explainable.
- **Don't trust the read blindly.** For digital PDFs the invoice number and total returned by the model must appear verbatim in the PDF text. A scan has no text to check against, so two models read it independently and any disagreement on a payment-relevant field is a hold. Arithmetic is re-checked either way.
- **Pay only for what arrived.** Goods receipts are the third leg of the match: billing ahead of receipts is held even when it fits the PO.
- **A person can correct the read, not the rules.** On a held or rejected invoice a reviewer can fix a misread field or key in a missing one. The rules then decide again, and who changed what and why is kept on the run. A corrected invoice no longer counts as touchless.
- **Fast by doing the slow thing first.** The model read starts the moment the file arrives, overlapping intake, and a slow or busy model is abandoned after 6 seconds for the next one. A run takes about 3 seconds, nearly all of it the read.
- **Quota-aware.** One model request per digital invoice, two per scan. The Gemini free tier allows only a small number of requests per model per day (20 on `gemini-3.6-flash` when tested), and quotas are counted per model, so the reader keeps a pool of five models, cheapest first. A model that reports its quota is used up is skipped for an hour, and if every model is out the offline parser takes over for digital PDFs (the page says so when that happens).
- **A hold is a question, not a queue.** Every hold names who must answer, asks one specific question ("Does INV-20831-R replace INV-20831?"), recommends an answer and offers the answers as buttons. Unanswered holds show how long they have waited and when they escalate (48 hours by default). Escalation is shown, not sent: there is no notification system behind it.
- **Holds resume by themselves.** If an invoice is waiting on a goods receipt, recording the receipt clears it with no one touching the invoice. A re-check that changes nothing leaves no trace. Doubts about the read itself (two models disagreeing) are carried through a re-check, never dropped.
- **It learns from answers.** If a known vendor bills under an unfamiliar name but the same tax ID, the process asks once whether it is the same company. A "yes" is saved to the vendor master and logged, so it is never asked again.
- **The process owner owns the thresholds.** Tolerances, limits and the escalation window are editable in the UI with a name attached; each change is logged and every held invoice is re-checked against it. No code change.
- **Touchless must be earned.** Above the approval limit ($50,000 by default) even a clean invoice needs the finance controller's sign-off.
- **What is billed must be on the PO.** Fitting inside the remaining balance is not enough: unrelated items, extra units, credit notes and zero-value documents are all stopped.
- **One invoice commits at a time.** History and PO-balance checks run under a lock, so two invoices arriving together cannot both draw on the same balance. (One process, one lock; several servers would need this in the database.)
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

Two more samples show what happens *after* a hold:

- **Billed before delivery** (`08`): two months of storage billed, one confirmed. Held with the question "has the rest been delivered?". Record the receipt on the reference tab and the invoice approves itself.
- **Unfamiliar vendor name** (`09`): "BW Hosting" with Brightwave's tax ID and bank account. Answer "yes, same company" once; the name is remembered and the invoice is re-checked and approved.

Also handled (covered by tests, no sample): missing invoice number/date (`07` is the sample), exact re-upload, blocked vendor, unknown PO, PO of another vendor, price variance, small overage or price difference within tolerance, items not on the PO, more units than ordered, credit notes, zero-value invoices, a tax-inclusive total with no rate printed, a clean invoice above the approval limit, and two invoices for the same PO arriving at the same moment.

## Assumptions

- PO amounts are net of tax; one currency per PO.
- Tolerance is the lower of 2% of the PO and $250.
- Goods receipts are seeded demo data (`GOODS_RECEIPTS` in `app/seed_data.py`); a real deployment would read them from the receiving system.
- If the second read of a scan does not arrive within 3 seconds, the single read is used and the run says so. It still has to pass every rule.
- "Manual work avoided" assumes 8 minutes to handle an invoice by hand and 3 minutes to decide a prepared hold (`IMPACT` in `app/config.py`).
- The live view holds each stage on screen for at least 0.15 s (`STAGE_PACING_MS`) so it can be followed; the real compute time is the figure shown per stage.
- Demo data (vendors, POs) is seeded in `app/seed_data.py`. "Reset demo data" restores it.

## Suggested demo order (5 minutes)

1. `01` Happy path → approved. Open a stage to show the checks.
2. `02` then `03` Split PO → approved part-invoice, then held. Show the PO balance on the reference tab.
3. `05` Reissued duplicate → held, points at the original run.
4. `06` Look-alike vendor → held for the vendor master team.
5. `04` Scanned, no PO → approved with the PO inferred (needs the Gemini key).
6. `08` Billed before delivery → held with a question. Click "It has arrived: record the receipt", record it, and the invoice approves itself.
7. `09` Unfamiliar vendor name → answer "Yes, same company". It is remembered (see the reference tab) and the invoice is approved.
8. Dashboard: KPIs, business impact, "Needs review"; answer the question on `03` or `05`.

That is more than five minutes. For the video, pick the happy path plus two or three of these; keep the rest for the interview. `07` (missing information, then correct the fields and re-check) is a good spare.

Press "Reset demo data" before each rehearsal.

## Deploy (Render, free)

Push this folder to a GitHub repo, create a Render Blueprint from it (`render.yaml` is included), and set `GEMINI_API_KEY` in the service's environment. Free instances sleep when idle and lose run history on restart, so open the link a minute before a demo.

## Built with

FastAPI, SQLite, vanilla JS, Gemini API for reading documents. Built with Claude Code as the coding assistant.
