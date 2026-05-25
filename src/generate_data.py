"""Generate a realistic Indian bank statement CSV with proper NEFT/UPI/IMPS/ECS
description formats and planted patterns for demo.

Planted patterns:
  - Monthly salary via NEFT CR from employer
  - 2 recurring EMIs: NACH DR (HDFC loan) + ECS DR (Bajaj Finance)
  - ~25 UPI/POS expenses per month
  - 2 cheque/ECS bounce events
  - 1 circular flow: Alpha Trading -> Gamma Enterprises -> back, within 7 days
  - Gambling (Dream11, Rummy)
  - Ambiguous rows: rent NEFT, family NEFT, vendor NEFT (LLM fallback candidates)
"""
from __future__ import annotations
import argparse
import csv
import random
from datetime import date, timedelta
from pathlib import Path

random.seed(42)

OPENING_BALANCE = 8_75_000.0          # ₹8.75 lakh — realistic for this customer
START = date(2025, 11, 1)
MONTHS = 6

# Realistic IFSC codes (correct 11-char format)
EMPLOYER_IFSC  = "UTIB0001234"        # Axis Bank
HDFC_IFSC      = "HDFC0001234"
BAJAJ_IFSC     = "KKBK0001234"       # Bajaj uses Kotak
SELF_IFSC      = "HDFC0009876"
ALPHA_IFSC     = "HDFC0001234"
GAMMA_IFSC     = "ICIC0005678"

EMPLOYER       = "FLEXILOANS TECHNOLOGIES PVT LTD"
EMPLOYER_SHORT = "FLEXILOANS TECH"

UPI_EXPENSES = [
    ("UPI/DR/swiggy@ibl/SWIGGY ONLINE ORDERING PVT LTD",       "food"),
    ("UPI/DR/zomato@icici/ZOMATO LIMITED",                       "food"),
    ("UPI/DR/amazon@apl/AMAZON SELLER SERVICES PVT LTD",        "shopping"),
    ("UPI/DR/flipkart@yesbank/FK RETAIL PVT LTD",               "shopping"),
    ("UPI/DR/bigbasket@hdfcbank/SUPERMARKET GROCERY SUPPLIES",   "grocery"),
    ("UPI/DR/dunzo@kotak/DUNZO DIGITAL PVT LTD",                "delivery"),
    ("UPI/DR/blinkit@icici/BLINKIT COMMERCE PVT LTD",           "grocery"),
    ("UPI/DR/nykaa@yesbank/NYKAA E-RETAIL PVT LTD",             "shopping"),
    ("UPI/DR/myntra@hdfcbank/MYNTRA DESIGNS PVT LTD",           "shopping"),
    ("UPI/DR/bookmyshow@kotak/BIGTREE ENTERTAINMENT PVT LTD",   "entertainment"),
    ("UPI/DR/uber@indus/UBER INDIA SYSTEMS PVT LTD",            "transport"),
    ("UPI/DR/ola@hdfcbank/ANI TECHNOLOGIES PVT LTD",            "transport"),
    ("UPI/DR/rapido@yesbank/ROPPEN TRANSPORTATION SERVICES",     "transport"),
    ("UPI/DR/meesho@axis/FASHNEAR TECHNOLOGIES PVT LTD",        "shopping"),
    ("UPI/DR/phoneperecharge@ybl/PHONEPE PRIVATE LIMITED",       "recharge"),
    ("UPI/DR/paytmmall@paytm/PAYTM E-COMMERCE PVT LTD",        "shopping"),
]
POS_EXPENSES = [
    "POS DEBIT/DMART AVENUE SUPERMARTS/MUMBAI MH",
    "POS DEBIT/RELIANCE FRESH STORES/MUMBAI MH",
    "POS DEBIT/STARBUCKS COFFEE INDIA/MUMBAI MH",
    "POS DEBIT/SHOPPERS STOP LIMITED/MUMBAI MH",
    "POS DEBIT/CROMA RETAIL INFINITI/MUMBAI MH",
    "POS DEBIT/MAKEMYTRIP INDIA PVT/GURGAON HR",
    "POS DEBIT/LENSKART SOLUTIONS/BENGALURU KA",
]

# Ambiguous transactions (should go to LLM fallback — not matched by seed rules)
AMBIGUOUS = [
    f"NEFT DR/{HDFC_IFSC}/0001234567/RAJESH KUMAR/RENT NOV2025/REF987600",
    f"NEFT DR/{HDFC_IFSC}/0005678901/PRIYA SHARMA/HOME LOAN ADVANCE/REF987601",
    f"NEFT CR/{GAMMA_IFSC}/XYZ TRADING ENTERPRISES PVT LTD/PYMT INV2025001/REF987602",
    f"NEFT DR/{HDFC_IFSC}/0009871234/TUSHAR VERMA/STUDY ABROAD FEE/REF987603",
    f"UPI/DR/insurancepremium@hdfcbank/HDFC ERGO GENERAL INSURANCE CO",
]


def add_days(d: date, n: int) -> date:
    return d + timedelta(days=n)


def month_iter(start: date, months: int):
    for m in range(months):
        y = start.year + (start.month - 1 + m) // 12
        mo = (start.month - 1 + m) % 12 + 1
        yield date(y, mo, 1)


def refno(seed: int) -> str:
    return f"REF{seed:010d}"


def gen():
    txns = []   # (date, description, amount, type)
    ref = 100000000

    def r():
        nonlocal ref
        ref += 1
        return f"REF{ref:010d}"

    # ── Salary: 1st of each month via NEFT CR ──────────────────────────────
    for first in month_iter(START, MONTHS):
        month_name = first.strftime("%b%Y").upper()
        sal = round(1_04_500 + random.uniform(-2000, 2000), 2)
        txns.append((first,
            f"NEFT CR/{EMPLOYER_IFSC}/{EMPLOYER}/SAL/{month_name}/{r()}",
            sal, "credit"))

    # ── EMI 1: NACH DR HDFC Bank Home Loan, 5th of each month ─────────────
    for first in month_iter(START, MONTHS):
        month_name = first.strftime("%b%Y").upper()
        txns.append((add_days(first, 4),
            f"NACH DR/{HDFC_IFSC}/HDFC BANK LIMITED/HOME LOAN EMI {month_name}/MANDATE0001234/{r()}",
            32_450.00, "debit"))

    # ── EMI 2: ECS DR Bajaj Finance Personal Loan, 10th ───────────────────
    for first in month_iter(START, MONTHS):
        month_name = first.strftime("%b%Y").upper()
        txns.append((add_days(first, 9),
            f"ECS DR/{BAJAJ_IFSC}/BAJAJ FINANCE LIMITED/PERSONAL LOAN EMI {month_name}/MANDATE0005678/{r()}",
            14_812.00, "debit"))

    # ── 2 ECS/Cheque bounces across 6 months ──────────────────────────────
    txns.append((date(2025, 12, 6),
        f"ECS RTN/MANDATE0005678/BAJAJ FINANCE LIMITED/INSUFF FUNDS/NOV2025/{r()}",
        590.00, "debit"))
    txns.append((date(2026, 3, 11),
        f"RETURN/MANDATE0001234/HDFC BANK LIMITED/INSUFF FUNDS/FEB2026/{r()}",
        590.00, "debit"))

    # ── UPI / POS expenses: ~25/month ─────────────────────────────────────
    for first in month_iter(START, MONTHS):
        for _ in range(random.randint(20, 26)):
            d = add_days(first, random.randint(0, 27))
            if random.random() < 0.7:
                base_desc, _ = random.choice(UPI_EXPENSES)
                desc = f"{base_desc}/{r()}"
            else:
                desc = f"{random.choice(POS_EXPENSES)}/{r()}"
            amt = round(random.uniform(80, 3500), 2)
            txns.append((d, desc, amt, "debit"))

    # ── Bill payments: electricity + mobile, 15th & 20th ─────────────────
    for first in month_iter(START, MONTHS):
        month_name = first.strftime("%b%Y").upper()
        txns.append((add_days(first, 14),
            f"NACH DR/{HDFC_IFSC}/BEST ELECTRIC SUPPLY UNDERTAKING/ELECT BILL {month_name}/{r()}",
            round(random.uniform(1800, 4200), 2), "debit"))
        txns.append((add_days(first, 19),
            f"UPI/DR/airtel@icici/BHARTI AIRTEL LIMITED/{r()}",
            round(random.choice([499, 599, 699, 799]), 2), "debit"))

    # ── ATM withdrawals: ~2/month ─────────────────────────────────────────
    for first in month_iter(START, MONTHS):
        txns.append((add_days(first, random.randint(5, 25)),
            f"ATM NWD/{HDFC_IFSC}/HDFC BANK ATM ANDHERI WEST MUMBAI/{r()}",
            round(random.choice([3000, 4000, 5000, 6000, 8000]), 2), "debit"))

    # ── Interest credit: quarterly ─────────────────────────────────────────
    txns.append((date(2025, 12, 31),
        f"INT CR/{SELF_IFSC}/SB INTEREST CREDIT DEC2025/{r()}",
        1_842.55, "credit"))
    txns.append((date(2026, 3, 31),
        f"INT CR/{SELF_IFSC}/SB INTEREST CREDIT MAR2026/{r()}",
        2_204.20, "credit"))

    # ── Ambiguous (LLM fallback target): 1/month ──────────────────────────
    for i, first in enumerate(month_iter(START, MONTHS)):
        d = add_days(first, random.randint(11, 25))
        txns.append((d, AMBIGUOUS[i % len(AMBIGUOUS)], random.choice([8000, 15000, 22000, 35000, 12000]),
                     "debit" if "DR" in AMBIGUOUS[i % len(AMBIGUOUS)] else "credit"))

    # ── Circular flow: Feb 12 → Feb 14 → Feb 18 ───────────────────────────
    txns.append((date(2026, 2, 12),
        f"NEFT DR/{ALPHA_IFSC}/0000998877/ALPHA TRADING CO/BUSINESS TRF/{r()}",
        1_00_000.00, "debit"))
    txns.append((date(2026, 2, 14),
        f"NEFT DR/{ALPHA_IFSC}/0000776655/GAMMA ENTERPRISES PVT LTD/ADVANCE/{r()}",
        99_750.00, "debit"))
    txns.append((date(2026, 2, 15),
        f"NEFT CR/{GAMMA_IFSC}/GAMMA ENTERPRISES PVT LTD/REFUND/{r()}",
        99_500.00, "credit"))
    txns.append((date(2026, 2, 18),
        f"NEFT CR/{ALPHA_IFSC}/ALPHA TRADING CO/RETURN FUNDS/{r()}",
        1_00_000.00, "credit"))

    # ── Gambling ───────────────────────────────────────────────────────────
    txns.append((date(2026, 1, 7),
        f"UPI/DR/dream11@icici/DREAM11 GAME AND SPORTS PVT LTD/{r()}",
        2_500.00, "debit"))
    txns.append((date(2026, 4, 14),
        f"UPI/DR/rummycircle@yesbank/HEAD DIGITAL WORKS PVT LTD/{r()}",
        4_000.00, "debit"))

    # ── Business inflow: occasional client payment ─────────────────────────
    txns.append((date(2025, 12, 10),
        f"NEFT CR/{GAMMA_IFSC}/PARAS WIRES PVT LTD/INV2025120010/{r()}",
        10_00_000.00, "credit"))
    txns.append((date(2026, 3, 15),
        f"IMPS/{r()}/MATRIX POLYTECH PVT LTD/{HDFC_IFSC}",
        3_54_000.00, "credit"))

    # Sort chronologically
    txns.sort(key=lambda x: x[0])

    bal = OPENING_BALANCE
    rows = []
    for d, desc, amt, typ in txns:
        if typ == "credit":
            bal += amt
        else:
            bal -= amt
        rows.append({
            "date": d.isoformat(),
            "description": desc,
            "amount": f"{amt:.2f}",
            "type": typ,
            "balance": f"{bal:.2f}",
        })
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(Path(__file__).resolve().parents[1] / "data" / "synthetic_statement.csv"))
    args = ap.parse_args()

    rows = gen()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["date", "description", "amount", "type", "balance"])
        w.writeheader()
        w.writerows(rows)
    print(f"Wrote {len(rows)} transactions → {out}")


if __name__ == "__main__":
    main()
