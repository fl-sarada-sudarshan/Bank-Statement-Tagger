"""LLM fallback: cluster untagged txns, single batched Claude call."""
from __future__ import annotations
import json
import os
import re
from dataclasses import dataclass, asdict
from typing import Optional
import pandas as pd

MODEL = "claude-sonnet-4-6"
ALLOWED_TAGS = [
    "salary", "business_inflow", "emi_payment", "cheque_bounce",
    "circular_transfer", "gambling", "regular_expense", "other",
]


@dataclass
class ClusterSuggestion:
    cluster_id: int
    cluster_key: str
    example_descriptions: list[str]
    txn_count: int
    suggested_tag: str
    suggested_regex: str
    suggested_direction: Optional[str]
    confidence: str  # "low" | "medium" | "high"
    reasoning: str


def normalize_description(desc: str) -> str:
    """Strip numbers, dates, UPI handles to get a clustering stem."""
    s = desc.upper()
    s = re.sub(r"\d+", "#", s)
    s = re.sub(r"[A-Z0-9._-]+@[A-Z]+", "@HANDLE", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def cluster_untagged(df: pd.DataFrame) -> pd.DataFrame:
    """Return df with cluster_key + cluster_id for untagged rows."""
    untagged = df[df["tag"].isna()].copy()
    if untagged.empty:
        untagged["cluster_key"] = []
        untagged["cluster_id"] = []
        return untagged
    untagged["cluster_key"] = untagged["description"].map(normalize_description)
    keys = {k: i for i, k in enumerate(sorted(untagged["cluster_key"].unique()))}
    untagged["cluster_id"] = untagged["cluster_key"].map(keys)
    return untagged


def _build_prompt(clusters: pd.DataFrame, tagged_examples: pd.DataFrame, ruleset_summary: list[dict]) -> tuple[str, str]:
    examples_str = ""
    for tag, grp in tagged_examples.groupby("tag"):
        sample = grp.head(3)["description"].tolist()
        if sample:
            examples_str += f"\n  {tag}: {sample}"

    cluster_block = []
    for cid, grp in clusters.groupby("cluster_id"):
        cluster_block.append({
            "cluster_id": int(cid),
            "normalized_key": grp["cluster_key"].iloc[0],
            "count": int(len(grp)),
            "direction": grp["type"].mode().iloc[0],
            "sample_descriptions": grp["description"].head(5).tolist(),
            "amount_range": [float(grp["amount"].min()), float(grp["amount"].max())],
        })

    system = (
        "You are a credit-analysis assistant that tags Indian bank-statement transactions. "
        f"Allowed tags: {ALLOWED_TAGS}. "
        "For each cluster you must return a JSON object with: cluster_id, suggested_tag (from allowed list), "
        "suggested_regex (a Python re-compatible pattern that should reliably match this cluster's descriptions), "
        "suggested_direction ('credit'|'debit'|null), confidence ('low'|'medium'|'high'), reasoning (1 sentence). "
        "Make the regex specific enough to not over-match other transaction types but general enough to cover variations."
    )

    user = (
        f"Existing rule tags (do not duplicate):\n{json.dumps(ruleset_summary, indent=2)}\n\n"
        f"Examples of already-tagged transactions (for reference):{examples_str}\n\n"
        f"Untagged clusters to classify:\n{json.dumps(cluster_block, indent=2)}\n\n"
        "Respond ONLY with a JSON array. Each element is an object with the fields described."
    )
    return system, user


def call_llm(clusters_df: pd.DataFrame, tagged_df: pd.DataFrame, ruleset_summary: list[dict]) -> tuple[list[ClusterSuggestion], dict]:
    """Returns (suggestions, usage_dict). usage_dict has input_tokens, output_tokens."""
    if clusters_df.empty:
        return [], {"input_tokens": 0, "output_tokens": 0}

    system, user = _build_prompt(clusters_df, tagged_df, ruleset_summary)

    # If no API key, return a deterministic stub for offline demo
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return _stub_suggestions(clusters_df), {"input_tokens": len(system + user) // 4, "output_tokens": 200}

    try:
        from anthropic import Anthropic
        client = Anthropic()
        resp = client.messages.create(
            model=MODEL,
            max_tokens=2000,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        text = "".join(b.text for b in resp.content if hasattr(b, "text"))
        # Extract JSON array
        m = re.search(r"\[.*\]", text, re.DOTALL)
        raw = json.loads(m.group(0)) if m else []
        suggestions = []
        for item in raw:
            cid = int(item["cluster_id"])
            grp = clusters_df[clusters_df["cluster_id"] == cid]
            if grp.empty:
                continue
            suggestions.append(ClusterSuggestion(
                cluster_id=cid,
                cluster_key=grp["cluster_key"].iloc[0],
                example_descriptions=grp["description"].head(5).tolist(),
                txn_count=int(len(grp)),
                suggested_tag=item.get("suggested_tag", "other"),
                suggested_regex=item.get("suggested_regex", re.escape(grp["cluster_key"].iloc[0])),
                suggested_direction=item.get("suggested_direction"),
                confidence=item.get("confidence", "medium"),
                reasoning=item.get("reasoning", ""),
            ))
        usage = {"input_tokens": resp.usage.input_tokens, "output_tokens": resp.usage.output_tokens}
        return suggestions, usage
    except Exception as e:
        print(f"[llm_fallback] API call failed ({e}), falling back to stub")
        return _stub_suggestions(clusters_df), {"input_tokens": len(system + user) // 4, "output_tokens": 200}


def _stub_suggestions(clusters_df: pd.DataFrame) -> list[ClusterSuggestion]:
    """Heuristic stub used when no API key is set so the demo runs offline."""
    out = []
    for cid, grp in clusters_df.groupby("cluster_id"):
        key = grp["cluster_key"].iloc[0]
        direction = grp["type"].mode().iloc[0]
        # crude heuristics
        if "RENT" in key:
            tag, regex = "regular_expense", r"(?i)RENT TO"
            reasoning = "Description starts with RENT TO indicating recurring rent payment."
        elif "TUITION" in key or "FEE" in key:
            tag, regex = "regular_expense", r"(?i)TUITION|SCHOOL FEE"
            reasoning = "Education-related fee debit."
        elif "VENDOR" in key or ("PYMT" in key and direction == "credit"):
            tag, regex = "business_inflow", r"(?i)PYMT TO VENDOR|VENDOR.*TRADING"
            reasoning = "Counterparty appears to be a business vendor, credit direction."
        elif "SISTER" in key or "FAMILY" in key or "TRF TO" in key:
            tag, regex = "other", r"(?i)TRF TO (SISTER|BROTHER|FAMILY|PARENT)"
            reasoning = "Intra-family transfer, not income or expense."
        else:
            tag, regex = "other", re.escape(key[:20])
            reasoning = "No strong signal; default to other."
        out.append(ClusterSuggestion(
            cluster_id=int(cid),
            cluster_key=key,
            example_descriptions=grp["description"].head(5).tolist(),
            txn_count=int(len(grp)),
            suggested_tag=tag,
            suggested_regex=regex,
            suggested_direction=direction,
            confidence="medium",
            reasoning=reasoning,
        ))
    return out


def ruleset_summary(rules) -> list[dict]:
    return [{"tag": r.tag, "regex": r.regex.pattern} for r in rules]
