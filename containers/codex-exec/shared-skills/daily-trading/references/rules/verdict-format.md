# Verdict Format

## External-Call Ban

First- and second-verdict agents use only the immutable snapshots supplied by the main agent. KIS, MCP, web, network, shell, file reads outside supplied persona text, file writes, and recollection are forbidden.

## First Verdict

The seven analyst and ten juror personas independently score every eligible symbol.

Score meanings:

| Score | Meaning |
|---:|---|
| `+2` | strong buy candidate |
| `+1` | buy candidate |
| `0` | hold / neutral |
| `-1` | reduce / sell candidate |
| `-2` | strong sell candidate |

Each agent returns:

```json
{
  "agent_id": "",
  "persona": "",
  "stage": "first-verdict",
  "symbols": [
    {
      "symbol_id": "",
      "symbol_name": "",
      "score": 0,
      "confidence": 1,
      "evidence": [],
      "risks": [],
      "missing_data": []
    }
  ],
  "errors": []
}
```

Rules:

- `score` is one of `-2`, `-1`, `0`, `1`, `2`.
- `confidence` is an integer from `1` to `10`.
- Evidence cites fields and observation dates from `merged.json`.
- One symbol's data cannot support another symbol.
- An agent cannot see other verdict outputs.

### First-Score Aggregation

For each symbol, ignore structurally invalid agent scores but record their errors.

```text
mean_score = sum(valid scores) / count(valid scores)
```

Map `mean_score` to the final first score:

| Mean score | Final first score |
|---:|---:|
| `>= 1.5` | `+2` |
| `>= 0.5 and < 1.5` | `+1` |
| `> -0.5 and < 0.5` | `0` |
| `> -1.5 and <= -0.5` | `-1` |
| `<= -1.5` | `-2` |

If no valid score exists, exclude the symbol from the second verdict and trading.

## Second Verdict

The second-verdict set contains eligible `+2` and `+1` symbols plus every eligible current holding. Short-, mid-, and long-term judges independently compare the set at portfolio level.

Each judge returns:

```json
{
  "agent_id": "",
  "persona": "short-term | mid-term | long-term",
  "stage": "second-verdict",
  "portfolio": {
    "target_cash_amount": 0,
    "cash_rationale": [],
    "market_view": "",
    "duplicate_exposure_limits": []
  },
  "symbols": [
    {
      "symbol_id": "",
      "symbol_name": "",
      "target_holding_quantity": 0,
      "relative_attractiveness_rank": 1,
      "rationale": [],
      "risks": [],
      "same_day_fill_guard": ""
    }
  ],
  "errors": []
}
```

Rules:

- `target_holding_quantity` is a non-negative integer.
- Every second-verdict symbol receives a target quantity, including holdings that should be reduced to zero.
- Consider relative attractiveness, duplicate exposure, current weight, market conditions, and same-day fills.
- Same-day fills are a repeated-trade guard; do not subtract them from current live holdings.
- No fixed minimum cash ratio, maximum cash ratio, or fixed investment ratio is allowed.
- Judges cannot add a symbol that is absent from the second-verdict set.

### Reconciliation

The main agent reconciles valid judge results:

- Final target holding quantity: median of valid judge target quantities for that symbol, rounded down only if a non-integer can occur.
- Final target cash amount: median of valid judge target cash amounts.
- If fewer than two valid judge results exist for a symbol, set no final target and exclude it from orders.
- Validate the reconciled quantities and cash against `account-before-verdict.json` total assets using the immutable market-snapshot valuation prices.
- If reconciled holdings plus target cash exceed total assets, reduce only buy-side target quantities in reverse relative-attractiveness order until the targets fit. Do not increase any sell target.
- If reconciled holdings plus target cash are below total assets, add the unexplained remainder to final target cash. Do not increase target quantities merely to consume cash.
- Apply explicit user limits and latest account constraints after reconciliation.
- Record every judge input, valid value, excluded value, and final result in `verdict-second.json`.

## Allowed Terms

| Field | Allowed values |
|---|---|
| artifact status | `success`, `partial`, `failed` |
| first score | `+2`, `+1`, `0`, `-1`, `-2` |
| eligibility | `eligible_for_verdict=true/false` |
| order direction | `buy`, `sell`, `none` |
| execution result | `submitted`, `skipped`, `blocked`, `failed` |
