"""Central place for every tunable rule, so the policy is readable in one screen."""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DATA_DIR = Path(os.getenv("DATA_DIR", ROOT / "data"))
UPLOAD_DIR = DATA_DIR / "uploads"
DB_PATH = DATA_DIR / "invoice_desk.db"
SAMPLES_DIR = ROOT / "samples"
STATIC_DIR = ROOT / "static"

# --- AI extraction -----------------------------------------------------------
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
# Tried in order; the first model that answers wins. Free-tier daily quotas are counted
# per model, so a wider pool means more invoices per day before the offline parser takes over.
# The lite models come first: the full flash models allow only about 20 requests a day each.
GEMINI_MODELS = [
    m.strip()
    for m in os.getenv(
        "GEMINI_MODELS", "gemini-3.5-flash-lite,gemini-3.1-flash-lite,gemini-3-flash-preview,gemini-3.6-flash,gemini-3.8-flash"
    ).split(",")
    if m.strip()
]
GEMINI_TIMEOUT_S = float(os.getenv("GEMINI_TIMEOUT_S", "6"))
# How long a scan waits for its second, confirming read after the first one arrives.
SECOND_READ_GRACE_S = float(os.getenv("SECOND_READ_GRACE_S", "3"))

# --- Business rules ----------------------------------------------------------
RULES = {
    # An invoice may exceed the PO's remaining balance by this much and still
    # auto-approve. Both limits must hold (2% of a $1M PO is not "small").
    "tolerance_pct": 2.0,
    "tolerance_abs": 250.00,
    # Unit price on an invoice line vs the PO line.
    "unit_price_tolerance_pct": 2.0,
    # Rounding slack when checking the invoice's own arithmetic.
    "arithmetic_slack": 0.02,
    # Same vendor + same total within this many days = probable duplicate.
    "near_duplicate_window_days": 7,
    # Vendor-name similarity (0-1) needed before we treat a name as "probably" a known vendor.
    "vendor_fuzzy_threshold": 0.82,
    # A PO inferred by the process (not printed on the invoice) only auto-approves below this value.
    "implied_po_auto_limit": 10000.00,
    "implied_po_min_score": 0.60,
    "implied_po_min_margin": 0.20,
    # Invoices dated in the future or older than this are suspicious.
    "max_invoice_age_days": 180,
    # Even a perfectly clean invoice above this total needs a person to sign it off.
    "auto_approve_limit": 50000.00,
    # A hold nobody has answered after this long escalates to the AP manager.
    "hold_sla_hours": 48,
}
DEFAULT_RULES = dict(RULES)

# Rules the process owner may change from the UI, without a developer: key -> (label, unit, min, max).
EDITABLE_RULES = {
    "tolerance_pct": ("Amount tolerance, % of PO", "%", 0, 20),
    "tolerance_abs": ("Amount tolerance, cap", "$", 0, 100000),
    "unit_price_tolerance_pct": ("Unit price tolerance", "%", 0, 20),
    "near_duplicate_window_days": ("Probable-duplicate window", "days", 0, 90),
    "implied_po_auto_limit": ("Inferred-PO auto-approval limit", "$", 0, 10000000),
    "auto_approve_limit": ("Automatic approval limit", "$", 0, 100000000),
    "hold_sla_hours": ("Hold escalates after", "hours", 1, 720),
}

# Minimum time a stage stays on screen in the live view. The real compute time
# is recorded separately and shown per stage; this only paces the display.
STAGE_PACING_MS = int(os.getenv("STAGE_PACING_MS", "150"))

# Assumptions behind the "manual work avoided" figure on the dashboard.
IMPACT = {
    "manual_minutes_per_invoice": 8,   # open PDF, find PO in spreadsheet, check, key in
    "review_minutes_per_hold": 3,      # a person reading a prepared hold and deciding
}
