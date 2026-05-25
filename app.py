"""FastAPI backend for the Bank Statement Agent."""
from __future__ import annotations
import io
import json
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import httpx
import pandas as pd
import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from rule_engine import load_ruleset, apply_rules, append_rule, make_rule_dict, CONFIDENCE_BY_SOURCE
from llm_ollama import cluster_untagged, stream_tags
from metrics import all_metrics
from anomaly import detect_circular_transfers
from output import build_credit_input, build_summary, append_run_log
from pricing import cost_inr, pure_llm_cost_per_statement, daily_projection
from entity_extractor import enrich_dataframe
from pdf_parser import parse_pdf, HAS_PDFPLUMBER

RULESET = ROOT / "data" / "ruleset.json"
RUN_LOG = ROOT / "data" / "run_log.json"
OUT_DIR = ROOT / "data" / "out"
SAMPLE_CSV = ROOT / "data" / "synthetic_statement.csv"
OLLAMA_URL = "http://localhost:11434"

app = FastAPI(title="Bank Statement Agent")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")


# ── Session state ──────────────────────────────────────────────────────────────

@dataclass
class Session:
    session_id: str
    df: Optional[pd.DataFrame] = None
    tagged: Optional[pd.DataFrame] = None
    clusters: Optional[pd.DataFrame] = None
    suggestions: dict = field(default_factory=dict)   # cluster_id -> dict
    decisions: dict = field(default_factory=dict)      # cluster_id -> pending|approved|denied
    usage: dict = field(default_factory=dict)
    statement_id: str = ""
    size_before: int = 0
    rules_learned: list = field(default_factory=list)
    finalized: bool = False
    credit_input: Optional[dict] = None


sessions: dict[str, Session] = {}


def get_session(session_id: str) -> Session:
    if session_id not in sessions:
        raise HTTPException(404, f"Session not found: {session_id}")
    return sessions[session_id]


def safe_records(df: pd.DataFrame) -> list[dict]:
    return json.loads(df.where(df.notna(), None).to_json(orient="records"))


# ── Static & root ──────────────────────────────────────────────────────────────

@app.get("/")
async def index():
    return FileResponse(str(ROOT / "static" / "index.html"))


# ── Utility endpoints ──────────────────────────────────────────────────────────

@app.get("/api/ruleset-info")
async def ruleset_info():
    data = json.loads(RULESET.read_text())["rules"]
    by_source = {}
    for r in data:
        s = r.get("source", "seed")
        by_source[s] = by_source.get(s, 0) + 1
    return {"count": len(data), "by_source": by_source}


@app.get("/api/models")
async def list_models():
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{OLLAMA_URL}/api/tags")
            models = [m["name"] for m in resp.json().get("models", [])]
        return {"models": models, "available": True}
    except Exception as e:
        return {"models": [], "available": False, "error": str(e)}


@app.get("/api/run-log")
async def get_run_log():
    if not RUN_LOG.exists():
        return []
    return json.loads(RUN_LOG.read_text())


@app.get("/api/ruleset")
async def get_ruleset():
    return json.loads(RULESET.read_text())["rules"]


# ── Upload & rule pass ─────────────────────────────────────────────────────────

@app.post("/api/upload")
async def upload(file: UploadFile = File(None), use_sample: str = Form("false")):
    statement_id = f"upload_{uuid.uuid4().hex[:6]}"

    if use_sample == "true" or file is None:
        if not SAMPLE_CSV.exists():
            raise HTTPException(400, "Sample CSV not found. Run: python3 -m src.generate_data")
        df = pd.read_csv(SAMPLE_CSV)
        statement_id = f"sample_{uuid.uuid4().hex[:6]}"
    else:
        content = await file.read()
        fname = (file.filename or "").lower()
        try:
            if fname.endswith(".pdf"):
                if not HAS_PDFPLUMBER:
                    raise HTTPException(400, "pdfplumber not installed. Run: pip install pdfplumber")
                df = parse_pdf(content)
            else:
                df = pd.read_csv(io.BytesIO(content))
        except ValueError as e:
            raise HTTPException(400, str(e))

    required = {"date", "description", "amount", "type", "balance"}
    missing = required - set(df.columns)
    if missing:
        raise HTTPException(400, f"Missing columns: {missing}. Found: {list(df.columns)}")

    df["amount"] = pd.to_numeric(df["amount"], errors="coerce").fillna(0)
    df["balance"] = pd.to_numeric(df["balance"], errors="coerce").fillna(0)

    rules = load_ruleset(RULESET)
    tagged = apply_rules(df, rules)

    # Enrich every row with counterparty, payment_method, lender, categorisation
    enrich_dataframe(tagged, direction_col="type")

    clusters = cluster_untagged(tagged)

    sess = Session(
        session_id=str(uuid.uuid4()),
        df=df, tagged=tagged, clusters=clusters,
        statement_id=statement_id,
        size_before=len(rules),
    )
    sessions[sess.session_id] = sess

    n_total = len(tagged)
    n_tagged = int(tagged["tag"].notna().sum())
    n_untagged = n_total - n_tagged

    rich_cols = ["date", "description", "counterparty", "amount", "type",
                 "transaction_categorisation", "payment_method", "lender",
                 "vendor", "tag", "rule_id", "confidence"]
    # only keep cols that exist
    rich_cols = [c for c in rich_cols if c in tagged.columns]

    tagged_preview = tagged[tagged["tag"].notna()][rich_cols].head(200)
    untagged_preview = tagged[tagged["tag"].isna()][
        [c for c in ["date", "description", "counterparty", "amount", "type",
                      "payment_method", "lender"] if c in tagged.columns]
    ]

    tag_counts = tagged["tag"].value_counts().fillna(0).astype(int).to_dict()

    return {
        "session_id": sess.session_id,
        "statement_id": statement_id,
        "total": n_total,
        "tagged": n_tagged,
        "untagged": n_untagged,
        "coverage_pct": round(n_tagged / n_total * 100, 1) if n_total else 0,
        "tag_counts": tag_counts,
        "tagged_rows": safe_records(tagged_preview),
        "untagged_rows": safe_records(untagged_preview),
    }


# ── LLM streaming ──────────────────────────────────────────────────────────────

@app.get("/api/llm-stream")
async def llm_stream(session_id: str, model: str):
    sess = get_session(session_id)
    if sess.clusters is None or sess.clusters.empty:
        async def empty():
            yield 'data: {"type":"done","total_clusters":0,"input_tokens":0,"output_tokens":0,"elapsed_ms":0}\n\n'
        return StreamingResponse(empty(), media_type="text/event-stream")

    sess.suggestions = {}
    sess.decisions = {}
    sess.usage = {}

    rules_raw = json.loads(RULESET.read_text())["rules"]
    tagged_labeled = sess.tagged[sess.tagged["tag"].notna()]

    async def event_gen():
        async for event_type, data in stream_tags(
            sess.clusters, tagged_labeled, rules_raw, model, OLLAMA_URL
        ):
            if event_type == "cluster":
                cid = data["cluster_id"]
                sess.suggestions[cid] = data
                sess.decisions[cid] = "pending"
            elif event_type == "done":
                sess.usage = data
            payload = json.dumps({"type": event_type, **data} if isinstance(data, dict) else {"type": event_type, "token": data})
            yield f"data: {payload}\n\n"

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Approve / Deny ─────────────────────────────────────────────────────────────

@app.post("/api/approve")
async def approve(body: dict):
    sess = get_session(body["session_id"])
    cid = int(body["cluster_id"])
    tag = body.get("tag", sess.suggestions.get(cid, {}).get("suggested_tag", "other"))
    regex = body.get("regex", sess.suggestions.get(cid, {}).get("suggested_regex", ""))
    direction = body.get("direction") or sess.suggestions.get(cid, {}).get("suggested_direction")

    rule = make_rule_dict(tag=tag, regex=regex, direction=direction,
                          amount_min=None, amount_max=None,
                          source="user_confirmed", priority=70)
    append_rule(RULESET, rule)
    sess.decisions[cid] = "approved"
    sess.rules_learned.append(rule)
    return {"success": True, "rule_id": rule["id"], "tag": tag}


@app.post("/api/deny")
async def deny(body: dict):
    sess = get_session(body["session_id"])
    cid = int(body["cluster_id"])
    sess.decisions[cid] = "denied"
    return {"success": True}


# ── Finalize ───────────────────────────────────────────────────────────────────

@app.post("/api/finalize")
async def finalize(body: dict):
    sess = get_session(body["session_id"])
    if sess.finalized:
        # Return cached result
        credit_path = OUT_DIR / f"{sess.statement_id}_credit_input.json"
        if credit_path.exists():
            return json.loads(credit_path.read_text())

    # Re-apply rules (picks up newly approved rules)
    rules_after = load_ruleset(RULESET)
    tagged = apply_rules(sess.df, rules_after)

    # Fill remaining untagged with LLM suggestions at medium confidence
    cluster_df = cluster_untagged(tagged)
    for idx in tagged[tagged["tag"].isna()].index:
        if idx in cluster_df.index:
            cid = int(cluster_df.loc[idx, "cluster_id"])
            sug = sess.suggestions.get(cid)
            if sug:
                tagged.at[idx, "tag"] = sug["suggested_tag"]
                tagged.at[idx, "rule_id"] = f"llm_cluster_{cid}"
                tagged.at[idx, "tag_source"] = "llm_suggested"
                tagged.at[idx, "confidence"] = CONFIDENCE_BY_SOURCE["llm_suggested"]

    tagged["tag"] = tagged["tag"].fillna("other")
    tagged["tag_source"] = tagged["tag_source"].fillna("unmatched")
    tagged["confidence"] = tagged["confidence"].fillna("low")

    # Re-enrich with final tags so categorisation strings are accurate
    enrich_dataframe(tagged, direction_col="type")

    metrics = all_metrics(tagged)
    anomalies = detect_circular_transfers(tagged)
    credit_input = build_credit_input(tagged, metrics, anomalies)
    sess.credit_input = credit_input
    summary_md = build_summary(credit_input, sess.statement_id)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / f"{sess.statement_id}_credit_input.json").write_text(json.dumps(credit_input, indent=2))
    (OUT_DIR / f"{sess.statement_id}_summary.md").write_text(summary_md)
    tagged.to_csv(OUT_DIR / f"{sess.statement_id}_tagged.csv", index=False)

    rule_tagged = int(tagged["tag_source"].isin(["seed", "user_confirmed"]).sum())
    llm_tagged = int(tagged["tag_source"].isin(["llm_suggested"]).sum())
    in_tok = int(sess.usage.get("input_tokens", 0))
    out_tok = int(sess.usage.get("output_tokens", 0))

    log_entry = append_run_log(
        RUN_LOG,
        statement_id=sess.statement_id,
        total_txns=int(len(tagged)),
        rule_tagged=rule_tagged,
        llm_tagged=llm_tagged,
        llm_input_tokens=in_tok,
        llm_output_tokens=out_tok,
        ruleset_size_before=sess.size_before,
        ruleset_size_after=len(rules_after),
        rules_learned=len(sess.rules_learned),
    )

    # Cloud equivalent (what same tokens would cost on Sonnet 4.6)
    cloud_equiv = cost_inr(in_tok, out_tok)
    pure_baseline = pure_llm_cost_per_statement(int(len(tagged)))

    sess.finalized = True

    return {
        **credit_input,
        "summary_md": summary_md,
        "run": {
            **log_entry,
            "local_cost_inr": 0,
            "cloud_equiv_inr": round(cloud_equiv, 4),
            "pure_baseline_inr": round(pure_baseline, 4),
            "projected_daily_local": 0,
            "projected_daily_baseline": round(daily_projection(pure_baseline), 2),
        },
        "rules_learned": sess.rules_learned,
        "tagged_rows": safe_records(tagged[[c for c in [
            "date", "description", "counterparty", "amount", "type",
            "transaction_categorisation", "payment_method", "lender",
            "vendor", "tag", "tag_source", "confidence"
        ] if c in tagged.columns]]),
    }


# ── AI Deep Analysis ───────────────────────────────────────────────────────────

def _build_analysis_prompt(credit_input: dict) -> str:
    m = credit_input.get("metrics", {})
    abb   = m.get("abb", {}).get("value", 0)
    bto   = m.get("bto", {}).get("value", 0)
    bnc   = m.get("bounce_ratio", {})
    oti   = m.get("oti", {})
    tags  = credit_input.get("tag_breakdown", {})
    anom  = credit_input.get("anomalies", [])

    anom_text = "None detected." if not anom else "\n".join(
        f"- {a.get('type','').replace('_',' ').title()}: counterparty {a.get('counterparty','')} "
        f"({a.get('outflow_amount',0):,.0f} out → {a.get('inflow_amount',0):,.0f} in, "
        f"{a.get('spread_days',0)} days, {a.get('severity','')} severity)"
        for a in anom
    )

    tag_lines = "\n".join(f"  {k}: {v} transactions" for k, v in tags.items())

    return f"""You are a senior credit analyst at a lending institution reviewing a 6-month bank statement.

UNDERWRITING METRICS:
  Average Bank Balance (ABB):      ₹{abb:,.0f}
  Monthly Bank Turnover (BTO):     ₹{bto:,.0f}  (median of monthly inflows)
  Bounce / ECS Failure Ratio:      {bnc.get('value',0)*100:.1f}%  ({bnc.get('bounce_count',0)} failures / {bnc.get('emi_count',0)} obligations)
  Obligation-to-Income Ratio (OTI): {oti.get('value',0)*100:.1f}%  (₹{oti.get('total_emi',0):,.0f} EMI / ₹{oti.get('total_inflow',0):,.0f} inflow)

TRANSACTION BREAKDOWN:
{tag_lines}

ANOMALIES:
{anom_text}

Write a structured credit analysis report with these exact section headings:

## Credit Profile Summary
(2–3 sentences on overall financial health and reliability)

## Income Stability
(Regularity, amount, source quality of credits. Call out any gaps or irregularities.)

## Repayment Behavior
(EMI payment consistency, bounce incidents, obligation management quality)

## Risk Signals
(Gambling, circular transfers, high cash withdrawals, salary delays, any red flags)

## Credit Recommendation
(Start with one of: ✅ APPROVE / ⚠️ CONDITIONAL APPROVE / ❌ REJECT — then 2–3 sentences of specific reasoning. If approving, suggest a maximum loan amount and rationale.)

Be specific with rupee amounts. Write in professional banking language. Keep each section to 3–4 sentences maximum."""


@app.get("/api/ai-analysis")
async def ai_analysis(session_id: str, model: str):
    sess = get_session(session_id)
    if not sess.credit_input:
        async def _err():
            yield 'data: {"type":"error","message":"Finalize the report first before running AI analysis."}\n\n'
        return StreamingResponse(_err(), media_type="text/event-stream")

    prompt = _build_analysis_prompt(sess.credit_input)

    async def event_gen():
        try:
            async with httpx.AsyncClient(timeout=300) as client:
                async with client.stream(
                    "POST", f"{OLLAMA_URL}/api/generate",
                    json={"model": model, "prompt": prompt, "stream": True},
                ) as resp:
                    if resp.status_code != 200:
                        body = await resp.aread()
                        yield f'data: {json.dumps({"type":"error","message":f"Ollama {resp.status_code}: {body.decode()[:200]}"})}\n\n'
                        return
                    full = ""
                    async for line in resp.aiter_lines():
                        if not line.strip():
                            continue
                        try:
                            chunk = json.loads(line)
                        except Exception:
                            continue
                        token = chunk.get("response", "")
                        if token:
                            full += token
                            yield f'data: {json.dumps({"type":"token","text":token})}\n\n'
                        if chunk.get("done"):
                            yield f'data: {json.dumps({"type":"done","full_text":full})}\n\n'
                            break
        except httpx.ConnectError:
            yield 'data: {"type":"error","message":"Cannot connect to Ollama — is it running?"}\n\n'
        except Exception as e:
            yield f'data: {json.dumps({"type":"error","message":str(e)})}\n\n'

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)
