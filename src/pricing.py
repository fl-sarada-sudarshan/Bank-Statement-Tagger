"""Token -> INR cost helpers and scale projections.

Claude Sonnet 4.6 pricing (USD per million tokens):
  - input:  $3
  - output: $15
USD->INR conversion approximate (held constant for demo): 83.
"""
USD_INR = 83.0
SONNET_INPUT_USD_PER_MTOK = 3.0
SONNET_OUTPUT_USD_PER_MTOK = 15.0

# Pure-LLM baseline assumption: every transaction sent individually.
# Per txn ~ 80 input tokens + 30 output tokens for a tagging call.
BASELINE_INPUT_PER_TXN = 80
BASELINE_OUTPUT_PER_TXN = 30


def cost_inr(input_tokens: int, output_tokens: int) -> float:
    usd = (input_tokens / 1_000_000) * SONNET_INPUT_USD_PER_MTOK + \
          (output_tokens / 1_000_000) * SONNET_OUTPUT_USD_PER_MTOK
    return usd * USD_INR


def pure_llm_cost_per_statement(avg_txns: int) -> float:
    return cost_inr(avg_txns * BASELINE_INPUT_PER_TXN, avg_txns * BASELINE_OUTPUT_PER_TXN)


def daily_projection(cost_per_stmt: float, statements_per_day: int = 50_000) -> float:
    return cost_per_stmt * statements_per_day
