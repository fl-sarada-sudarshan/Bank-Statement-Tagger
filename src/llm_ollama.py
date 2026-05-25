"""Ollama-based LLM tagger: clusters untagged transactions, streams token-by-token."""
from __future__ import annotations
import json
import re
import time
from dataclasses import dataclass
from typing import AsyncGenerator, Optional
import httpx
import pandas as pd

ALLOWED_TAGS = [
    "salary", "business_inflow", "emi_payment", "cheque_bounce",
    "circular_transfer", "gambling", "regular_expense", "other",
]

OLLAMA_URL = "http://localhost:11434"


@dataclass
class ClusterSuggestion:
    cluster_id: int
    cluster_key: str
    example_descriptions: list[str]
    txn_count: int
    suggested_tag: str
    suggested_regex: str
    suggested_direction: Optional[str]
    confidence: str
    reasoning: str


def normalize_description(desc: str) -> str:
    s = desc.upper()
    s = re.sub(r"\d+", "#", s)
    s = re.sub(r"[A-Z0-9._-]+@[A-Z]+", "@HANDLE", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def cluster_untagged(df: pd.DataFrame) -> pd.DataFrame:
    untagged = df[df["tag"].isna()].copy()
    if untagged.empty:
        untagged["cluster_key"] = pd.Series(dtype=str)
        untagged["cluster_id"] = pd.Series(dtype=int)
        return untagged
    untagged["cluster_key"] = untagged["description"].map(normalize_description)
    keys = {k: i for i, k in enumerate(sorted(untagged["cluster_key"].unique()))}
    untagged["cluster_id"] = untagged["cluster_key"].map(keys)
    return untagged


def build_prompt(clusters_df: pd.DataFrame, tagged_df: pd.DataFrame, ruleset_rules: list[dict]) -> str:
    examples = {}
    for tag, grp in tagged_df.groupby("tag"):
        examples[tag] = grp.head(2)["description"].tolist()
    examples_str = "\n".join(f"  {tag}: {descs}" for tag, descs in examples.items())

    cluster_list = []
    for cid, grp in clusters_df.groupby("cluster_id"):
        cluster_list.append({
            "cluster_id": int(cid),
            "direction": grp["type"].mode().iloc[0],
            "count": int(len(grp)),
            "sample_descriptions": grp["description"].head(3).tolist(),
            "amount_range": [float(grp["amount"].min()), float(grp["amount"].max())],
        })

    compact_rules = [{"tag": r["tag"], "regex": r["match"]["description_regex"]} for r in ruleset_rules[:15]]

    example_output = json.dumps([
        {"cluster_id": 0, "suggested_tag": "regular_expense",
         "suggested_regex": "(?i)FOOD|SWIGGY|ZOMATO", "suggested_direction": "debit",
         "confidence": "high", "reasoning": "Recurring food-delivery debits from Swiggy/Zomato."}
    ], indent=2)

    return f"""You are a credit analyst tagging Indian bank statement transactions for loan underwriting.

ALLOWED TAGS: {", ".join(ALLOWED_TAGS)}

EXISTING RULES already handled — do not duplicate them:
{json.dumps(compact_rules, indent=2)}

TAGGED EXAMPLES for reference:
{examples_str}

CLUSTERS TO CLASSIFY:
{json.dumps(cluster_list, indent=2)}

Return a JSON array — one object per cluster. Each object must have EXACTLY these keys:
  cluster_id        (integer — match from input)
  suggested_tag     (one of the ALLOWED TAGS above)
  suggested_regex   (valid Python regex matching this cluster's descriptions)
  suggested_direction  ("credit" | "debit" | null)
  confidence        ("high" | "medium" | "low")
  reasoning         (one sentence explaining the classification)

Example of the exact format expected:
{example_output}

Output ONLY the raw JSON array. No markdown fences, no prose, no explanation."""


def parse_clusters(response: str) -> list[dict]:
    """Robustly extract cluster list regardless of how the model wrapped it."""
    response = response.strip()
    # Strip markdown fences
    response = re.sub(r"```(?:json)?\s*\n?", "", response).replace("```", "").strip()

    def _valid(items: list) -> list[dict]:
        return [i for i in items if isinstance(i, dict) and "cluster_id" in i and "suggested_tag" in i]

    # 1. Direct parse
    try:
        parsed = json.loads(response)
        if isinstance(parsed, list):
            return _valid(parsed)
        if isinstance(parsed, dict):
            for key in ("clusters", "results", "suggestions", "data", "items", "tags"):
                if key in parsed and isinstance(parsed[key], list):
                    v = _valid(parsed[key])
                    if v:
                        return v
            for val in parsed.values():
                if isinstance(val, list):
                    v = _valid(val)
                    if v:
                        return v
    except json.JSONDecodeError:
        pass

    # 2. Find first JSON array in text
    m = re.search(r"\[[\s\S]*?\]", response)
    if m:
        try:
            v = _valid(json.loads(m.group(0)))
            if v:
                return v
        except json.JSONDecodeError:
            pass

    # 3. Find first JSON object containing a list
    m2 = re.search(r"\{[\s\S]*?\}", response)
    if m2:
        try:
            obj = json.loads(m2.group(0))
            if isinstance(obj, dict):
                for val in obj.values():
                    if isinstance(val, list):
                        v = _valid(val)
                        if v:
                            return v
        except json.JSONDecodeError:
            pass

    # 4. Scrape individual objects
    items = []
    for m3 in re.finditer(r'\{[^{}]*"cluster_id"[^{}]*\}', response, re.DOTALL):
        try:
            obj = json.loads(m3.group(0))
            if "cluster_id" in obj and "suggested_tag" in obj:
                items.append(obj)
        except Exception:
            pass
    return items


async def list_models(ollama_url: str = OLLAMA_URL) -> list[str]:
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{ollama_url}/api/tags")
            return [m["name"] for m in resp.json().get("models", [])]
    except Exception:
        return []


async def stream_tags(
    clusters_df: pd.DataFrame,
    tagged_df: pd.DataFrame,
    ruleset_rules: list[dict],
    model: str,
    ollama_url: str = OLLAMA_URL,
) -> AsyncGenerator[tuple[str, object], None]:
    """Yields (event_type, data) tuples:
    - ('thinking', str token)
    - ('cluster', dict)
    - ('done', dict with stats)
    - ('error', dict)
    """
    if clusters_df.empty:
        yield "done", {"total_clusters": 0, "input_tokens": 0, "output_tokens": 0, "elapsed_ms": 0}
        return

    prompt = build_prompt(clusters_df, tagged_df, ruleset_rules)
    full_response = ""
    start = time.time()
    input_tokens = 0
    output_tokens = 0

    try:
        async with httpx.AsyncClient(timeout=300) as client:
            async with client.stream(
                "POST",
                f"{ollama_url}/api/generate",
                json={"model": model, "prompt": prompt, "stream": True},
            ) as resp:
                if resp.status_code != 200:
                    body = await resp.aread()
                    yield "error", {"message": f"Ollama error {resp.status_code}: {body.decode()[:200]}"}
                    return

                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    token = chunk.get("response", "")
                    full_response += token
                    if token:
                        yield "thinking", token

                    if chunk.get("done"):
                        input_tokens = chunk.get("prompt_eval_count", len(prompt.split()))
                        output_tokens = chunk.get("eval_count", len(full_response.split()))
                        break

    except httpx.ConnectError:
        yield "error", {"message": "Cannot connect to Ollama at localhost:11434 — is it running?"}
        return
    except Exception as e:
        yield "error", {"message": str(e)}
        return

    elapsed = int((time.time() - start) * 1000)

    items = parse_clusters(full_response)
    for item in items:
        cid = int(item.get("cluster_id", -1))
        grp = clusters_df[clusters_df["cluster_id"] == cid]
        if grp.empty:
            continue
        examples = grp.head(5)
        yield "cluster", {
            "cluster_id": cid,
            "cluster_key": grp["cluster_key"].iloc[0],
            "example_descriptions": examples["description"].tolist(),
            "example_amounts": [float(a) for a in examples["amount"].tolist()],
            "direction": grp["type"].mode().iloc[0] if "type" in grp.columns else None,
            "amount_range": [float(grp["amount"].min()), float(grp["amount"].max())],
            "txn_count": int(len(grp)),
            "suggested_tag": item.get("suggested_tag", "other"),
            "suggested_regex": item.get("suggested_regex", ""),
            "suggested_direction": item.get("suggested_direction"),
            "confidence": item.get("confidence", "medium"),
            "reasoning": item.get("reasoning", ""),
        }

    yield "done", {
        "total_clusters": len(items),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "elapsed_ms": elapsed,
    }
