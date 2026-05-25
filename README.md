# Bank Statement Auto-Tag & Metrics Agent

FlexiLoans Hackathon — Topic 3 (Underwriting)

---

## What We Built — Problem Statement Coverage

### Core features (required)

- [x] **Transaction classifier** — every transaction tagged as `salary`, `business_inflow`, `emi_payment`, `cheque_bounce`, `gambling`, `regular_expense`, or `other`
- [x] **Rules-first engine with LLM fallback** — 41 bank-agnostic seed rules cover 90–97% of transactions; unrecognised clusters go to a local Ollama LLM, keeping cost near zero
- [x] **ABB (Average Bank Balance)** — computed from daily closing balance, forward-filled across days with no transactions
- [x] **BTO (Bank Turnover)** — monthly inflow excluding reversals and internal transfers; median and per-month breakdown
- [x] **Bounce ratio** — `count(cheque_bounce) / count(emi_payment + cheque_bounce)`
- [x] **Obligation-to-Income (OTI)** — `sum(emi_payment) / sum(salary + business_inflow)` per month
- [x] **Anomaly detection** — circular transfer detection: finds money going A→B→C→A within 14 days at similar amounts
- [x] **Confidence scoring per metric** — each metric carries a confidence level (high/medium/low) based on what share of contributing transactions were tagged by seed rules vs LLM
- [x] **Structured output** — `credit_input.json` with metrics, anomalies, tag breakdown, and confidence scores
- [x] **Human-readable summary** — `summary.md` narrating income stability, repayment behaviour, risk signals, and anomalies in plain language
- [x] **Synthetic bank statement** — 6 months, ~240 transactions, with planted salary, EMI, cheque bounces, and a circular transfer pattern

### Beyond the MVP

- [x] **PDF ingestion** — parses real multi-page bank PDFs (tested on 22-page ICICI business current account); handles NEFT, RTGS, IMPS, UPI, ACH, NACH, POS, ATM descriptions across banks
- [x] **Learning loop** — LLM suggestions are shown to the user for approve/deny; approved tags are promoted into `ruleset.json` so future statements need fewer LLM calls
- [x] **AI Analysis tab** — streams a full credit analyst report (5 sections: income profile, stability, repayment, risk signals, recommendation) via Ollama SSE
- [x] **Cost transparency** — shows Claude-equivalent cost per run vs a pure-LLM baseline, with projected savings at 50K statements/day
- [x] **Web UI** — single-page app: upload → rule tagging → LLM review → metrics → AI report; no CLI required

### Discussion angles addressed

- [x] **Cost at scale** — cost counter shows ₹0 (Ollama, local) vs Claude Sonnet 4.6 equivalent; pure-LLM baseline and 50K-stmts/day savings computed live
- [x] **Hybrid classifier** — rules handle the bulk (90%+), LLM only sees the ambiguous tail; ruleset grows with use so LLM share shrinks over time
- [x] **Latency** — rule engine runs in milliseconds; LLM call is batched (all untagged clusters in one prompt) and streams token-by-token to the UI
- [ ] **GST cross-check** — not implemented (optional per problem statement)
- [ ] **Precision/recall on held-out test set** — not measured; hand-verified on synthetic + real ICICI statement

---

## Running

```bash
pip install -r requirements.txt
python3 -m uvicorn app:app --host 0.0.0.0 --port 8000 --reload
```

Open `http://localhost:8000`

---

## Stack

- **Backend**: FastAPI + Python 3.11
- **PDF parsing**: pdfplumber (multi-page, ICICI / HDFC / SBI / Axis formats)
- **Rule engine**: regex + amount/direction filters, priority-ordered, persisted in `data/ruleset.json`
- **LLM fallback**: Ollama (local, zero API cost) — streamed via SSE
- **Frontend**: Vanilla JS + CSS, served as static files

---

## Project layout

```
bank-statement-agent/
├── app.py                   # FastAPI routes + session state
├── data/
│   ├── ruleset.json         # 41 general seed rules (bank-agnostic)
│   └── synthetic_statement.csv
├── src/
│   ├── rule_engine.py       # Rule loader, matcher, persister
│   ├── pdf_parser.py        # Multi-format PDF → DataFrame
│   ├── entity_extractor.py  # Counterparty name normalisation
│   ├── llm_ollama.py        # Ollama SSE streaming + cluster parsing
│   ├── metrics.py           # ABB, BTO, bounce ratio, OTI
│   ├── anomaly.py           # Circular transfer detection
│   └── output.py            # credit_input.json + summary.md
└── static/
    ├── index.html
    ├── app.js
    └── style.css
```

---

## Workflow

1. **Upload** a bank statement (CSV or PDF)
2. **Rule engine** tags ~90–97% of transactions instantly with no LLM cost
3. **LLM clustering** groups unrecognised transactions and suggests tags + regexes
4. **Approve / deny** suggestions — approved rules are saved to `ruleset.json` permanently
5. **Metrics** tab shows ABB, BTO, bounce ratio, OTI with per-metric confidence
6. **AI Analysis** tab streams a credit analyst report via Ollama

---

## Ruleset

`data/ruleset.json` ships with 41 general-purpose rules covering:

| Category | Tag | Examples |
|---|---|---|
| Bounce / return | `cheque_bounce` | ECS RTN, NACH RETURN, MANDATE FAIL |
| Salary | `salary` | NEFT+SAL, PAYROLL, PFMS |
| Gambling | `gambling` | Dream11, Rummy Circle, Bet365 |
| EMI / loan repayment | `emi_payment` | EMI, ACH+LOAN, lender names, BIL/BANK |
| Business inflow | `business_inflow` | RTGS, NEFT CR, IMPS CR, UPI CR, CASH DEP, SETTLEMENT |
| Tax / GST | `regular_expense` | GST CHALLAN, ADVANCE TAX, GIB/, TDS |
| POS / ATM | `regular_expense` | POS DEBIT, ATM NWD, CASH WDL |
| UPI / IMPS debit | `regular_expense` | UPI DR, IMPS DR |
| Utility bills | `regular_expense` | Airtel, JIO, electricity, water |
| Internal transfer | `other` | INFT, OWN ACCOUNT, SWEEP |

Rules are bank-agnostic — patterns match universal keywords (NEFT, RTGS, IMPS, UPI, ACH, ECS, NACH, CLG) rather than any single bank's internal format codes.
