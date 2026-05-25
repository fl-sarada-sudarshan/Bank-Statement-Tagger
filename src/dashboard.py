"""Interactive Streamlit dashboard:
  Upload statement → live rule-tagging → review untagged → approve/deny LLM
  suggestions → rules get added → final metrics + anomalies + learning curve + cost.

Run with:  streamlit run src/dashboard.py
"""
from __future__ import annotations
import json
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd
import streamlit as st
import plotly.express as px
import plotly.graph_objects as go

from rule_engine import (
    load_ruleset, apply_rules, append_rule, make_rule_dict, CONFIDENCE_BY_SOURCE,
)
from llm_fallback import cluster_untagged, call_llm, ruleset_summary
from metrics import all_metrics
from anomaly import detect_circular_transfers
from output import build_credit_input, build_summary, append_run_log
from pricing import cost_inr, pure_llm_cost_per_statement, daily_projection

ROOT = Path(__file__).resolve().parents[1]
RULESET = ROOT / "data" / "ruleset.json"
RUN_LOG = ROOT / "data" / "run_log.json"
OUT_DIR = ROOT / "data" / "out"
SAMPLE_CSV = ROOT / "data" / "synthetic_statement.csv"

st.set_page_config(page_title="Bank Statement Agent", layout="wide", page_icon="🏦")

# ---------- helpers ----------
def conf_bar(pct: float) -> str:
    blocks = int(round(pct * 12))
    return "█" * blocks + "░" * (12 - blocks)


def conf_color(band: str) -> str:
    return {"HIGH": "🟢", "MEDIUM": "🟡", "LOW": "🔴"}.get(band, "⚪")


def reset_session():
    for k in list(st.session_state.keys()):
        del st.session_state[k]


# ---------- session state defaults ----------
ss = st.session_state
ss.setdefault("stage", "upload")  # upload | rule_pass | review | done
ss.setdefault("df", None)
ss.setdefault("tagged", None)
ss.setdefault("suggestions", [])
ss.setdefault("decisions", {})  # cluster_id -> 'pending'|'approved'|'denied'
ss.setdefault("usage", {"input_tokens": 0, "output_tokens": 0})
ss.setdefault("rules_learned", [])
ss.setdefault("statement_id", None)
ss.setdefault("size_before", 0)


# ---------- sidebar ----------
st.sidebar.title("🏦 Statement Agent")
st.sidebar.caption("Rules-first tagging with LLM fallback that learns from your approvals.")
st.sidebar.divider()
st.sidebar.markdown(f"**Stage:** `{ss.stage}`")
ruleset_count = len(json.loads(RULESET.read_text())["rules"])
st.sidebar.markdown(f"**Current ruleset:** {ruleset_count} rules")
if st.sidebar.button("🔄 Reset session", use_container_width=True):
    reset_session()
    st.rerun()

st.title("Bank Statement Auto-Tag & Metrics Agent")

# =====================================================================
# STAGE 1: Upload
# =====================================================================
if ss.stage == "upload":
    st.subheader("Step 1 — Upload a bank statement")
    st.caption("CSV must have columns: `date, description, amount, type, balance`. "
               "`type` is `credit` or `debit`.")

    cc = st.columns([3, 1])
    uploaded = cc[0].file_uploader("Drop CSV here", type=["csv"])
    use_sample = cc[1].button("📂 Use sample CSV", use_container_width=True)

    if uploaded is not None:
        df = pd.read_csv(uploaded)
        ss.statement_id = f"upload_{uuid.uuid4().hex[:6]}"
    elif use_sample and SAMPLE_CSV.exists():
        df = pd.read_csv(SAMPLE_CSV)
        ss.statement_id = f"sample_{uuid.uuid4().hex[:6]}"
    else:
        st.stop()

    df["amount"] = df["amount"].astype(float)
    df["balance"] = df["balance"].astype(float)
    ss.df = df
    ss.stage = "rule_pass"
    st.rerun()


# =====================================================================
# STAGE 2: Rule pass (live)
# =====================================================================
if ss.stage == "rule_pass":
    st.subheader(f"Step 2 — Rule-based tagging  ({ss.statement_id})")
    df = ss.df
    st.write(f"Loaded **{len(df)} transactions**. Applying rule engine…")
    progress = st.progress(0)
    status = st.empty()

    rules = load_ruleset(RULESET)
    ss.size_before = len(rules)
    # cheap animation
    for i in range(20):
        progress.progress((i + 1) / 20)
        status.caption(f"matching rules… {(i+1)*5}%")
        time.sleep(0.02)

    tagged = apply_rules(df, rules)
    ss.tagged = tagged

    n_total = len(tagged)
    n_tagged = int(tagged["tag"].notna().sum())
    n_untagged = n_total - n_tagged
    coverage = n_tagged / n_total * 100 if n_total else 0

    c1, c2, c3 = st.columns(3)
    c1.metric("Transactions", n_total)
    c2.metric("Rule-tagged", n_tagged, f"{coverage:.1f}% coverage")
    c3.metric("Need LLM fallback", n_untagged)

    with st.expander(f"📋 See {n_tagged} rule-tagged transactions"):
        st.dataframe(tagged[tagged["tag"].notna()][
            ["date", "description", "amount", "type", "tag", "rule_id", "confidence"]
        ], use_container_width=True, height=300)

    st.divider()
    if n_untagged == 0:
        st.success("All transactions tagged by rules — no LLM needed.")
        if st.button("➡️ Continue to results", type="primary"):
            ss.stage = "done"
            st.rerun()
    else:
        st.warning(f"⚠️  {n_untagged} transactions could not be tagged by rules. "
                   "Send them to the LLM for clustering + tag suggestions?")
        st.dataframe(tagged[tagged["tag"].isna()][
            ["date", "description", "amount", "type"]
        ], use_container_width=True, height=240)
        if st.button("🤖 Run LLM fallback", type="primary"):
            with st.spinner("Clustering untagged transactions and calling Claude…"):
                clusters = cluster_untagged(tagged)
                rules = load_ruleset(RULESET)
                suggestions, usage = call_llm(
                    clusters,
                    tagged[tagged["tag"].notna()],
                    ruleset_summary(rules),
                )
                ss.suggestions = suggestions
                ss.usage = usage
                ss.decisions = {s.cluster_id: "pending" for s in suggestions}
            ss.stage = "review"
            st.rerun()


# =====================================================================
# STAGE 3: Review LLM suggestions (approve / deny live)
# =====================================================================
if ss.stage == "review":
    st.subheader("Step 3 — Review LLM tag suggestions")
    st.caption(f"Claude proposed tags for **{len(ss.suggestions)} clusters** "
               f"(tokens: in={ss.usage.get('input_tokens',0)} · out={ss.usage.get('output_tokens',0)} · "
               f"cost ≈ ₹{cost_inr(ss.usage.get('input_tokens',0), ss.usage.get('output_tokens',0)):.4f}). "
               "Approve to add the rule to your ruleset permanently — denied suggestions are only used for this run.")

    decided = sum(1 for v in ss.decisions.values() if v != "pending")
    st.progress(decided / max(1, len(ss.suggestions)),
                text=f"{decided}/{len(ss.suggestions)} reviewed")

    for s in ss.suggestions:
        with st.container(border=True):
            decision = ss.decisions.get(s.cluster_id, "pending")
            head = st.columns([4, 1])
            head[0].markdown(f"### Cluster #{s.cluster_id} — {s.txn_count} transaction(s)")
            badge = {"pending": "⏳ Pending", "approved": "✅ Approved", "denied": "❌ Denied"}[decision]
            head[1].markdown(f"**{badge}**")

            st.markdown("**Sample descriptions:**")
            for d in s.example_descriptions[:3]:
                st.caption(f"• {d}")

            cols = st.columns(4)
            new_tag = cols[0].selectbox(
                "Tag", ["salary", "business_inflow", "emi_payment", "cheque_bounce",
                        "circular_transfer", "gambling", "regular_expense", "other"],
                index=["salary", "business_inflow", "emi_payment", "cheque_bounce",
                       "circular_transfer", "gambling", "regular_expense", "other"].index(s.suggested_tag),
                key=f"tag_{s.cluster_id}",
            )
            new_regex = cols[1].text_input("Regex", value=s.suggested_regex, key=f"regex_{s.cluster_id}")
            cols[2].text_input("Direction", value=str(s.suggested_direction or ""), key=f"dir_{s.cluster_id}", disabled=True)
            cols[3].text_input("Confidence", value=s.confidence, key=f"conf_{s.cluster_id}", disabled=True)
            st.caption(f"💭 _Claude's reasoning:_ {s.reasoning}")

            bcol = st.columns([1, 1, 6])
            if bcol[0].button("✅ Approve", key=f"app_{s.cluster_id}", disabled=(decision != "pending"), type="primary"):
                rule = make_rule_dict(
                    tag=new_tag,
                    regex=new_regex,
                    direction=s.suggested_direction,
                    amount_min=None, amount_max=None,
                    source="user_confirmed", priority=70,
                )
                append_rule(RULESET, rule)
                ss.rules_learned.append(rule)
                ss.decisions[s.cluster_id] = "approved"
                st.toast(f"Added rule {rule['id']} → {rule['tag']}", icon="✅")
                st.rerun()
            if bcol[1].button("❌ Deny", key=f"den_{s.cluster_id}", disabled=(decision != "pending")):
                ss.decisions[s.cluster_id] = "denied"
                st.rerun()

    st.divider()
    all_done = all(v != "pending" for v in ss.decisions.values()) if ss.decisions else True
    cols = st.columns([1, 1, 4])
    if cols[0].button("▶️ Finalise & compute metrics", type="primary", disabled=not all_done):
        ss.stage = "done"
        st.rerun()
    if cols[1].button("⏭️ Skip remaining (deny all)"):
        for cid, v in ss.decisions.items():
            if v == "pending":
                ss.decisions[cid] = "denied"
        st.rerun()


# =====================================================================
# STAGE 4: Done — metrics, anomalies, learning curve, cost
# =====================================================================
if ss.stage == "done":
    st.subheader("Step 4 — Final analysis")

    df = ss.df
    rules_after = load_ruleset(RULESET)
    tagged = apply_rules(df, rules_after)

    # For still-untagged, fall back to LLM-suggested tags (even denied ones tag this run at medium conf)
    sug_by_cluster = {s.cluster_id: s for s in ss.suggestions}
    cluster_df = cluster_untagged(tagged)
    for idx in tagged[tagged["tag"].isna()].index:
        if idx in cluster_df.index:
            cid = int(cluster_df.loc[idx, "cluster_id"])
            s = sug_by_cluster.get(cid)
            if s:
                tagged.at[idx, "tag"] = s.suggested_tag
                tagged.at[idx, "rule_id"] = f"llm_cluster_{cid}"
                tagged.at[idx, "tag_source"] = "llm_suggested"
                tagged.at[idx, "confidence"] = CONFIDENCE_BY_SOURCE["llm_suggested"]
    tagged["tag"] = tagged["tag"].fillna("other")
    tagged["tag_source"] = tagged["tag_source"].fillna("unmatched")
    tagged["confidence"] = tagged["confidence"].fillna("low")

    metrics = all_metrics(tagged)
    anomalies = detect_circular_transfers(tagged)
    credit_input = build_credit_input(tagged, metrics, anomalies)
    summary_md = build_summary(credit_input, ss.statement_id)

    # Persist outputs + log
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / f"{ss.statement_id}_credit_input.json").write_text(json.dumps(credit_input, indent=2))
    (OUT_DIR / f"{ss.statement_id}_summary.md").write_text(summary_md)
    tagged.to_csv(OUT_DIR / f"{ss.statement_id}_tagged.csv", index=False)

    rule_tagged_final = int(tagged["tag_source"].isin(["seed", "user_confirmed"]).sum())
    llm_tagged_final = int(tagged["tag_source"].isin(["llm_suggested"]).sum())

    # Only log once per stage entry
    if not ss.get("_logged"):
        append_run_log(
            RUN_LOG,
            statement_id=ss.statement_id,
            total_txns=int(len(tagged)),
            rule_tagged=rule_tagged_final,
            llm_tagged=llm_tagged_final,
            llm_input_tokens=int(ss.usage.get("input_tokens", 0)),
            llm_output_tokens=int(ss.usage.get("output_tokens", 0)),
            ruleset_size_before=ss.size_before,
            ruleset_size_after=len(rules_after),
            rules_learned=len(ss.rules_learned),
        )
        ss._logged = True

    run_log = json.loads(RUN_LOG.read_text()) if RUN_LOG.exists() else []
    latest = run_log[-1] if run_log else {}

    # ---- Cost row ----
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("LLM cost this run", f"₹{latest.get('llm_cost_inr', 0):.4f}",
              help=f"{latest.get('llm_input_tokens',0)} in + {latest.get('llm_output_tokens',0)} out tokens")
    c2.metric("Pure-LLM baseline", f"₹{latest.get('pure_llm_baseline_inr', 0):.4f}",
              f"savings ₹{latest.get('savings_inr', 0):.4f}")
    c3.metric("Projected @ 50K/day", f"₹{latest.get('projected_daily_cost_inr', 0):,.0f}",
              f"vs ₹{latest.get('projected_daily_baseline_inr', 0):,.0f} pure-LLM")
    savings_pct = 0
    if latest.get("projected_daily_baseline_inr", 0):
        savings_pct = (1 - latest["projected_daily_cost_inr"] / latest["projected_daily_baseline_inr"]) * 100
    c4.metric("Cost savings", f"{savings_pct:.1f}%", "vs pure-LLM baseline")

    st.divider()

    # ---- Trust dashboard ----
    st.header("🛡️ Trust Dashboard")
    m = metrics
    rows = [
        ("ABB (Avg Bank Balance)", f"₹{m['abb']['value']:,.2f}",
         m['abb'].get('confidence_pct', 1.0), m['abb']['confidence'], "balance-derived"),
        ("BTO (Monthly Turnover)", f"₹{m['bto']['value']:,.2f}",
         m['bto'].get('confidence_pct', 0), m['bto']['confidence'], "median monthly inflow"),
        ("Bounce Ratio", f"{m['bounce_ratio']['value']*100:.2f}%",
         m['bounce_ratio'].get('confidence_pct', 0), m['bounce_ratio']['confidence'],
         f"{m['bounce_ratio'].get('bounce_count',0)} bounces / {m['bounce_ratio'].get('emi_count',0)} EMIs"),
        ("OTI (Obligation-to-Income)", f"{m['oti']['value']*100:.2f}%",
         m['oti'].get('confidence_pct', 0), m['oti']['confidence'],
         f"EMI ₹{m['oti'].get('total_emi',0):,.0f} / Inflow ₹{m['oti'].get('total_inflow',0):,.0f}"),
    ]
    for name, val, pct, band, note in rows:
        cc = st.columns([3, 2, 3, 1, 4])
        cc[0].markdown(f"**{name}**")
        cc[1].markdown(f"### {val}")
        cc[2].code(conf_bar(pct), language=None)
        cc[3].markdown(f"{conf_color(band)} {band}")
        cc[4].caption(note)

    with st.expander("🔎 Trace any tag back to its rule"):
        tag_filter = st.selectbox("Filter by tag", ["(all)"] + sorted(tagged["tag"].unique().tolist()))
        view = tagged if tag_filter == "(all)" else tagged[tagged["tag"] == tag_filter]
        st.dataframe(view[["date", "description", "amount", "type", "tag", "rule_id", "tag_source", "confidence"]],
                     use_container_width=True)

    # ---- Anomalies ----
    st.subheader("⚠️ Anomalies")
    if not anomalies:
        st.success("No anomalies detected.")
    else:
        for a in anomalies:
            with st.container(border=True):
                st.markdown(f"**{a['type'].replace('_', ' ').title()}** — counterparty `{a['counterparty']}` "
                            f"({a['severity'].upper()})")
                cc = st.columns(4)
                cc[0].metric("Outflow", f"₹{a['outflow_amount']:,.0f}", a['outflow_date'])
                cc[1].metric("Inflow", f"₹{a['inflow_amount']:,.0f}", a['inflow_date'])
                cc[2].metric("Spread", f"{a['spread_days']} days")
                cc[3].metric("Δ amount", f"{a['amount_delta_pct']}%")
                for e in a["evidence"]:
                    st.caption(f"• {e}")

    st.divider()

    # ---- Learning curve ----
    st.header("📈 Learning Curve")
    if len(run_log) >= 1:
        log_df = pd.DataFrame(run_log)
        log_df["run_index"] = range(1, len(log_df) + 1)
        cc = st.columns(2)
        with cc[0]:
            fig = px.line(log_df, x="run_index", y="coverage_pct", markers=True,
                          title="Rule-based coverage % across runs",
                          labels={"coverage_pct": "% tagged by rules", "run_index": "run #"})
            fig.update_traces(line_color="#16a34a")
            fig.update_yaxes(range=[0, 100])
            learn_runs = log_df[log_df["rules_learned_this_run"] > 0]
            for _, r in learn_runs.iterrows():
                fig.add_annotation(x=r["run_index"], y=r["coverage_pct"],
                                   text=f"+{int(r['rules_learned_this_run'])} rules",
                                   showarrow=True, arrowhead=2, ay=-30)
            st.plotly_chart(fig, use_container_width=True)
        with cc[1]:
            fig2 = px.bar(log_df, x="run_index", y="ruleset_size_after",
                          title="Ruleset size after each run",
                          labels={"ruleset_size_after": "# rules", "run_index": "run #"})
            fig2.update_traces(marker_color="#2563eb")
            st.plotly_chart(fig2, use_container_width=True)

        fig3 = go.Figure()
        fig3.add_trace(go.Scatter(x=log_df["run_index"], y=log_df["llm_cost_inr"],
                                  name="Our agent", mode="lines+markers", line=dict(color="#16a34a")))
        fig3.add_trace(go.Scatter(x=log_df["run_index"], y=log_df["pure_llm_baseline_inr"],
                                  name="Pure-LLM baseline", mode="lines+markers",
                                  line=dict(color="#dc2626", dash="dash")))
        fig3.update_layout(title="Cost per statement — agent vs pure-LLM baseline",
                           xaxis_title="run #", yaxis_title="₹")
        st.plotly_chart(fig3, use_container_width=True)

    # ---- Rules learned this run ----
    if ss.rules_learned:
        with st.expander(f"✨ {len(ss.rules_learned)} rule(s) learned this run"):
            st.dataframe(pd.DataFrame([{
                "id": r["id"], "tag": r["tag"], "regex": r["match"]["description_regex"],
                "source": r["source"],
            } for r in ss.rules_learned]), use_container_width=True)

    with st.expander("📜 Full ruleset"):
        rs = json.loads(RULESET.read_text())["rules"]
        st.dataframe(pd.DataFrame([{
            "id": r["id"], "tag": r["tag"], "regex": r["match"]["description_regex"],
            "direction": r["match"].get("direction"), "source": r["source"],
            "priority": r["priority"],
        } for r in rs]), use_container_width=True)

    with st.expander("📄 Human-readable summary"):
        st.markdown(summary_md)

    st.divider()
    if st.button("📤 Analyse another statement", type="primary"):
        reset_session()
        st.rerun()
