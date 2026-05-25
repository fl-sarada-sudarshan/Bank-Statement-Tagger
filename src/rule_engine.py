"""Rule engine: load ruleset, apply rules to transactions, persist additions."""
from __future__ import annotations
import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Optional
import pandas as pd

SOURCE_PRIORITY = {"user_confirmed": 3, "seed": 2, "llm_suggested": 1}
CONFIDENCE_BY_SOURCE = {"user_confirmed": "high", "seed": "high", "llm_suggested": "medium"}


@dataclass
class Rule:
    id: str
    tag: str
    regex: re.Pattern
    direction: Optional[str]
    amount_min: Optional[float]
    amount_max: Optional[float]
    priority: int
    source: str
    confirmations: int

    @classmethod
    def from_dict(cls, d: dict) -> "Rule":
        m = d["match"]
        return cls(
            id=d["id"],
            tag=d["tag"],
            regex=re.compile(m["description_regex"]),
            direction=m.get("direction"),
            amount_min=m.get("amount_min"),
            amount_max=m.get("amount_max"),
            priority=d.get("priority", 50),
            source=d.get("source", "seed"),
            confirmations=d.get("confirmations", 0),
        )

    def matches(self, desc: str, amount: float, direction: str) -> bool:
        if self.direction and self.direction != direction:
            return False
        if self.amount_min is not None and amount < self.amount_min:
            return False
        if self.amount_max is not None and amount > self.amount_max:
            return False
        return bool(self.regex.search(desc))


def load_ruleset(path: Path) -> list[Rule]:
    data = json.loads(Path(path).read_text())
    rules = [Rule.from_dict(r) for r in data["rules"]]
    # Sort: priority desc, then source priority desc, then confirmations desc
    rules.sort(key=lambda r: (r.priority, SOURCE_PRIORITY.get(r.source, 0), r.confirmations), reverse=True)
    return rules


def save_ruleset(path: Path, rules_raw: list[dict]) -> None:
    Path(path).write_text(json.dumps({"rules": rules_raw}, indent=2))


def append_rule(path: Path, new_rule: dict) -> None:
    data = json.loads(Path(path).read_text())
    data["rules"].append(new_rule)
    Path(path).write_text(json.dumps(data, indent=2))


def apply_rules(df: pd.DataFrame, rules: list[Rule]) -> pd.DataFrame:
    """Return df with added columns: tag, rule_id, confidence, source."""
    df = df.copy()
    tags: list[Optional[str]] = [None] * len(df)
    rule_ids: list[Optional[str]] = [None] * len(df)
    confs: list[Optional[str]] = [None] * len(df)
    sources: list[Optional[str]] = [None] * len(df)

    for i, row in enumerate(df.itertuples(index=False)):
        desc = str(row.description)
        amt = float(row.amount)
        direction = str(row.type)
        for r in rules:  # already sorted by priority
            if r.matches(desc, amt, direction):
                tags[i] = r.tag
                rule_ids[i] = r.id
                confs[i] = CONFIDENCE_BY_SOURCE.get(r.source, "medium")
                sources[i] = r.source
                break

    df["tag"] = tags
    df["rule_id"] = rule_ids
    df["confidence"] = confs
    df["tag_source"] = sources
    return df


def make_rule_dict(tag: str, regex: str, direction: Optional[str], amount_min: Optional[float],
                   amount_max: Optional[float], source: str, priority: int = 70) -> dict:
    import uuid
    return {
        "id": f"r_{source[:3]}_{uuid.uuid4().hex[:8]}",
        "tag": tag,
        "match": {
            "description_regex": regex,
            "direction": direction,
            "amount_min": amount_min,
            "amount_max": amount_max,
        },
        "priority": priority,
        "source": source,
        "confirmations": 1 if source == "user_confirmed" else 0,
        "created_at": date.today().isoformat(),
    }
