"""End-to-end pipeline:
  CSV -> rules -> LLM fallback -> learning loop -> metrics -> anomaly -> outputs.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import pandas as pd

from .rule_engine import load_ruleset, apply_rules, CONFIDENCE_BY_SOURCE
from .llm_fallback import cluster_untagged, call_llm, ruleset_summary
from .learning_loop import run_learning_loop
from .metrics import all_metrics
from .anomaly import detect_circular_transfers
from .output import build_credit_input, build_summary, append_run_log

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RULESET = ROOT / "data" / "ruleset.json"
DEFAULT_CSV = ROOT / "data" / "synthetic_statement.csv"
RUN_LOG = ROOT / "data" / "run_log.json"
OUT_DIR = ROOT / "data" / "out"


def run(csv_path: Path, ruleset_path: Path, statement_id: str, interactive: bool, auto_approve: bool) -> dict:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    df = pd.read_csv(csv_path)
    df["amount"] = df["amount"].astype(float)
    df["balance"] = df["balance"].astype(float)

    rules_before = load_ruleset(ruleset_path)
    size_before = len(rules_before)

    tagged = apply_rules(df, rules_before)
    rule_tagged_count = tagged["tag"].notna().sum()
    untagged_count_before_llm = tagged["tag"].isna().sum()
    print(f"[main] Rule pass: {rule_tagged_count}/{len(tagged)} tagged "
          f"({rule_tagged_count/len(tagged)*100:.1f}% coverage); "
          f"{untagged_count_before_llm} untagged go to LLM.")

    # Cluster untagged + call LLM
    clusters = cluster_untagged(tagged)
    suggestions, usage = call_llm(clusters, tagged[tagged["tag"].notna()], ruleset_summary(rules_before))
    print(f"[main] LLM proposed {len(suggestions)} cluster suggestions "
          f"(tokens: in={usage['input_tokens']}, out={usage['output_tokens']}).")

    # Learning loop -> appends rules to ruleset.json
    new_rules: list[dict] = []
    if suggestions and (interactive or auto_approve):
        new_rules = run_learning_loop(
            suggestions, ruleset_path,
            auto_approve=auto_approve,
        )

    # Re-apply rules so LLM-learned/approved suggestions tag their rows
    rules_after = load_ruleset(ruleset_path)
    tagged = apply_rules(df, rules_after)
    # For any still-untagged after the second pass, fall back to LLM suggestions
    # directly (mark as llm_suggested medium confidence even if user declined to learn).
    still_untagged = tagged[tagged["tag"].isna()].index
    if len(still_untagged) > 0 and suggestions:
        # map cluster_id -> suggestion
        sug_by_cluster = {s.cluster_id: s for s in suggestions}
        cluster_df = cluster_untagged(tagged)
        for idx in still_untagged:
            if idx in cluster_df.index:
                cid = int(cluster_df.loc[idx, "cluster_id"])
                s = sug_by_cluster.get(cid)
                if s:
                    tagged.at[idx, "tag"] = s.suggested_tag
                    tagged.at[idx, "rule_id"] = f"llm_cluster_{cid}"
                    tagged.at[idx, "confidence"] = CONFIDENCE_BY_SOURCE["llm_suggested"]
                    tagged.at[idx, "tag_source"] = "llm_suggested"
    # Final fill
    tagged["tag"] = tagged["tag"].fillna("other")
    tagged["tag_source"] = tagged["tag_source"].fillna("unmatched")
    tagged["confidence"] = tagged["confidence"].fillna("low")

    rule_tagged_final = tagged["tag_source"].isin(["seed", "user_confirmed"]).sum()
    llm_tagged_final = tagged["tag_source"].isin(["llm_suggested"]).sum()

    metrics = all_metrics(tagged)
    anomalies = detect_circular_transfers(tagged)

    credit_input = build_credit_input(tagged, metrics, anomalies)
    summary = build_summary(credit_input, statement_id)

    (OUT_DIR / f"{statement_id}_credit_input.json").write_text(json.dumps(credit_input, indent=2))
    (OUT_DIR / f"{statement_id}_summary.md").write_text(summary)
    tagged.to_csv(OUT_DIR / f"{statement_id}_tagged.csv", index=False)

    log_entry = append_run_log(
        RUN_LOG,
        statement_id=statement_id,
        total_txns=int(len(tagged)),
        rule_tagged=int(rule_tagged_final),
        llm_tagged=int(llm_tagged_final),
        llm_input_tokens=int(usage["input_tokens"]),
        llm_output_tokens=int(usage["output_tokens"]),
        ruleset_size_before=size_before,
        ruleset_size_after=len(rules_after),
        rules_learned=len(new_rules),
    )

    print(f"[main] Outputs written to {OUT_DIR}/")
    print(f"[main] Coverage (rule-tagged): {log_entry['coverage_pct']}%")
    print(f"[main] LLM cost this run: ₹{log_entry['llm_cost_inr']} "
          f"(pure-LLM baseline ₹{log_entry['pure_llm_baseline_inr']}, savings ₹{log_entry['savings_inr']})")
    print(f"[main] Projected at 50K stmts/day: ₹{log_entry['projected_daily_cost_inr']} "
          f"vs baseline ₹{log_entry['projected_daily_baseline_inr']}")
    if anomalies:
        print(f"[main] ⚠️  {len(anomalies)} anomaly(ies) flagged")
    return log_entry


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=str(DEFAULT_CSV))
    ap.add_argument("--ruleset", default=str(DEFAULT_RULESET))
    ap.add_argument("--statement-id", default="stmt_001")
    ap.add_argument("--interactive", action="store_true", help="Prompt user to approve LLM suggestions")
    ap.add_argument("--auto-approve", action="store_true", help="Auto-approve LLM suggestions into ruleset")
    args = ap.parse_args()
    run(Path(args.csv), Path(args.ruleset), args.statement_id, args.interactive, args.auto_approve)


if __name__ == "__main__":
    main()
