import pandas as pd
from pathlib import Path
from src.rule_engine import load_ruleset, apply_rules
from src.metrics import all_metrics
from src.anomaly import detect_circular_transfers

ROOT = Path(__file__).resolve().parents[1]
RULESET = ROOT / "data" / "ruleset.json"


def _sample_df():
    return pd.DataFrame([
        {"date": "2026-01-01", "description": "NEFT CR ACME CORP SALARY JAN", "amount": 52000, "type": "credit", "balance": 102000},
        {"date": "2026-01-05", "description": "ECS DR HDFC LOAN EMI", "amount": 12450, "type": "debit", "balance": 89550},
        {"date": "2026-01-10", "description": "BAJAJ FIN EMI AUTO DEBIT", "amount": 5812, "type": "debit", "balance": 83738},
        {"date": "2026-01-18", "description": "I/W CHQ RET INSUFF FUNDS", "amount": 250, "type": "debit", "balance": 83488},
        {"date": "2026-01-20", "description": "UPI/SWIGGY/ORDER", "amount": 450, "type": "debit", "balance": 83038},
    ])


def test_rules_tag_known_categories():
    df = _sample_df()
    rules = load_ruleset(RULESET)
    tagged = apply_rules(df, rules)
    tags = tagged["tag"].tolist()
    assert tags[0] == "salary"
    assert tags[1] == "emi_payment"
    assert tags[2] == "emi_payment"
    assert tags[3] == "cheque_bounce"
    assert tags[4] == "regular_expense"


def test_metrics_compute():
    df = _sample_df()
    rules = load_ruleset(RULESET)
    tagged = apply_rules(df, rules)
    m = all_metrics(tagged)
    assert m["oti"]["total_inflow"] == 52000
    assert m["oti"]["total_emi"] == 12450 + 5812
    assert m["bounce_ratio"]["bounce_count"] == 1
    assert m["bounce_ratio"]["emi_count"] == 2
    assert m["abb"]["value"] > 0


def test_circular_detection():
    df = pd.DataFrame([
        {"date": "2026-02-12", "description": "IMPS DR TRF TO COUNTERPARTY_B A/C 998877",
         "amount": 100000, "type": "debit", "balance": 50000, "tag": "other", "tag_source": "seed"},
        {"date": "2026-02-18", "description": "IMPS CR REFUND COUNTERPARTY_B A/C 998877",
         "amount": 100000, "type": "credit", "balance": 150000, "tag": "other", "tag_source": "seed"},
    ])
    flags = detect_circular_transfers(df)
    assert len(flags) == 1
    assert flags[0]["counterparty"] == "acct_998877"
    assert flags[0]["severity"] == "high"


def test_no_false_positive_circular():
    df = pd.DataFrame([
        {"date": "2026-02-12", "description": "IMPS DR TRF TO COUNTERPARTY_B A/C 998877",
         "amount": 100000, "type": "debit", "balance": 50000, "tag": "other", "tag_source": "seed"},
        {"date": "2026-03-15", "description": "IMPS CR FROM COUNTERPARTY_X A/C 111222",
         "amount": 100000, "type": "credit", "balance": 150000, "tag": "other", "tag_source": "seed"},
    ])
    flags = detect_circular_transfers(df)
    assert flags == []
