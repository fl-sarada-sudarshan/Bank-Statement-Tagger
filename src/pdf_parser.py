"""Parse PDF bank statements into a standard DataFrame.

Supports:
  - Standard multi-column format (Debit / Credit / Balance)
  - ICICI Bank 9-column format:
      Sr No | Tran ID | Value Date | Txn Date | Cheque/Ref |
      Transaction Remarks | Withdrawal (Dr) | Deposit (Cr) | Balance
  - Multi-page PDFs where the header row only appears on page 1
  - Multi-line cell text (collapsed to single line)
  - "NA" / "Nil" values in amount columns
  - Negative balances (overdraft accounts)
  - Date formats: DD-Mon-YYYY, DD/MM/YYYY, YYYY-MM-DD, DD-MM-YYYY
"""
from __future__ import annotations
import io
import re
from datetime import datetime
from typing import Optional

import pandas as pd

try:
    import pdfplumber
    HAS_PDFPLUMBER = True
except ImportError:
    HAS_PDFPLUMBER = False

# ── Header alias sets (lower-cased, whitespace-collapsed) ─────────────────────

DATE_ALIASES = {
    'date', 'txn date', 'value date', 'transaction date', 'posting date',
    'trans date', 'tran date', 'entry date', 'valuedate', 'txndate',
}
DESC_ALIASES = {
    'description', 'narration', 'particulars', 'remarks',
    'transaction details', 'details', 'transaction narration',
    'transaction remarks', 'particulars/description', 'chq/ref details',
    'transaction description', 'transaction particulars',
}
DEBIT_ALIASES = {
    'debit', 'withdrawal', 'withdrawl', 'dr', 'debit amount',
    'withdrawals', 'dr amount', 'withdrawal (dr)', 'withdrawl (dr)',
    'debit(dr)', 'amount(dr)', 'withdrawal amount', 'debit amt',
}
CREDIT_ALIASES = {
    'credit', 'deposit', 'cr', 'credit amount', 'deposits',
    'cr amount', 'deposit (cr)', 'credit(cr)', 'amount(cr)',
    'deposit amount', 'credit amt',
}
BALANCE_ALIASES = {
    'balance', 'closing balance', 'running balance', 'available balance',
    'bal', 'closing bal', 'outstanding balance', 'book balance',
    'balance (in rs)', 'balance (rs)',
}

# ── Regexes ───────────────────────────────────────────────────────────────────

AMT_RE  = re.compile(r'-?[\d,]+\.?\d*')
DATE_RE = re.compile(
    r'\d{1,2}[-/]\d{1,2}[-/]\d{2,4}'        # DD/MM/YYYY or DD-MM-YYYY
    r'|\d{4}[-/]\d{2}[-/]\d{2}'              # YYYY-MM-DD
    r'|\d{1,2}[-/](?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[-/]\d{2,4}',  # DD-Mon-YYYY
    re.I,
)

_MONTH_MAP = {
    'jan': '01', 'feb': '02', 'mar': '03', 'apr': '04',
    'may': '05', 'jun': '06', 'jul': '07', 'aug': '08',
    'sep': '09', 'oct': '10', 'nov': '11', 'dec': '12',
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _collapse(cell) -> str:
    """Flatten multi-line cell to a single cleaned string.

    Joins continuation lines intelligently:
    - No space when next line starts with /  @  or a digit (path/URL continuations)
    - No space when current line ends with /  @  -  (same reason)
    - Space everywhere else
    """
    if cell is None:
        return ''
    s = str(cell)
    # Line break immediately before a path/URL continuation character → no space
    s = re.sub(r'\n(?=[/@\d])', '', s)
    # Line break immediately after a trailing connector → no space
    s = re.sub(r'(?<=[/@\-])\n', '', s)
    # Remaining newlines → single space
    s = s.replace('\n', ' ')
    return re.sub(r'\s+', ' ', s).strip()


def _norm_header(cell) -> str:
    return re.sub(r'\s+', ' ', str(cell or '').replace('\n', ' ')).strip().lower()


def _clean_amount(val) -> Optional[float]:
    s = _collapse(val)
    if not s or s.upper() in ('NA', 'NIL', '-', '', 'N/A', 'NULL'):
        return None
    s = s.replace(',', '')
    # handle (1,234.56) negative notation
    if s.startswith('(') and s.endswith(')'):
        s = '-' + s[1:-1]
    m = re.search(r'-?[\d]+\.?\d*', s)
    return float(m.group(0)) if m else None


def _parse_date(raw) -> Optional[str]:
    """Normalise any common date format → YYYY-MM-DD string."""
    s = _collapse(raw)
    if not s:
        return None
    # DD-Mon-YYYY or DD-Mon-YY
    m = re.match(r'(\d{1,2})[-/](Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[-/](\d{2,4})', s, re.I)
    if m:
        day, mon, yr = m.group(1), m.group(2).lower(), m.group(3)
        yr = ('20' + yr) if len(yr) == 2 else yr
        return f"{yr}-{_MONTH_MAP[mon]}-{int(day):02d}"
    # YYYY-MM-DD (already ISO)
    m = re.match(r'(\d{4})[-/](\d{1,2})[-/](\d{1,2})', s)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    # DD/MM/YYYY or DD-MM-YYYY
    m = re.match(r'(\d{1,2})[-/](\d{1,2})[-/](\d{2,4})', s)
    if m:
        d, mo, yr = m.group(1), m.group(2), m.group(3)
        yr = ('20' + yr) if len(yr) == 2 else yr
        return f"{yr}-{int(mo):02d}-{int(d):02d}"
    return None


def _looks_like_date(s: str) -> bool:
    return bool(DATE_RE.search(_collapse(s)))


def _looks_like_amount(s: str) -> bool:
    c = _collapse(s)
    return bool(c) and c.upper() not in ('NA', 'NIL', 'N/A') and bool(re.search(r'[\d,]+\.?\d*', c))


# ── Column detection ──────────────────────────────────────────────────────────

def _detect_columns_from_headers(headers: list[str]) -> dict:
    """Map field name → column index from a normalised header list."""
    mapping: dict[str, int] = {}
    for i, h in enumerate(headers):
        hn = _norm_header(h)
        # Remove parenthetical suffixes like "(dr)", "(cr)", "(rs)" for matching
        hn_clean = re.sub(r'\s*\(.*?\)', '', hn).strip()
        if hn_clean in DATE_ALIASES or hn in DATE_ALIASES:
            if 'date' not in mapping:          # prefer Value Date / first date col
                mapping['date'] = i
        elif hn_clean in DESC_ALIASES or hn in DESC_ALIASES:
            mapping['description'] = i
        elif hn_clean in DEBIT_ALIASES or hn in DEBIT_ALIASES:
            mapping['debit'] = i
        elif hn_clean in CREDIT_ALIASES or hn in CREDIT_ALIASES:
            mapping['credit'] = i
        elif hn_clean in BALANCE_ALIASES or hn in BALANCE_ALIASES:
            mapping['balance'] = i
    return mapping


def _detect_columns_structural(sample_rows: list[list]) -> Optional[dict]:
    """Fallback: infer columns by position from data rows."""
    if not sample_rows:
        return None
    # Find a row that looks like a real transaction
    for row in sample_rows[:5]:
        vals = [_collapse(c) for c in row]
        n = len(vals)
        if n < 4:
            continue

        # Scan for a date column
        date_col = next((i for i, v in enumerate(vals) if _looks_like_date(v)), None)
        if date_col is None:
            continue

        # Balance is usually the last numeric column
        balance_col = None
        for i in range(n - 1, -1, -1):
            if _looks_like_amount(vals[i]) or (vals[i] and vals[i][0] == '-' and _looks_like_amount(vals[i])):
                balance_col = i
                break
        if balance_col is None:
            continue

        # Description: longest text column (excluding numeric cols)
        numeric_cols = {date_col, balance_col}
        # Also check 2 cols before balance for Dr/Cr
        cr_col = dr_col = None
        for i in range(balance_col - 1, -1, -1):
            if i in numeric_cols:
                continue
            v = vals[i].upper()
            if v == 'NA' or _looks_like_amount(v):
                if cr_col is None:
                    cr_col = i
                    numeric_cols.add(i)
                elif dr_col is None:
                    dr_col = i
                    numeric_cols.add(i)
                    break

        desc_col = max(
            (i for i in range(n) if i not in numeric_cols),
            key=lambda i: len(vals[i]),
            default=None,
        )
        if desc_col is None:
            continue

        mapping: dict[str, int] = {
            'date': date_col,
            'description': desc_col,
            'balance': balance_col,
        }
        if cr_col is not None:
            mapping['credit'] = cr_col
        if dr_col is not None:
            mapping['debit'] = dr_col
        return mapping
    return None


# ── ICICI-specific 9-column detector ─────────────────────────────────────────

def _is_icici_9col(table: list[list]) -> bool:
    """Detect ICICI Bank 9-col layout: Sr | TranID | ValDate | TxnDate | Ref | Remarks | Dr | Cr | Bal"""
    if not table:
        return False
    row = table[0]
    if len(row) != 9:
        return False
    # First cell should be a serial number (int) or header "Sr No"
    first = _collapse(row[0]).lower()
    if not (re.match(r'^\d+$', first) or 'sr' in first or 'no' in first):
        return False
    # Col 2 or 3 should look like a date
    return _looks_like_date(row[2]) or _looks_like_date(row[3])


_ICICI_9COL = {
    'date': 2,          # Value Date
    'description': 5,   # Transaction Remarks
    'debit': 6,         # Withdrawal (Dr)
    'credit': 7,        # Deposit (Cr)
    'balance': 8,       # Balance
}


# ── Row extraction ────────────────────────────────────────────────────────────

def _extract_rows(tables: list[list[list]], col_map: dict) -> list[dict]:
    records = []
    for table in tables:
        for row in table:
            if not row or len(row) <= max(col_map.values()):
                continue
            def get(key):
                idx = col_map.get(key)
                return row[idx] if idx is not None and idx < len(row) else None

            raw_date = _collapse(get('date'))
            if not raw_date or not DATE_RE.search(raw_date):
                continue  # not a data row

            date_iso = _parse_date(raw_date)
            if not date_iso:
                continue

            debit  = _clean_amount(get('debit'))
            credit = _clean_amount(get('credit'))
            if debit is None and credit is None:
                continue

            amount    = debit if debit is not None else credit
            direction = 'debit' if debit is not None else 'credit'
            balance   = _clean_amount(get('balance'))
            # Preserve negative balance sign
            raw_bal = _collapse(get('balance'))
            if raw_bal and raw_bal.strip().startswith('-') and balance is not None:
                balance = -abs(balance)

            desc = _collapse(get('description'))

            records.append({
                'date': date_iso,
                'description': desc,
                'amount': abs(amount),
                'type': direction,
                'balance': balance if balance is not None else 0.0,
            })
    return records


# ── Main entry point ──────────────────────────────────────────────────────────

def parse_pdf(file_bytes: bytes) -> pd.DataFrame:
    """Extract bank statement rows from PDF bytes.

    Returns DataFrame with columns: date, description, amount, type, balance.
    Raises ValueError if extraction fails.
    """
    if not HAS_PDFPLUMBER:
        raise ValueError("pdfplumber not installed. Run: pip install pdfplumber")

    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        page_tables: list[list[list]] = []
        for page in pdf.pages:
            tables = page.extract_tables()
            for tbl in tables:
                if tbl and len(tbl) >= 1:
                    page_tables.append(tbl)

    if not page_tables:
        raise ValueError(
            "No tables found in PDF. The file may be scanned (image-based) "
            "or use an unsupported layout."
        )

    # ── Determine column mapping ──────────────────────────────────────────────

    col_map: Optional[dict] = None
    data_tables: list[list[list]] = []

    first_tbl = page_tables[0]

    # Strategy A: explicit header row (normalised)
    first_row_headers = [_norm_header(c) for c in first_tbl[0]]
    candidate = _detect_columns_from_headers(first_row_headers)
    has_date = 'date' in candidate
    has_desc = 'description' in candidate
    has_amounts = ('debit' in candidate or 'credit' in candidate)

    if has_date and has_desc and has_amounts:
        col_map = candidate
        # Page 1: skip header row; pages 2+: all rows are data
        data_tables.append(first_tbl[1:])
        data_tables.extend(page_tables[1:])

    # Strategy B: ICICI 9-column structural match
    if col_map is None:
        # Check if first page table looks like ICICI 9-col (may or may not have header)
        # Try skipping first row (header), check second row
        if len(first_tbl) >= 2 and _is_icici_9col(first_tbl[1:]):
            col_map = _ICICI_9COL
            data_tables.append(first_tbl[1:])   # skip header
            data_tables.extend(page_tables[1:])
        elif _is_icici_9col(first_tbl):
            col_map = _ICICI_9COL
            data_tables = page_tables            # no header to skip

    # Strategy C: structural detection from data rows
    if col_map is None:
        all_rows = [row for tbl in page_tables for row in tbl]
        col_map = _detect_columns_structural(all_rows)
        if col_map:
            data_tables = page_tables

    if col_map is None:
        raise ValueError(
            "Could not identify column structure. "
            f"First-page headers found: {first_row_headers}. "
            "Expected columns similar to: Date, Description/Narration, "
            "Debit/Withdrawal, Credit/Deposit, Balance."
        )

    # Ensure we have at least one amount column
    if 'debit' not in col_map and 'credit' not in col_map:
        raise ValueError(
            f"No debit/credit columns detected. Column map: {col_map}. "
            f"Headers: {first_row_headers}"
        )

    # ── Extract and assemble ─────────────────────────────────────────────────

    records = _extract_rows(data_tables, col_map)
    if not records:
        raise ValueError(
            "Table structure detected but no valid transaction rows extracted. "
            "Check that the PDF contains digital (not scanned) text."
        )

    df = pd.DataFrame(records)
    df['amount']  = df['amount'].astype(float)
    df['balance'] = df['balance'].astype(float)
    df = df.sort_values('date').reset_index(drop=True)
    return df
