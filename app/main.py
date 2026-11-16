"""HTTP API + static UI for Invoice Desk."""
import asyncio
import json
import threading
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config, db, pipeline

app = FastAPI(title="Invoice Desk")
db.init()

MIME = {".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}
MAX_BYTES = 10 * 1024 * 1024

SAMPLE_INFO = {
    "01_happy_path.pdf": ("Happy path", "Clean invoice, PO quoted, everything matches.", "APPROVED"),
    "02_split_po_shipment_1.pdf": ("Split PO: shipment 1", "First of two invoices against one PO. Part-invoice.", "APPROVED"),
    "03_split_po_shipment_2_over.pdf": ("Split PO: shipment 2", "Second invoice bills more than is left on the PO. Run after shipment 1.", "HOLD"),
    "04_scanned_no_po.pdf": ("Scanned, no PO number", "Image-only scan, tax buried in the total, PO has to be inferred.", "APPROVED"),
    "05_reissued_duplicate.pdf": ("Reissued duplicate", "Same invoice as the happy path with a new number. Run after it.", "HOLD"),
    "06_lookalike_vendor_new_bank.pdf": ("Look-alike vendor", "Near-identical vendor name, different tax ID and bank account.", "HOLD"),
    "07_missing_invoice_number.pdf": ("Missing information", "No invoice number and no date on the document.", "REJECTED"),
}


@app.get("/api/health")
def health():
    return {"ok": True, "ai_enabled": bool(config.GEMINI_API_KEY), "models": config.GEMINI_MODELS}


@app.get("/api/samples")
def samples():
    out = []
    for p in sorted(config.SAMPLES_DIR.glob("*.pdf")):
        title, desc, expected = SAMPLE_INFO.get(p.name, (p.stem, "", ""))
        out.append({"file": p.name, "title": title, "description": desc, "expected": expected})
    return out


@app.get("/api/samples/{name}")
def sample_file(name: str):
    path = config.SAMPLES_DIR / Path(name).name
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path, media_type="application/pdf", content_disposition_type="inline")


@app.post("/api/runs")
async def start_run(file: UploadFile | None = File(None), sample: str | None = Form(None)):
    if sample:
        path = config.SAMPLES_DIR / Path(sample).name
        if not path.exists():
            raise HTTPException(404, "Unknown sample")
        data, filename = path.read_bytes(), path.name
    elif file:
        data, filename = await file.read(), Path(file.filename or "upload.pdf").name
    else:
        raise HTTPException(400, "Send a file or a sample name")
    mime = MIME.get(Path(filename).suffix.lower())
    if not mime:
        raise HTTPException(400, "Only PDF, PNG and JPG invoices are supported")
    if not data or len(data) > MAX_BYTES:
        raise HTTPException(400, "File is empty or larger than 10 MB")

    run_id = "R-" + uuid.uuid4().hex[:6].upper()
    stored = config.UPLOAD_DIR / f"{run_id}{Path(filename).suffix.lower()}"
    config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    stored.write_bytes(data)
    db.create_run({"id": run_id, "created_at": datetime.now().isoformat(timespec="seconds"), "filename": filename,
                   "file_path": str(stored), "mime": mime, "status": "running"})
    threading.Thread(target=pipeline.run_pipeline, args=(run_id, data, filename, mime), daemon=True).start()
    return {"id": run_id}


@app.get("/api/runs")
def runs():
    return db.list_runs()


@app.get("/api/runs/{run_id}")
def run(run_id: str):
    r = db.get_run(run_id)
    if not r:
        raise HTTPException(404)
    return r


@app.get("/api/runs/{run_id}/file")
def run_file(run_id: str):
    f = db.run_file(run_id)
    if not f or not Path(f["file_path"]).exists():
        raise HTTPException(404)
    return FileResponse(f["file_path"], media_type=f["mime"], content_disposition_type="inline")


@app.get("/api/runs/{run_id}/events")
async def run_events(run_id: str):
    """Server-sent events: push the run's state every time a stage changes."""
    async def stream():
        last = None
        for _ in range(1200):  # hard stop after ~3 minutes
            r = db.get_run(run_id)
            if not r:
                break
            payload = json.dumps(r)
            if payload != last:
                last = payload
                yield f"data: {payload}\n\n"
            if r["status"] != "running":
                break
            await asyncio.sleep(0.15)
    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


class Review(BaseModel):
    action: str
    reviewer: str = ""
    note: str = ""


@app.post("/api/runs/{run_id}/review")
def review(run_id: str, body: Review):
    if body.action not in ("approve", "reject"):
        raise HTTPException(400, "action must be approve or reject")
    r = pipeline.review(run_id, body.action, body.reviewer.strip(), body.note.strip())
    if not r:
        raise HTTPException(409, "This invoice is not waiting for review")
    return r


class Rerun(BaseModel):
    fields: dict
    reviewer: str = ""
    note: str = ""


@app.post("/api/runs/{run_id}/rerun")
def rerun(run_id: str, body: Rerun):
    if not body.reviewer.strip() or not body.note.strip():
        raise HTTPException(400, "A name and a reason are required, both are kept for audit")
    problem = pipeline.rerun(run_id, body.fields, body.reviewer.strip(), body.note.strip())
    if problem:
        raise HTTPException(409, problem)
    return {"id": run_id}


@app.get("/api/stats")
def stats():
    rs = [r for r in db.list_runs() if r["status"] != "running"]
    n = len(rs)
    by = lambda key, val: [r for r in rs if r[key] == val]
    auto = [r for r in by("decision", "APPROVED") if not r["corrections"]]   # corrected by a person is not touchless
    waiting = by("final_state", "HOLD")
    stopped = [r for r in rs if r["decision"] != "APPROVED" and r["final_state"] != "APPROVED"]
    cleared = by("final_state", "APPROVED")
    total = lambda xs: round(sum(r["total_amount"] or 0 for r in xs), 2)
    imp = config.IMPACT
    held_n = len(by("decision", "HOLD"))
    saved = n * imp["manual_minutes_per_invoice"] - held_n * imp["review_minutes_per_hold"]
    reasons = {}
    for r in rs:
        if r["decision"] != "APPROVED":
            g = reasons.setdefault((r["decision"], r["headline"]), {"outcome": r["decision"], "reason": r["headline"], "owner": r["owner"], "count": 0, "value": 0.0})
            g["count"] += 1
            g["value"] = round(g["value"] + (r["total_amount"] or 0), 2)
    return {
        "minutes_saved": max(saved, 0),
        "manual_minutes": n * imp["manual_minutes_per_invoice"],
        "impact": imp,
        "reasons": sorted(reasons.values(), key=lambda g: -g["value"]),
        "corrected": len([r for r in rs if r["corrections"]]),
        "processed": n,
        "auto_approved": len(auto),
        "touchless_rate": round(len(auto) / n * 100) if n else 0,
        "held": len(by("decision", "HOLD")),
        "rejected": len(by("decision", "REJECTED")),
        "awaiting_review": len(waiting),
        "awaiting_value": total(waiting),
        "cleared_value": total(cleared),
        "stopped_value": total(stopped),
        "avg_seconds": round(sum(r["duration_ms"] or 0 for r in rs) / n / 1000, 1) if n else 0,
    }


@app.get("/api/reference")
def reference():
    return {"vendors": db.vendors(), "purchase_orders": db.purchase_orders(), "rules": config.RULES, "impact": config.IMPACT}


@app.post("/api/reset")
def reset():
    db.reset()
    return {"ok": True}


app.mount("/", StaticFiles(directory=config.STATIC_DIR, html=True), name="static")
