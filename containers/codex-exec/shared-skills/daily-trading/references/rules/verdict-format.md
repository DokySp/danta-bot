# Verdict Format

## External-Call Ban

`first-verdict`, `second-verdict`, and `final-risk-verdict` agents use only the immutable snapshots supplied by the Main agent. KIS, MCP, web, network, shell, file reads outside supplied persona text, file writes, and recollection are forbidden.

## `decision-brief.json` Input

`decision-brief.json` is the default input for all verdict agents. Do not give verdict agents raw `merged.json` unless the user explicitly changes this contract.

The brief contains compact per-symbol market, financial, KIS news/disclosure, account exposure, eligibility, evidence mode, and error summaries. It must not contain long raw API payloads, full article text, repeated source detail, or sensitive account/authentication values.

`evidence_mode="price_only"` symbols are still eligible when they have a resolved identifier, name, current-or-last price, and observation time. Agents must score them with lower confidence and explicit missing-data notes instead of excluding them solely because financial or news data is absent.

For `final-risk-verdict`, `price_only` remains order-eligible evidence when the symbol is eligible and the order candidate satisfies account, market-status, order-type, user-limit, and same-day-fill constraints. Missing, partial, failed, or no-data financial/news evidence may be recorded as warning or lower confidence, but it is not by itself a reason to return `blocked` or `needs_review` for `order_cash`, `order_resv`, demo submission, or real submission.

## `first-verdict`

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
- Evidence cites fields and observation dates from `decision-brief.json`.
- For `price_only` symbols, evidence must cite the price snapshot and observation date, and `missing_data` must mention absent financial/news evidence when absent.
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

If no valid score exists, exclude the symbol from `second-verdict` and trading.

## `second-verdict`

The `second-verdict` set contains eligible `+2` and `+1` symbols plus every eligible current holding. Short-, mid-, and long-term judges independently compare the set at portfolio level.

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
- Every `second-verdict` symbol receives a target quantity, including holdings that should be reduced to zero.
- Consider relative attractiveness, duplicate exposure, current weight, market conditions, and same-day fills.
- Same-day fills are a repeated-trade guard; do not subtract them from current live holdings.
- No fixed minimum cash ratio, maximum cash ratio, or fixed investment ratio is allowed.
- Judges cannot add a symbol that is absent from the `second-verdict` set.

### Reconciliation

The Main agent reconciles valid judge results:

- Final target holding quantity: median of valid judge target quantities for that symbol, rounded down only if a non-integer can occur.
- Final target cash amount: median of valid judge target cash amounts.
- If fewer than two valid judge results exist for a symbol, set no final target and exclude it from orders.
- Validate the reconciled quantities and cash against `account-before-verdict.json` total assets using the immutable market-snapshot valuation prices.
- If reconciled holdings plus target cash exceed total assets, reduce only buy-side target quantities in reverse relative-attractiveness order until the targets fit. Do not increase any sell target.
- If reconciled holdings plus target cash are below total assets, add the unexplained remainder to final target cash. Do not increase target quantities merely to consume cash.
- Apply explicit user limits and latest account constraints after reconciliation.
- Record every judge input, valid value, excluded value, and final result in `verdict-second.json`.

## `final-risk-verdict`

The `final-risk-verdict` sub-agent reviews the Main agent's order candidates after `account-before-order.json` is refreshed and before any order submission.

Inputs:

- `decision-brief.json`
- `verdict-second.json`
- sanitized `account-before-order.json`
- the Main agent's order candidate list

Forbidden:

- recalculating target quantities
- replacing or overriding judge results
- adding new order candidates
- changing direction or quantity
- calling KIS, MCP, web, network, shell, or any external source
- submitting, reserving, correcting, or cancelling orders
- writing files

The `final-risk-verdict` returns:

```json
{
  "agent_id": "",
  "persona": "final-risk",
  "stage": "final-risk-verdict",
  "result": "approved | blocked | needs_review",
  "approved_order_ids": [],
  "risk_checks": [
    {
      "check": "",
      "status": "pass | fail | review",
      "evidence": "",
      "affected_orders": []
    }
  ],
  "blocking_reasons": [],
  "review_reasons": [],
  "errors": []
}
```

Rules:

- `approved` means every order candidate passed `final-risk-verdict` review as provided.
- `blocked` means at least one required risk gate failed and no order may be submitted.
- `needs_review` means the evidence is insufficient or ambiguous and no order may be submitted.
- Missing, invalid, or `failed` `final-risk-verdict` output blocks order submission.
- The Main agent writes `final-order-verdict.json` and enforces the block/approval result.
- `price_only` evidence mode, missing financial evidence, or missing news evidence is not insufficient or ambiguous evidence by itself when the candidate otherwise satisfies the execution gates.
- `expected_holding_quantity` is the pre-candidate expected holding quantity after already-active pending and reserved quantities are considered. It is valid for this field to differ from `target_holding_quantity`; consistency must be checked through `additional_required_quantity` and `validated_order_quantity`.

## Allowed Terms

| Field | Allowed values |
|---|---|
| artifact status | `success`, `partial`, `failed` |
| first score | `+2`, `+1`, `0`, `-1`, `-2` |
| eligibility | `eligible_for_verdict=true/false` |
| `final-risk-verdict` result | `approved`, `blocked`, `needs_review` |
| order direction | `buy`, `sell`, `none` |
| execution result | `submitted`, `skipped`, `blocked`, `failed` |
