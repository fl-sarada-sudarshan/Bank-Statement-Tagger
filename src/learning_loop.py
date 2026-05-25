"""Human-in-the-loop approval that promotes accepted suggestions into ruleset.json."""
from __future__ import annotations
from pathlib import Path
from typing import Callable, Optional
try:
    from .llm_fallback import ClusterSuggestion
    from .rule_engine import append_rule, make_rule_dict
except ImportError:
    from llm_fallback import ClusterSuggestion
    from rule_engine import append_rule, make_rule_dict


def cli_prompt(s: ClusterSuggestion) -> tuple[str, Optional[str], Optional[str]]:
    """Returns (action, override_tag, override_regex).
    action in {'approve', 'deny', 'edit'}.
    """
    print("\n" + "=" * 60)
    print(f"Cluster #{s.cluster_id}  ({s.txn_count} transactions)")
    print(f"Examples:")
    for d in s.example_descriptions[:3]:
        print(f"  - {d}")
    print(f"\nSuggested tag:    {s.suggested_tag}")
    print(f"Suggested regex:  {s.suggested_regex}")
    print(f"Direction:        {s.suggested_direction}")
    print(f"Confidence:       {s.confidence}")
    print(f"Reasoning:        {s.reasoning}")
    print("-" * 60)
    while True:
        choice = input("[a]pprove / [d]eny / [e]dit tag / [q]uit interactive: ").strip().lower()
        if choice in ("a", "approve", ""):
            return "approve", None, None
        if choice in ("d", "deny"):
            return "deny", None, None
        if choice in ("e", "edit"):
            new_tag = input(f"  new tag (blank to keep '{s.suggested_tag}'): ").strip() or s.suggested_tag
            new_regex = input(f"  new regex (blank to keep): ").strip() or s.suggested_regex
            return "approve", new_tag, new_regex
        if choice in ("q", "quit"):
            return "deny", None, None
        print("Please enter a/d/e/q.")


def run_learning_loop(
    suggestions: list[ClusterSuggestion],
    ruleset_path: Path,
    prompter: Callable[[ClusterSuggestion], tuple[str, Optional[str], Optional[str]]] = cli_prompt,
    auto_approve: bool = False,
) -> list[dict]:
    """Returns list of new rule dicts that were appended to the ruleset."""
    new_rules: list[dict] = []
    for s in suggestions:
        if auto_approve:
            action, tag, regex = "approve", None, None
        else:
            action, tag, regex = prompter(s)
        if action != "approve":
            continue
        rule = make_rule_dict(
            tag=tag or s.suggested_tag,
            regex=regex or s.suggested_regex,
            direction=s.suggested_direction,
            amount_min=None,
            amount_max=None,
            source="user_confirmed" if not auto_approve else "llm_suggested",
            priority=70,
        )
        append_rule(ruleset_path, rule)
        new_rules.append(rule)
        print(f"  ✓ Added rule {rule['id']} -> tag={rule['tag']}")
    return new_rules
