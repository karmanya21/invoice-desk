"""Demo reference data: the 'procurement system' the invoices are matched against."""

VENDORS = [
    {"id": "V-001", "name": "Meridian Office Supply Co.", "aliases": ["Meridian Office Supply", "Meridian Office Supply Company"],
     "tax_id": "47-2819934", "bank_last4": "4410", "status": "approved", "contact_email": "ar@meridianoffice.example"},
    {"id": "V-002", "name": "Halden Industrial Parts Ltd", "aliases": ["Halden Industrial", "Halden Industrial Parts"],
     "tax_id": "83-1150276", "bank_last4": "9027", "status": "approved", "contact_email": "billing@haldenparts.example"},
    {"id": "V-003", "name": "Kestrel Logistics Inc.", "aliases": ["Kestrel Logistics", "Kestrel Freight & Logistics"],
     "tax_id": "36-7704112", "bank_last4": "1185", "status": "approved", "contact_email": "accounts@kestrellogistics.example"},
    {"id": "V-004", "name": "Brightwave Cloud Services", "aliases": ["Brightwave", "Brightwave Cloud"],
     "tax_id": "91-3348820", "bank_last4": "6672", "status": "approved", "contact_email": "invoices@brightwave.example"},
    {"id": "V-005", "name": "Orchid Facilities Management", "aliases": ["Orchid Facilities", "Orchid FM"],
     "tax_id": "58-6621907", "bank_last4": "3059", "status": "approved", "contact_email": "finance@orchidfm.example"},
    {"id": "V-006", "name": "Castellan Print Works", "aliases": [],
     "tax_id": "22-9047713", "bank_last4": "7781", "status": "blocked", "contact_email": "accounts@castellanprint.example"},
]

# PO amounts are net of tax. tax_rate is the rate the buyer expects on the invoice.
PURCHASE_ORDERS = [
    {"po_number": "PO-4471", "vendor_id": "V-001", "description": "Office furniture for 3rd floor fit-out",
     "currency": "USD", "tax_rate": 8.0, "buyer": "Priya Nair", "issued_date": "2026-09-22",
     "lines": [("Ergonomic task chair", 25, 310.00), ("Height-adjustable desk", 10, 640.00), ("Dual monitor arm", 25, 84.00)]},
    {"po_number": "PO-4480", "vendor_id": "V-002", "description": "CNC spindle assemblies HX-200, delivered in shipments",
     "currency": "USD", "tax_rate": 8.0, "buyer": "Daniel Okafor", "issued_date": "2026-09-30",
     "lines": [("Spindle assembly HX-200", 16, 2500.00)]},
    {"po_number": "PO-4502", "vendor_id": "V-003", "description": "Freight - regional distribution, October",
     "currency": "USD", "tax_rate": 8.0, "buyer": "Priya Nair", "issued_date": "2026-10-01",
     "lines": [("Regional freight distribution - October", 1, 8400.00)]},
    {"po_number": "PO-4503", "vendor_id": "V-003", "description": "Warehousing and pallet storage, Q4",
     "currency": "USD", "tax_rate": 8.0, "buyer": "Priya Nair", "issued_date": "2026-10-01",
     "lines": [("Warehouse pallet storage - monthly", 3, 4000.00)]},
    {"po_number": "PO-4515", "vendor_id": "V-004", "description": "Cloud hosting, annual commitment billed monthly",
     "currency": "USD", "tax_rate": 0.0, "buyer": "Daniel Okafor", "issued_date": "2026-08-15",
     "lines": [("Managed cloud hosting - monthly", 12, 1950.00)]},
    {"po_number": "PO-4521", "vendor_id": "V-005", "description": "Janitorial services, November",
     "currency": "USD", "tax_rate": 8.0, "buyer": "Daniel Okafor", "issued_date": "2026-10-28",
     "lines": [("Janitorial services - November", 1, 3200.00)]},
]
