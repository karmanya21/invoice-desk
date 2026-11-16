"""Generate the demo invoice PDFs in samples/.

Three deliberately different vendor layouts plus one scanned (image-only) invoice,
so the extraction step has to cope with different labels and formats.

    python scripts/make_samples.py
"""
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

OUT = Path(__file__).resolve().parent.parent / "samples"
BUYER = ["Northfield Manufacturing Inc.", "Accounts Payable", "2200 Lakeshore Drive", "Chicago, IL 60601"]
W, H = A4


def money(x):
    return f"{x:,.2f}"


# --- Layout A: classic invoice (Meridian) -------------------------------------
def layout_classic(path, d):
    c = canvas.Canvas(str(path), pagesize=A4)
    c.setFillColor(colors.HexColor("#1f3a5f"))
    c.rect(0, H - 90, W, 90, stroke=0, fill=1)
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 20)
    c.drawString(40, H - 50, d["vendor"])
    c.setFont("Helvetica", 9)
    c.drawString(40, H - 68, d["vendor_address"])
    c.setFont("Helvetica-Bold", 24)
    c.drawRightString(W - 40, H - 55, "INVOICE")
    if d.get("stamp"):
        c.setFont("Helvetica-Bold", 10)
        c.drawRightString(W - 40, H - 74, d["stamp"])

    c.setFillColor(colors.black)
    y = H - 125
    for label, key in (("Invoice #", "number"), ("Invoice date", "date"), ("Due date", "due"), ("PO number", "po")):
        if d.get(key):
            c.setFont("Helvetica-Bold", 10)
            c.drawString(360, y, label)
            c.setFont("Helvetica", 10)
            c.drawString(450, y, d[key])
            y -= 16
    c.setFont("Helvetica-Bold", 10)
    c.drawString(40, H - 125, "Bill to")
    c.setFont("Helvetica", 10)
    for i, line in enumerate(BUYER):
        c.drawString(40, H - 141 - 14 * i, line)

    y = H - 230
    c.setFillColor(colors.HexColor("#e8edf3"))
    c.rect(40, y - 6, W - 80, 22, stroke=0, fill=1)
    c.setFillColor(colors.black)
    c.setFont("Helvetica-Bold", 10)
    c.drawString(48, y, "Description")
    c.drawRightString(360, y, "Qty")
    c.drawRightString(450, y, "Unit price")
    c.drawRightString(W - 48, y, "Amount")
    c.setFont("Helvetica", 10)
    y -= 26
    for desc, qty, price in d["lines"]:
        c.drawString(48, y, desc)
        c.drawRightString(360, y, f"{qty:g}")
        c.drawRightString(450, y, money(price))
        c.drawRightString(W - 48, y, money(qty * price))
        y -= 20
    c.line(40, y + 8, W - 40, y + 8)
    y -= 14
    sub = sum(q * p for _, q, p in d["lines"])
    tax = round(sub * d["tax_rate"] / 100, 2)
    for label, val, bold in (("Subtotal", sub, False), (f"Sales tax ({d['tax_rate']:g}%)", tax, False), ("Amount due (USD)", sub + tax, True)):
        c.setFont("Helvetica-Bold" if bold else "Helvetica", 11 if bold else 10)
        c.drawRightString(450, y, label)
        c.drawRightString(W - 48, y, ("$" if bold else "") + money(val))
        y -= 18

    y -= 30
    if d.get("notice"):
        c.setFillColor(colors.HexColor("#b00020"))
        c.setFont("Helvetica-Bold", 10)
        c.drawString(40, y, d["notice"])
        c.setFillColor(colors.black)
        y -= 20
    c.setFont("Helvetica", 9)
    c.drawString(40, y, f"Tax ID: {d['tax_id']}")
    c.drawString(40, y - 14, f"Remit to: First Lakes Bank, account ending {d['bank_last4']}")
    c.drawString(40, y - 28, "Payment terms: Net 30")
    c.save()


# --- Layout B: different labels, two-column header (Halden) -------------------
def layout_modern(path, d):
    c = canvas.Canvas(str(path), pagesize=A4)
    c.setFont("Times-Bold", 22)
    c.drawString(50, H - 60, d["vendor"])
    c.setFont("Times-Roman", 10)
    c.drawString(50, H - 76, d["vendor_address"])
    c.drawString(50, H - 90, f"Tax registration no. {d['tax_id']}")
    c.setStrokeColor(colors.HexColor("#c2410c"))
    c.setLineWidth(2)
    c.line(50, H - 102, W - 50, H - 102)

    c.setFont("Times-Bold", 14)
    c.drawString(50, H - 130, "TAX INVOICE")
    rows = [("Invoice No.", d.get("number")), ("Issue date", d.get("date")), ("Payment due", d.get("due")), ("Customer order ref", d.get("po"))]
    y = H - 152
    for label, val in rows:
        if val:
            c.setFont("Times-Roman", 10)
            c.drawString(50, y, f"{label}:")
            c.setFont("Times-Bold", 10)
            c.drawString(150, y, val)
            y -= 15
    c.setFont("Times-Bold", 10)
    c.drawString(350, H - 152, "Invoiced to")
    c.setFont("Times-Roman", 10)
    for i, line in enumerate(BUYER):
        c.drawString(350, H - 167 - 14 * i, line)

    y = H - 250
    c.setLineWidth(0.7)
    c.setStrokeColor(colors.black)
    c.setFont("Times-Bold", 10)
    c.drawString(50, y, "Item")
    c.drawRightString(370, y, "Units")
    c.drawRightString(450, y, "Rate")
    c.drawRightString(W - 50, y, "Line total")
    c.line(50, y - 5, W - 50, y - 5)
    c.setFont("Times-Roman", 10)
    y -= 22
    for desc, qty, price in d["lines"]:
        c.drawString(50, y, desc)
        c.drawRightString(370, y, f"{qty:g}")
        c.drawRightString(450, y, money(price))
        c.drawRightString(W - 50, y, money(qty * price))
        y -= 18
    c.line(50, y + 6, W - 50, y + 6)
    sub = sum(q * p for _, q, p in d["lines"])
    tax = round(sub * d["tax_rate"] / 100, 2)
    y -= 12
    for label, val, bold in (("Net amount", sub, False), (f"Tax at {d['tax_rate']:g}%", tax, False), ("Total payable USD", sub + tax, True)):
        c.setFont("Times-Bold" if bold else "Times-Roman", 11 if bold else 10)
        c.drawRightString(450, y, label)
        c.drawRightString(W - 50, y, money(val))
        y -= 17
    c.setFont("Times-Italic", 9)
    c.drawString(50, 80, f"Bank transfer to account ending {d['bank_last4']}, Harbor Commercial Bank. {d.get('footer', '')}")
    c.save()


# --- Layout C: informal statement with fields missing (Orchid) ----------------
def layout_statement(path, d):
    c = canvas.Canvas(str(path), pagesize=A4)
    c.setFont("Helvetica-Bold", 18)
    c.setFillColor(colors.HexColor("#6b21a8"))
    c.drawCentredString(W / 2, H - 60, d["vendor"])
    c.setFillColor(colors.black)
    c.setFont("Helvetica", 9)
    c.drawCentredString(W / 2, H - 76, d["vendor_address"])
    c.setFont("Helvetica-Bold", 13)
    c.drawString(50, H - 120, "Statement of services rendered")
    c.setFont("Helvetica", 10)
    c.drawString(50, H - 140, "To: " + ", ".join(BUYER[:1] + BUYER[2:]))
    if d.get("po"):
        c.drawString(50, H - 156, f"Purchase order: {d['po']}")
    y = H - 200
    for desc, qty, price in d["lines"]:
        c.drawString(50, y, desc)
        c.drawRightString(W - 50, y, money(qty * price))
        y -= 18
    sub = sum(q * p for _, q, p in d["lines"])
    tax = round(sub * d["tax_rate"] / 100, 2)
    c.line(50, y + 6, W - 50, y + 6)
    y -= 10
    c.drawString(50, y, f"Sales tax {d['tax_rate']:g}%")
    c.drawRightString(W - 50, y, money(tax))
    y -= 20
    c.setFont("Helvetica-Bold", 11)
    c.drawString(50, y, "Balance due")
    c.drawRightString(W - 50, y, "$" + money(sub + tax))
    c.setFont("Helvetica", 9)
    c.drawString(50, y - 50, f"Tax ID {d['tax_id']}  |  Please pay to account ending {d['bank_last4']}")
    c.drawString(50, y - 64, "Thank you for your business.")
    c.save()


# --- Scanned invoice: image-only PDF, no text layer (Kestrel) -----------------
def _font(size, bold=False):
    candidates = [
        "/System/Library/Fonts/Supplemental/Courier New Bold.ttf" if bold else "/System/Library/Fonts/Supplemental/Courier New.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    ]
    for p in candidates:
        if Path(p).exists():
            return ImageFont.truetype(p, size)
    return ImageFont.load_default(size)


def layout_scanned(path, d):
    img = Image.new("L", (1240, 1754), 246)
    dr = ImageDraw.Draw(img)
    dr.text((90, 90), d["vendor"].upper(), font=_font(44, True), fill=25)
    dr.text((90, 150), d["vendor_address"], font=_font(22), fill=40)
    dr.text((90, 182), f"Tax ID {d['tax_id']}", font=_font(22), fill=40)
    dr.line((90, 225, 1150, 225), fill=40, width=3)
    dr.text((90, 260), "FREIGHT INVOICE", font=_font(34, True), fill=25)
    dr.text((760, 262), f"No: {d['number']}", font=_font(26, True), fill=25)
    dr.text((760, 300), f"Date: {d['date']}", font=_font(24), fill=40)
    dr.text((90, 340), "Customer:", font=_font(24, True), fill=25)
    for i, line in enumerate(BUYER):
        dr.text((90, 374 + 30 * i), line, font=_font(24), fill=40)
    dr.line((90, 540, 1150, 540), fill=60, width=2)
    dr.text((90, 556), "Service", font=_font(24, True), fill=25)
    dr.text((980, 556), "Charge", font=_font(24, True), fill=25)
    dr.line((90, 594, 1150, 594), fill=60, width=2)
    y = 620
    for desc, qty, price in d["lines"]:
        dr.text((90, y), desc, font=_font(24), fill=40)
        y += 34
    for extra in d.get("line_notes", []):
        dr.text((120, y), extra, font=_font(21), fill=70)
        y += 30
    sub = sum(q * p for _, q, p in d["lines"])
    total = round(sub * (1 + d["tax_rate"] / 100), 2)
    dr.line((90, y + 20, 1150, y + 20), fill=60, width=2)
    dr.text((90, y + 44), f"TOTAL DUE (incl. {d['tax_rate']:g}% sales tax)", font=_font(27, True), fill=25)
    dr.text((930, y + 44), f"${money(total)}", font=_font(27, True), fill=25)
    dr.text((90, y + 150), f"Pay by bank transfer, account ending {d['bank_last4']}. Terms 30 days.", font=_font(22), fill=50)
    # A received stamp and scanner artefacts.
    dr.rectangle((820, 1380, 1120, 1480), outline=90, width=4)
    dr.text((842, 1398), "RECEIVED", font=_font(34, True), fill=90)
    dr.text((842, 1440), "05 NOV 2026", font=_font(22), fill=90)

    img = img.rotate(1.1, resample=Image.BICUBIC, fillcolor=240)
    rnd = random.Random(7)
    px = img.load()
    for _ in range(26000):
        x, yy = rnd.randrange(img.width), rnd.randrange(img.height)
        px[x, yy] = max(0, min(255, px[x, yy] + rnd.randint(-70, 30)))
    img = img.filter(ImageFilter.GaussianBlur(0.7))
    img.convert("RGB").save(path, "PDF", resolution=150)


MERIDIAN_LINES = [("Ergonomic task chair", 25, 310.00), ("Height-adjustable desk", 10, 640.00), ("Dual monitor arm", 25, 84.00)]

SAMPLES = [
    ("01_happy_path.pdf", layout_classic, dict(
        vendor="Meridian Office Supply Co.", vendor_address="118 Canal Street, Milwaukee, WI 53202", tax_id="47-2819934", bank_last4="4410",
        number="INV-20831", date="2026-11-03", due="2026-12-03", po="PO-4471", tax_rate=8, lines=MERIDIAN_LINES)),
    ("02_split_po_shipment_1.pdf", layout_modern, dict(
        vendor="Halden Industrial Parts Ltd", vendor_address="Unit 9, Forge Road Industrial Estate, Cleveland, OH 44114", tax_id="83-1150276", bank_last4="9027",
        number="HIP-7731", date="2026-10-20", due="2026-11-19", po="PO-4480", tax_rate=8,
        lines=[("Spindle assembly HX-200 (shipment 1 of 2)", 10, 2500.00)], footer="Balance of order to follow.")),
    ("03_split_po_shipment_2_over.pdf", layout_modern, dict(
        vendor="Halden Industrial Parts Ltd", vendor_address="Unit 9, Forge Road Industrial Estate, Cleveland, OH 44114", tax_id="83-1150276", bank_last4="9027",
        number="HIP-7764", date="2026-11-10", due="2026-12-10", po="PO-4480", tax_rate=8,
        lines=[("Spindle assembly HX-200 (shipment 2 of 2)", 7, 2500.00)], footer="Final shipment.")),
    ("04_scanned_no_po.pdf", layout_scanned, dict(
        vendor="Kestrel Logistics Inc.", vendor_address="4 Harbor Gate, Gary, IN 46402", tax_id="36-7704112", bank_last4="1185",
        number="KL/2026/1187", date="2026-11-02", tax_rate=8,
        lines=[("Regional freight distribution, October 2026", 1, 8400.00)],
        line_notes=["41 pallet movements, Chicago hub to regional depots", "Fuel surcharge and tolls included"])),
    ("05_reissued_duplicate.pdf", layout_classic, dict(
        vendor="Meridian Office Supply Co.", vendor_address="118 Canal Street, Milwaukee, WI 53202", tax_id="47-2819934", bank_last4="4410",
        number="INV-20831-R", date="2026-11-03", due="2026-12-03", po="PO-4471", tax_rate=8, lines=MERIDIAN_LINES, stamp="REVISED COPY")),
    ("06_lookalike_vendor_new_bank.pdf", layout_classic, dict(
        vendor="Meridian Office Supplies Inc.", vendor_address="PO Box 7730, Wilmington, DE 19801", tax_id="47-2819943", bank_last4="8826",
        number="INV-20877", date="2026-11-09", due="2026-11-16", po="PO-4471", tax_rate=8,
        lines=[("Height-adjustable desk", 10, 640.00)],
        notice="NOTICE: Our bank details have changed. Please update your records before payment.")),
    ("07_missing_invoice_number.pdf", layout_statement, dict(
        vendor="Orchid Facilities Management", vendor_address="77 Greenway Plaza, Evanston, IL 60201", tax_id="58-6621907", bank_last4="3059",
        po="PO-4521", tax_rate=8, lines=[("Janitorial services - November", 1, 3200.00)])),
]

if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    for name, fn, data in SAMPLES:
        fn(OUT / name, data)
        print("wrote", name)
