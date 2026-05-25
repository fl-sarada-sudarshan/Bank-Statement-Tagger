"""Anomaly detection: circular transfers across counterparty graph.

A circular transfer is suspicious when:
  - amounts are similar (within ±10%),
  - all legs occur within a short window (≤14 days),
  - and a counterparty receives and then returns funds (round-trip pattern).
We detect the simpler high-precision case here: same counterparty appears as both
a debit destination and a credit source with similar amounts within the window.
"""
from __future__ import annotations
import re
from datetime import timedelta
import pandas as pd

COUNTERPARTY_RE = re.compile(r"(?:TO|FROM)\s+([A-Z][A-Z_0-9 ]{2,})", re.IGNORECASE)
ACCT_RE = re.compile(r"A/?C\s*[:#]?\s*([0-9]{4,})", re.IGNORECASE)
BARE_ACCT_RE = re.compile(r"\b([0-9]{6,})\b")
NAMED_CP_RE = re.compile(r"(COUNTERPARTY_[A-Z0-9]+)", re.IGNORECASE)


def extract_counterparty(desc: str) -> str | None:
    m = ACCT_RE.search(desc)
    if m:
        return f"acct_{m.group(1)}"
    m = BARE_ACCT_RE.search(desc)
    if m:
        return f"acct_{m.group(1)}"
    m = NAMED_CP_RE.search(desc)
    if m:
        return m.group(1).upper()
    m = COUNTERPARTY_RE.search(desc)
    if m:
        return m.group(1).strip().upper()
    return None


def detect_circular_transfers(df: pd.DataFrame, window_days: int = 14, amount_tolerance: float = 0.1) -> list[dict]:
    d = df.copy()
    d["date"] = pd.to_datetime(d["date"])
    d["counterparty"] = d["description"].map(extract_counterparty)
    d = d[d["counterparty"].notna()].sort_values("date").reset_index(drop=True)

    flags: list[dict] = []
    seen_pairs: set[tuple] = set()
    for i, row in d.iterrows():
        if row["type"] != "debit":
            continue
        cp = row["counterparty"]
        amt = float(row["amount"])
        d0 = row["date"]
        # look for a credit from the same counterparty within window with similar amount
        candidates = d[
            (d["counterparty"] == cp)
            & (d["type"] == "credit")
            & (d["date"] >= d0)
            & (d["date"] <= d0 + timedelta(days=window_days))
        ]
        for _, c in candidates.iterrows():
            ca = float(c["amount"])
            if amt == 0:
                continue
            if abs(ca - amt) / amt <= amount_tolerance:
                key = (cp, d0.isoformat(), c["date"].isoformat())
                if key in seen_pairs:
                    continue
                seen_pairs.add(key)
                flags.append({
                    "type": "circular_transfer",
                    "counterparty": cp,
                    "outflow_date": d0.date().isoformat(),
                    "outflow_amount": amt,
                    "inflow_date": c["date"].date().isoformat(),
                    "inflow_amount": ca,
                    "spread_days": int((c["date"] - d0).days),
                    "amount_delta_pct": round(abs(ca - amt) / amt * 100, 2),
                    "severity": "high" if abs(ca - amt) / amt < 0.02 else "medium",
                    "evidence": [row["description"], c["description"]],
                })
    return flags
