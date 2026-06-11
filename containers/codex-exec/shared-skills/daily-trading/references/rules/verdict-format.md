# Verdict Format

## External-Call Ban

`first-verdict` and `second-verdict` agents use only the immutable snapshots supplied by the Main agent. KIS, MCP, web, network, shell, file reads outside supplied persona text, canonical artifact writes, and recollection are forbidden.

Verdict agents may write only their own human-review Markdown companion file described below. They must not write, update, or repair `run.json`, `decision-brief.json`, `verdict-first.json`, `verdict-second.json`, account artifacts, execution artifacts, wrapper files, or another agent's Markdown file.

## Human-Review Markdown Companion

Each verdict agent writes one companion Markdown file for human inspection:

```text
reports/runs/<run_id>/verdicts/<stage>--<agent_role>--<task_name>.md
```

Filename rules:

- `<stage>` is exactly `first-verdict` or `second-verdict`.
- `<agent_role>` is the launcher `agent_role` after replacing every character except ASCII letters, digits, `_`, `-`, and `.` with `-`.
- `<task_name>` is the launcher `task_name` with the same safe-name replacement.
- Do not include spaces, slashes, timestamps, persona display names, symbol names, or free-form suffixes in the filename.

Content rules:

- Write Korean prose for human review.
- Cover every supplied eligible asset for that agent.
- Use exactly one Markdown table for per-symbol judgements.
- The table header must be exactly:

  ```markdown
  | 종목 | 점수 | confidence(확신도) | 의견(판단) |
  |---|---:|---:|---|
  ```

- `종목` must include both symbol id and symbol name, for example `005930 삼성전자`.
- `점수` is a human-review score from `0` to `10`.
- `confidence(확신도)` is a human-review confidence score from `0` to `10`.
- `의견(판단)` is a concise Korean judgement with the decision, key evidence, key risk or missing context.
- Include exactly one row for every supplied eligible asset for that agent.
- Do not add evidence that is absent from `decision-brief.json` or `verdict-first.json`.
- Do not include account numbers, account product codes, tokens, app keys, app secrets, HTS IDs, authorization headers, cookies, or raw credentials.
- Do not add extra per-symbol bullet lists or alternate per-symbol sections outside the table.

The companion Markdown file is never the source of truth for scoring, target quantities, reconciliation, order calculation, or order gates. The JSON response captured in the launcher wrapper remains the only machine-validated verdict output. If the Markdown file is missing, malformed, incomplete, or inconsistent with JSON, record a warning and continue from the JSON verdict data.

The Markdown table is never parsed as machine input. For `first-verdict` Markdown, use the same `0` to `10` score and confidence as the canonical JSON result. If Markdown and JSON disagree, record a warning and use the JSON result. For `second-verdict` Markdown, choose a portfolio-attractiveness `점수` consistent with the judge's target quantity, current exposure, and rationale; this score does not replace target quantities, target cash, reconciliation, or order gates.

## `decision-brief.json` Input

`decision-brief.json` is the input for `first-verdict` and `second-verdict` agents.

The brief contains compact per-symbol price/chart, optional financial, optional KIS news/disclosure, account exposure, eligibility, evidence mode, and error summaries. It must not contain long raw API payloads, full article text, repeated source detail, or sensitive account/authentication values.

Financial/news absence is not a negative signal by itself. If a symbol has a resolved identifier, name, current-or-last price, and observation time, agents must score it from the available price/chart and account evidence and must not lower score, lower confidence, exclude it, or remove its target solely because financial or news data is absent.

## `first-verdict`

The selected five `first-verdict` personas independently score every eligible symbol.

Score meanings:

| Score | Meaning |
|---:|---|
| `9-10` | strong buy candidate |
| `7-8` | buy candidate |
| `5-6` | hold / neutral |
| `3-4` | reduce / sell candidate |
| `0-2` | strong sell candidate |

Each agent returns:

```json
{
  "agent_id": "",
  "persona": "",
  "stage": "first-verdict",
  "human_markdown_path": "reports/runs/<run_id>/verdicts/first-verdict--<agent_role>--<task_name>.md",
  "symbols": [
    {
      "symbol_id": "",
      "symbol_name": "",
      "score": 5,
      "confidence": 5,
      "evidence": [],
      "risks": [],
      "missing_data": []
    }
  ],
  "errors": []
}
```

Rules:

- `score` is an integer from `0` to `10`.
- `confidence` is an integer from `0` to `10`.
- Evidence cites fields and observation dates from `decision-brief.json`.
- If financial/news/market-status data is absent, `missing_data` may mention it as context, but the absence must not lower score or confidence by itself.
- One symbol's data cannot support another symbol.
- An agent cannot see other verdict outputs.
- `human_markdown_path` is informational. Missing or invalid path metadata must not invalidate otherwise valid symbol scores.

### First-Score Aggregation

For each symbol, ignore structurally invalid agent scores but record their errors.

```text
mean_score = sum(valid scores) / count(valid scores)
```

Map `mean_score` to the final first score:

```text
final_first_score = round_half_up(mean_score)
```

`final_first_score` is an integer from `0` to `10`.

If no valid score exists, exclude the symbol from `second-verdict` and trading.

## `second-verdict`

The `second-verdict` set contains eligible symbols with `final_first_score >= 7` plus every eligible current holding. Mid- and long-term judges independently compare the set at portfolio level.

Each judge returns:

```json
{
  "agent_id": "",
  "persona": "mid-term | long-term",
  "stage": "second-verdict",
  "human_markdown_path": "reports/runs/<run_id>/verdicts/second-verdict--<agent_role>--<task_name>.md",
  "portfolio": {
    "target_cash_amount": 0,
    "cash_rationale": [],
    "price_chart_view": "",
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
- Consider relative attractiveness, duplicate exposure, current weight, price/chart conditions, and same-day fills.
- Same-day fills are a repeated-trade guard; do not subtract them from current live holdings.
- No fixed minimum cash ratio, maximum cash ratio, or fixed investment ratio is allowed.
- Judges cannot add a symbol that is absent from the `second-verdict` set.
- `human_markdown_path` is informational. Missing or invalid path metadata must not invalidate otherwise valid target quantities.

### Reconciliation

The Main agent reconciles valid judge results:

- Final target holding quantity: with exactly two valid judge results, use the average of the two target quantities and round down if fractional.
- Final target cash amount: with exactly two valid judge results, use the average of the two target cash amounts.
- If either judge result is invalid or missing for a symbol, set no final target and exclude it from orders.
- Validate the reconciled quantities and cash against `account-before-verdict.json` total assets using the immutable price snapshot valuation prices.
- If reconciled holdings plus target cash exceed total assets, reduce only buy-side target quantities in reverse relative-attractiveness order until the targets fit. Do not increase any sell target.
- If reconciled holdings plus target cash are below total assets, add the unexplained remainder to final target cash. Do not increase target quantities merely to consume cash.
- Apply explicit user limits and latest account constraints after reconciliation.
- Record every judge input, valid value, excluded value, and final result in `verdict-second.json`.

## Allowed Terms

| Field | Allowed values |
|---|---|
| artifact status | `success`, `partial`, `failed` |
| first score | integer `0` to `10` |
| eligibility | `eligible_for_verdict=true/false` |
| order direction | `buy`, `sell`, `none` |
| execution result | `submitted`, `skipped`, `blocked`, `failed` |
