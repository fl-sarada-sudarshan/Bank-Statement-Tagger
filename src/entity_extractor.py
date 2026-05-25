"""Extract counterparty name, payment method, lender bank, entity type, and
   build a human-readable Transaction Categorisation string from raw bank
   statement description text.

   Supports the standard Indian bank description formats:
     NEFT CR/<IFSC>/<ENTITY>/<REFNO>
     NEFT DR/<IFSC>/<ACCTNO>/<ENTITY>/<REFNO>
     NACH DR/<ENTITY>/<IFSC>/<MANDATE>/<REF>
     ECS DR <ENTITY>/<REF>
     IMPS/<REF>/<ENTITY>/<BANK>
     UPI/DR/<vpa@bank>/<NAME>/<TXNREF>
     POS DEBIT/<MERCHANT>/<CITY>/<REF>
     ATM NWD/<LOCATION>/<REF>
     CHQ DEP/<ENTITY>/<CHQNO>
     RETURN/<MANDATE>/<ENTITY>/INSUFF FUNDS
"""
from __future__ import annotations
import re
from typing import Optional

# ── Constants ──────────────────────────────────────────────────────────────────

COMPANY_RE = re.compile(
    r'\b(PVT|LTD|PRIVATE|LIMITED|PRI|CORP|CORPORATION|CO\b|INC\b|'
    r'ENTERPRISE|ENTERPRISES|TRADING|INDUSTRIES|INDUSTRY|FINANCE|'
    r'FINANCIAL|BANK\b|TECHNOLOGIES|TECH\b|SOLUTIONS|SERVICES|'
    r'VENTURES|PVTLTD|ONLINE)\b',
    re.I
)

IFSC_RE = re.compile(r'\b([A-Z]{4})0[A-Z0-9]{6}\b')
LONG_NUM_RE = re.compile(r'\b\d{6,}\b')

BANK_MAP = {
    'HDFC': 'HDFC Bank',
    'ICIC': 'ICICI Bank',
    'UTIB': 'Axis Bank',
    'KKBK': 'Kotak Mahindra Bank',
    'SBIN': 'State Bank of India',
    'PUNB': 'Punjab National Bank',
    'CNRB': 'Canara Bank',
    'INDB': 'IndusInd Bank',
    'YESB': 'Yes Bank',
    'BARB': 'Bank of Baroda',
    'IOBA': 'Indian Overseas Bank',
    'UBIN': 'Union Bank of India',
    'FDRL': 'Federal Bank',
    'IDBI': 'IDBI Bank',
    'BKID': 'Bank of India',
    'MAHB': 'Bank of Maharashtra',
    'RATN': 'RBL Bank',
    'SIBL': 'South Indian Bank',
    'KVBL': 'Karur Vysya Bank',
    'CITI': 'Citibank',
    'SCBL': 'Standard Chartered',
    'HSBC': 'HSBC',
    'AXISBK': 'Axis Bank',
    'KOTAK': 'Kotak Mahindra Bank',
}

PAYMENT_PATTERNS = [
    ('NEFT',   re.compile(r'\bNEFT\b', re.I)),
    ('RTGS',   re.compile(r'\bRTGS\b', re.I)),
    ('IMPS',   re.compile(r'\b(?:IMPS|MMT/IMPS)\b', re.I)),
    ('UPI',    re.compile(r'\bUPI\b',  re.I)),
    ('CHEQUE', re.compile(r'\b(?:CHQ|CHEQUE|CMS|BIL/ONL|BIL/PAVC|BIL/)\b', re.I)),
    ('ECS',    re.compile(r'\b(?:ECS|NACH|ACH)\b', re.I)),
    ('ATM',    re.compile(r'\b(?:ATM|NWD|BY CASH|CAM/)\b', re.I)),
    ('POS',    re.compile(r'\b(?:POS|MESPOS|FT-MESPOS)\b', re.I)),
    ('GST',    re.compile(r'\bGIB/', re.I)),
    ('INT',    re.compile(r':Int\.\s*Coll:', re.I)),
]

# NEFT reference prefix → lender bank (ICICI statement format)
NEFT_PREFIX_MAP = {
    'UTIB': 'Axis Bank',
    'HDFC': 'HDFC Bank',
    'ICIC': 'ICICI Bank',
    'SBIN': 'State Bank of India',
    'PUNB': 'Punjab National Bank',
    'INDB': 'IndusInd Bank',
    'KVBL': 'Karur Vysya Bank',
    'BARB': 'Bank of Baroda',
    'CNRB': 'Canara Bank',
    'YESB': 'Yes Bank',
    'RATN': 'RBL Bank',
    'FDRL': 'Federal Bank',
    'UTIBN': 'Axis Bank',      # NEFT clearing code for Axis
    'INDBR': 'IndusInd Bank',
    'KVBLN': 'Karur Vysya Bank',
}

# ── Name cleaning ──────────────────────────────────────────────────────────────

def _clean(name: str) -> str:
    name = re.sub(r'[^A-Za-z0-9 &.,\-]', ' ', name)
    name = re.sub(r'\s+', ' ', name).strip().upper()
    return name


def _is_meaningful(name: str) -> bool:
    if not name or len(name) < 3:
        return False
    # reject strings that are purely numbers or single-char words
    words = name.split()
    alpha_words = [w for w in words if any(c.isalpha() for c in w)]
    return len(alpha_words) >= 1


# ── Core extractor ─────────────────────────────────────────────────────────────

def _lender_from_neft_prefix(desc_up: str) -> Optional[str]:
    """Extract lender bank from NEFT-/RTGS- reference prefix (ICICI statement format)."""
    m = re.match(r'(?:NEFT|RTGS)[-\s]+([A-Z]{4,6})', desc_up)
    if m:
        prefix = m.group(1)
        for key, name in NEFT_PREFIX_MAP.items():
            if prefix.startswith(key):
                return name
    return None


def extract_fields(description: str, direction: str) -> dict:
    """Return dict with counterparty, payment_method, lender, entity_type,
    transaction_categorisation, vendor."""
    desc = description.strip()
    desc_up = desc.upper()

    # --- Payment method ---
    payment_method = None
    for name, pat in PAYMENT_PATTERNS:
        if pat.search(desc_up):
            payment_method = name
            break

    # --- Lender bank from IFSC code ---
    lender = None
    for m in IFSC_RE.finditer(desc_up):
        prefix = m.group(1)
        if prefix in BANK_MAP:
            lender = BANK_MAP[prefix]
            break
    # Lender from NEFT/RTGS clearing prefix (ICICI format)
    if not lender:
        lender = _lender_from_neft_prefix(desc_up)
    # Explicit bank mentions if no IFSC
    if not lender:
        for code, name in BANK_MAP.items():
            if re.search(r'\b' + re.escape(code) + r'\b', desc_up):
                lender = name
                break

    # --- Counterparty name ---
    # Strip IFSC codes and long numbers to help name extraction
    stripped = IFSC_RE.sub('', desc_up)
    stripped = LONG_NUM_RE.sub('', stripped)
    stripped = re.sub(r'\s+', ' ', stripped).strip()

    counterparty = _try_extract_name(desc_up, stripped, payment_method)

    # --- Entity type ---
    entity_type = None
    if counterparty:
        if COMPANY_RE.search(counterparty):
            entity_type = 'company'
        else:
            words = counterparty.split()
            alpha_words = [w for w in words if w.isalpha()]
            entity_type = 'person' if 1 <= len(alpha_words) <= 4 else 'company'

    # --- Vendor (for POS/UPI merchant transactions) ---
    vendor = None
    if payment_method in ('POS', 'UPI') and counterparty:
        vendor = counterparty

    return {
        'counterparty': counterparty,
        'payment_method': payment_method,
        'lender': lender,
        'entity_type': entity_type,
        'vendor': vendor,
    }


def _try_extract_name(desc_up: str, stripped: str, payment: Optional[str]) -> Optional[str]:

    # ── Standard format patterns ───────────────────────────────────────────────

    # NEFT CR/<IFSC>/<ENTITY>/...
    m = re.search(r'NEFT\s+CR\s*/\s*(?:[A-Z]{4}0[A-Z0-9]{6}\s*/)?\s*([A-Z][A-Z0-9 &.,\-]{2,40}?)(?:\s*/|\s+REF|\s+SAL|\s+NOV|\s+DEC|\s+JAN|\s+FEB|\s+MAR|\s+APR|\s+MAY|\s+JUN|\s+JUL|\s+AUG|\s+SEP|\s+OCT|$)', desc_up)
    if m:
        name = _clean(m.group(1))
        if _is_meaningful(name): return name

    # NEFT DR/<IFSC>/<ACCTNO>/<ENTITY>/...
    m = re.search(r'NEFT\s+DR\s*/\s*(?:[A-Z]{4}0[A-Z0-9]{6}\s*/\s*)?(?:\d{5,}\s*/)?\s*([A-Z][A-Z0-9 &.,\-]{2,40}?)(?:\s*/|\s+REF|\s+LOAN|\s+EMI|\s+NOV|\s+$)', desc_up)
    if m:
        name = _clean(m.group(1))
        if _is_meaningful(name): return name

    # NACH/ECS DR/<IFSC>/<ENTITY>/...
    m = re.search(r'(?:NACH|ECS)\s+DR\s*/\s*(?:[A-Z]{4}0[A-Z0-9]{6}\s*/\s*)?([A-Z][A-Z0-9 &.,\-]{2,40}?)(?:\s*/|$)', desc_up)
    if m:
        name = _clean(m.group(1))
        if _is_meaningful(name): return name

    # ECS RTN/<ENTITY>/... or RETURN/<MANDATE>/<ENTITY>/INSUFF
    m = re.search(r'(?:ECS\s*RTN|RETURN)\s*/\s*(?:[A-Z0-9]{5,}\s*/)?\s*([A-Z][A-Z0-9 &.,\-]{2,40}?)(?:\s*/|$)', desc_up)
    if m:
        name = _clean(m.group(1))
        if _is_meaningful(name): return name

    # IMPS/<REF>/<ENTITY>/<BANK>
    m = re.search(r'IMPS\s*/\s*(?:[A-Z0-9\-]{5,}\s*/)?\s*([A-Z][A-Z0-9 &.,\-]{2,30}?)(?:\s*/|$)', desc_up)
    if m:
        name = _clean(m.group(1))
        if _is_meaningful(name): return name

    # UPI/DR/<vpa@bank>/<NAME>/  — name is part after @ section
    m = re.search(r'UPI\s*/\s*(?:DR|CR|DEBIT|CREDIT)\s*/\s*[^/]+@[^/]+\s*/\s*([A-Z][A-Z0-9 &.,\-]{2,40}?)(?:\s*/|$)', desc_up)
    if m:
        name = _clean(m.group(1))
        if _is_meaningful(name): return name

    # UPI — extract name before @ as fallback
    m = re.search(r'UPI[/\-][^@]*?[/\-]([A-Z][A-Z0-9\-_.]{1,25})@', desc_up)
    if m:
        raw = m.group(1).replace('.', ' ').replace('-', ' ')
        name = _clean(raw)
        if _is_meaningful(name): return name

    # POS DEBIT/<MERCHANT>/...
    m = re.search(r'POS\s+DEBIT\s*/\s*([A-Z][A-Z0-9 &.,\-]{2,30}?)(?:\s*/|$)', desc_up)
    if m:
        name = _clean(m.group(1))
        if _is_meaningful(name): return name

    # CHQ DEP/<ENTITY>/...
    m = re.search(r'CHQ\s+(?:DEP|WDL)\s*/\s*([A-Z][A-Z0-9 &.,\-]{2,40}?)(?:\s*/|$)', desc_up)
    if m:
        name = _clean(m.group(1))
        if _is_meaningful(name): return name

    # ── ICICI / alternate bank format patterns ────────────────────────────────

    # NEFT-{BANKREF}-{ENTITY}  (e.g. NEFT-UTIBN62025060514129850-GOOGLE INDIA DIGITAL S)
    m = re.search(r'NEFT[-\s]+[A-Z]{4,6}[A-Z0-9\s]{6,20}-\s*([A-Z][A-Z0-9 &.,\-]{2,50}?)(?:-\s*\d{5,}|$)', desc_up)
    if m:
        name = _clean(m.group(1))
        if _is_meaningful(name): return name

    # RTGS-{BANKREF}-{ENTITY}-{ACCT}
    m = re.search(r'RTGS[-\s]+[A-Z]{4,6}[A-Z0-9\s]{6,20}-\s*([A-Z][A-Z0-9 &.,\-]{2,50}?)(?:-\s*\d{5,}|$)', desc_up)
    if m:
        name = _clean(m.group(1))
        if _is_meaningful(name): return name

    # MMT/IMPS/{REF}/{NAME}/{IFSC}
    m = re.search(r'MMT/IMPS/\d+/([A-Z][A-Z0-9 &.,\-]{1,30}?)(?:/|$)', desc_up)
    if m:
        name = _clean(m.group(1))
        if _is_meaningful(name): return name

    # ACH/{ENTITY}/{MANDATE_OR_IFSC}/...  (ICICI ACH = NACH)
    m = re.search(r'ACH/([A-Z][A-Z0-9 &.,\-]{2,40}?)(?:/|$)', desc_up)
    if m:
        name = _clean(m.group(1))
        if _is_meaningful(name): return name

    # BIL/ONL/{REF}/{ENTITY} or BIL/PAVC/{REF}/{ENTITY}
    m = re.search(r'BIL/(?:ONL|PAVC)/[A-Z0-9]+/([A-Z][A-Z0-9 &.,\-\/]{1,40}?)(?:/|$)', desc_up)
    if m:
        name = _clean(m.group(1).split('/')[0])
        if _is_meaningful(name): return name

    # UPI/{NUMREF}/UPI Pay/{VPA}/...  or  UPI/{NUMREF}/{DESC}/{VPA}/...
    m = re.search(r'UPI/\d+/(?:UPI\s*PAY/)?([A-Z0-9._@\-]{3,40})(?:/|$)', desc_up)
    if m:
        raw = m.group(1)
        if '@' in raw:
            raw = raw.split('@')[0]
        name = _clean(raw.replace('.', ' ').replace('-', ' ').replace('_', ' '))
        if _is_meaningful(name): return name

    # BY CASH - {LOCATION}
    m = re.search(r'BY\s+CASH\s*[-–]\s*([A-Z][A-Z0-9 ]{2,40}?)(?:\s*$)', desc_up)
    if m:
        name = _clean(m.group(1))
        if _is_meaningful(name): return name

    # CAM/{ACCT}/{DESC}  — ATM/branch cash deposit
    m = re.search(r'CAM/[A-Z0-9]+/([A-Z][A-Z0-9 \-]{2,30}?)(?:/|$)', desc_up)
    if m:
        name = _clean(m.group(1))
        if _is_meaningful(name): return name

    # GIB/{REF}/GST/{...}  — always "GST Payment"
    if re.search(r'\bGIB/', desc_up):
        return 'GST PAYMENT'

    # MESPOS/{DESC}/{ID} or FT-MESPOS SET — merchant POS settlement
    m = re.search(r'MESPOS/([A-Z][A-Z0-9 _\-]{2,30}?)(?:/|$)', desc_up)
    if m:
        name = _clean(m.group(1).replace('_', ' '))
        if _is_meaningful(name): return 'MESPOS ' + name
    if re.search(r'FT-MESPOS', desc_up):
        return 'MESPOS SETTLEMENT'

    # {ACCTNO}:Int. Coll:{PERIOD}  — bank interest
    if re.search(r':INT\.?\s*COLL:', desc_up):
        return 'BANK INTEREST'

    # Bank charges: "Mob alrt Chg", "IMPS Chg", "SMS Chg", etc.
    m = re.search(r'(MOB\s*ALR?T|IMPS|SMS|CHEQUE BOOK|DEBIT CARD)\s*(?:CHG|CHARGE)', desc_up)
    if m:
        return 'BANK CHARGES'

    return None


# ── Categorisation string ──────────────────────────────────────────────────────

def build_categorisation(tag: str, counterparty: Optional[str], direction: str, entity_type: Optional[str]) -> str:
    cp = counterparty or ""
    if tag == "salary":
        return f"Salary from {cp}" if cp else "Salary Credit"
    if tag == "business_inflow":
        return f"Business Receipt — {cp}" if cp else "Business Inflow"
    if tag == "emi_payment":
        return f"EMI Payment — {cp}" if cp else "Loan EMI"
    if tag == "cheque_bounce":
        return f"Cheque / ECS Bounce — {cp}" if cp else "Cheque Bounce"
    if tag == "circular_transfer":
        return f"⚠ Suspicious Circular Transfer — {cp}" if cp else "Suspicious Transfer"
    if tag == "gambling":
        return f"Gambling / Gaming — {cp}" if cp else "Gambling"
    if tag == "regular_expense":
        return f"Expense — {cp}" if cp else "Regular Expense"
    if tag == "other":
        if direction == "credit":
            return f"Transfer from {cp}" if cp else "Inward Credit"
        return f"Transfer to {cp}" if cp else "Outward Transfer"
    # Fallback for untagged/unknown
    if direction == "credit":
        return f"Transfer from {cp}" if cp else "Inward Credit"
    return f"Transfer to {cp}" if cp else "Outward Transfer"


def enrich_dataframe(df, direction_col: str = "type"):
    """Add counterparty, payment_method, lender, entity_type, vendor,
    transaction_categorisation columns to df in-place."""
    import pandas as pd
    records = []
    for _, row in df.iterrows():
        fields = extract_fields(str(row["description"]), str(row.get(direction_col, "")))
        tag = str(row.get("tag") or "other")
        direction = str(row.get(direction_col, ""))
        fields["transaction_categorisation"] = build_categorisation(
            tag, fields["counterparty"], direction, fields["entity_type"]
        )
        records.append(fields)
    meta = pd.DataFrame(records, index=df.index)
    for col in meta.columns:
        df[col] = meta[col]
    return df
