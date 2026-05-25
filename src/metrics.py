"""Underwriting metrics with confidence scoring."""
from __future__ import annotations
import pandas as pd

HIGH_CONF_SOURCES = {"seed", "user_confirmed"}


def _confidence_pct(tags_df: pd.DataFrame) -> float:
    if len(tags_df) == 0:
        return 1.0
    high = tags_df["tag_source"].isin(HIGH_CONF_SOURCES).sum()
    return float(high / len(tags_df))


def _confidence_band(pct: float) -> str:
    if pct >= 0.9:
        return "HIGH"
    if pct >= 0.6:
        return "MEDIUM"
    return "LOW"


def average_bank_balance(df: pd.DataFrame) -> dict:
    """Mean of daily closing balance (forward-filled across days with no txn)."""
    if df.empty:
        return {"value": 0.0, "confidence": "HIGH", "confidence_pct": 1.0, "method": "no_data"}
    d = df.sort_values("date").copy()
    d["date"] = pd.to_datetime(d["date"])
    # Take last balance per day
    daily = d.groupby(d["date"].dt.date)["balance"].last()
    full_idx = pd.date_range(daily.index.min(), daily.index.max(), freq="D")
    daily.index = pd.to_datetime(daily.index)
    daily = daily.reindex(full_idx).ffill()
    abb = float(daily.mean())
    return {
        "value": round(abb, 2),
        "confidence": "HIGH",
        "confidence_pct": 1.0,
        "method": "balance-derived (tag-independent)",
    }


def monthly_turnover(df: pd.DataFrame) -> dict:
    """Median monthly inflow (credits tagged salary/business_inflow)."""
    inflows = df[df["tag"].isin(["salary", "business_inflow"])].copy()
    if inflows.empty:
        return {"value": 0.0, "confidence": "LOW", "confidence_pct": 0.0, "monthly": {}}
    inflows["date"] = pd.to_datetime(inflows["date"])
    inflows["month"] = inflows["date"].dt.to_period("M").astype(str)
    monthly = inflows.groupby("month")["amount"].sum()
    pct = _confidence_pct(inflows)
    return {
        "value": round(float(monthly.median()), 2),
        "monthly": {k: round(float(v), 2) for k, v in monthly.items()},
        "confidence_pct": round(pct, 2),
        "confidence": _confidence_band(pct),
    }


def bounce_ratio(df: pd.DataFrame) -> dict:
    bounces = df[df["tag"] == "cheque_bounce"]
    emis = df[df["tag"] == "emi_payment"]
    denom = len(bounces) + len(emis)
    if denom == 0:
        return {"value": 0.0, "confidence": "HIGH", "confidence_pct": 1.0, "bounce_count": 0, "emi_count": 0}
    ratio = len(bounces) / denom
    contrib = pd.concat([bounces, emis])
    pct = _confidence_pct(contrib)
    return {
        "value": round(ratio, 4),
        "bounce_count": int(len(bounces)),
        "emi_count": int(len(emis)),
        "confidence_pct": round(pct, 2),
        "confidence": _confidence_band(pct),
    }


def obligation_to_income(df: pd.DataFrame) -> dict:
    inflows = df[df["tag"].isin(["salary", "business_inflow"])]
    emis = df[df["tag"] == "emi_payment"]
    inc = float(inflows["amount"].sum())
    obl = float(emis["amount"].sum())
    contrib = pd.concat([inflows, emis])
    pct = _confidence_pct(contrib) if len(contrib) else 0.0
    oti = (obl / inc) if inc > 0 else 0.0
    return {
        "value": round(oti, 4),
        "total_inflow": round(inc, 2),
        "total_emi": round(obl, 2),
        "confidence_pct": round(pct, 2),
        "confidence": _confidence_band(pct),
    }


def all_metrics(df: pd.DataFrame) -> dict:
    return {
        "abb": average_bank_balance(df),
        "bto": monthly_turnover(df),
        "bounce_ratio": bounce_ratio(df),
        "oti": obligation_to_income(df),
    }
