"""Emit credit_input.json, summary.md, and append to run_log.json."""
from __future__ import annotations
import json
import uuid
from datetime import datetime
from pathlib import Path
import pandas as pd

try:
    from .pricing import cost_inr, pure_llm_cost_per_statement, daily_projection
except ImportError:
    from pricing import cost_inr, pure_llm_cost_per_statement, daily_projection


def build_credit_input(df: pd.DataFrame, metrics: dict, anomalies: list[dict]) -> dict:
    tag_counts = df["tag"].fillna("untagged").value_counts().to_dict()
    source_counts = df["tag_source"].fillna("none").value_counts().to_dict()
    return {
        "transactions_total": int(len(df)),
        "tag_breakdown": {k: int(v) for k, v in tag_counts.items()},
        "tag_source_breakdown": {k: int(v) for k, v in source_counts.items()},
        "metrics": metrics,
        "anomalies": anomalies,
    }


def _fmt_inr(x: float) -> str:
    return f"₹{x:,.2f}"


def build_summary(credit_input: dict, statement_id: str) -> str:
    m = credit_input["metrics"]
    abb = m["abb"]
    bto = m["bto"]
    bnc = m["bounce_ratio"]
    oti = m["oti"]

    lines = [
        f"# Bank Statement Summary — {statement_id}",
        f"_Generated {datetime.now().isoformat(timespec='seconds')}_",
        "",
        f"**Transactions analysed:** {credit_input['transactions_total']}",
        "",
        "## Underwriting Metrics",
        f"- **ABB (Average Bank Balance):** {_fmt_inr(abb['value'])} — {abb['confidence']} confidence",
        f"- **BTO (Monthly Turnover, median):** {_fmt_inr(bto['value'])} — {bto['confidence']} confidence ({int(bto.get('confidence_pct',0)*100)}% high-conf tags)",
        f"- **Bounce Ratio:** {bnc['value']*100:.2f}%  ({bnc.get('bounce_count',0)} bounces / {bnc.get('emi_count',0)} EMIs) — {bnc['confidence']} confidence",
        f"- **OTI (Obligation-to-Income):** {oti['value']*100:.2f}% — {oti['confidence']} confidence",
        "",
        "## Tag Breakdown",
    ]
    for tag, n in credit_input["tag_breakdown"].items():
        lines.append(f"- {tag}: {n}")

    lines += ["", "## Anomalies"]
    if not credit_input["anomalies"]:
        lines.append("- None detected.")
    else:
        for a in credit_input["anomalies"]:
            lines.append(
                f"- ⚠️ **{a['type']}** with counterparty `{a['counterparty']}`: "
                f"{_fmt_inr(a['outflow_amount'])} out on {a['outflow_date']} → "
                f"{_fmt_inr(a['inflow_amount'])} in on {a['inflow_date']} "
                f"({a['spread_days']}d, Δ{a['amount_delta_pct']}%, severity={a['severity']})"
            )
    return "\n".join(lines)


def append_run_log(
    run_log_path: Path,
    *,
    statement_id: str,
    total_txns: int,
    rule_tagged: int,
    llm_tagged: int,
    llm_input_tokens: int,
    llm_output_tokens: int,
    ruleset_size_before: int,
    ruleset_size_after: int,
    rules_learned: int,
) -> dict:
    run_cost = cost_inr(llm_input_tokens, llm_output_tokens)
    pure_cost = pure_llm_cost_per_statement(total_txns)
    entry = {
        "run_id": uuid.uuid4().hex[:8],
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "statement_id": statement_id,
        "total_txns": total_txns,
        "rule_tagged_count": rule_tagged,
        "llm_tagged_count": llm_tagged,
        "coverage_pct": round(rule_tagged / total_txns * 100, 2) if total_txns else 0.0,
        "llm_input_tokens": llm_input_tokens,
        "llm_output_tokens": llm_output_tokens,
        "llm_cost_inr": round(run_cost, 4),
        "pure_llm_baseline_inr": round(pure_cost, 4),
        "savings_inr": round(pure_cost - run_cost, 4),
        "ruleset_size_before": ruleset_size_before,
        "ruleset_size_after": ruleset_size_after,
        "rules_learned_this_run": rules_learned,
        "projected_daily_cost_inr": round(daily_projection(run_cost), 2),
        "projected_daily_baseline_inr": round(daily_projection(pure_cost), 2),
    }

    log = []
    if Path(run_log_path).exists():
        try:
            log = json.loads(Path(run_log_path).read_text())
        except Exception:
            log = []
    log.append(entry)
    Path(run_log_path).write_text(json.dumps(log, indent=2))
    return entry
