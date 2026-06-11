# Verdict Format

## Shared Rules

Verdict agents use only supplied immutable artifacts, persona text, and this format. They must not call KIS, MCP, web, network, shell, or external data sources; read unrelated files; recollect; or write canonical artifacts.

They may write one human-review Markdown sidecar:

```text
reports/runs/<run_id>/verdicts/<stage>--<agent_role>--<task_name>.md
```

`<stage>` is `first-verdict` or `second-verdict`. Sanitize `agent_role` and `task_name` by replacing every character except ASCII letters, digits, `_`, `-`, and `.` with `-`. Do not add timestamps, symbol names, persona names, spaces, slashes, or suffixes.

Sidecar content:

- Korean prose.
- Exactly one per-symbol Markdown table.
- Header exactly:

  ```markdown
  | 종목 | 점수 | confidence(확신도) | 의견(판단) |
  |---|---:|---:|---|
  ```

- One row for every supplied eligible asset.
- `종목` includes symbol id and name.
- `점수` and `confidence(확신도)` are `0` to `10`.
- `의견(판단)` is concise and cites only supplied evidence.
- No extra per-symbol sections or sensitive values.

The sidecar is never machine input. JSON captured by the launcher is authoritative. Missing, malformed, or inconsistent sidecars are warnings only.

`decision-brief.json` is the verdict input. It should contain compact price/chart, optional financial/news/market-status summaries, account exposure, eligibility, evidence mode, and errors. Absence of optional financial/news/market-status data is context only; it must not lower score, lower confidence, exclude a symbol, remove a target, or block orders by itself.

When the launcher supplies compact verdict specs, it may replace canonical artifact paths with `verdict-inputs/` slices containing only the listed `symbol_ids`. Read only those supplied paths. Do not load unrelated symbols, raw memory caches, or optional source files.

## `first-verdict`

Selected first-verdict personas independently score every eligible symbol.

Score scale:

| Score | Meaning |
|---:|---|
| `9-10` | strong buy candidate |
| `7-8` | buy candidate |
| `5-6` | hold / neutral |
| `3-4` | reduce / sell candidate |
| `0-2` | strong sell candidate |

Return JSON:

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

- `score` and `confidence` are integers from `0` to `10`.
- Evidence cites fields and observation dates from `decision-brief.json`.
- One symbol's data cannot support another symbol.
- Agents cannot see other verdict outputs.
- `human_markdown_path` is informational.

Aggregation by Main agent:

```text
mean_score = sum(valid scores) / count(valid scores)
final_first_score = round_half_up(mean_score)
```

If no valid score exists, exclude that symbol from `second-verdict` and trading.

## `second-verdict`

Input set = eligible symbols with `final_first_score >= 7` plus every eligible current holding. Mid- and long-term judges compare that set at portfolio level.

Return JSON:

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
      "risks": []
    }
  ],
  "errors": []
}
```

Rules:

- `target_holding_quantity` is a non-negative integer.
- Every second-verdict symbol receives a target quantity, including reduce-to-zero holdings.
- Consider relative attractiveness, duplicate exposure, current weight, and price/chart conditions.
- No fixed cash ratio or fixed investment ratio.
- Judges cannot add symbols outside the supplied set.

Reconciliation by Main agent:

- With two valid judges, average target quantities and round down when fractional.
- With two valid judges, average target cash.
- If either judge result is invalid or missing for a symbol, set no final target and exclude it from orders.
- Validate reconciled holdings plus target cash against `account-before-verdict.json` total assets using immutable price snapshot valuations.
- If targets exceed assets, reduce only buy-side quantities in reverse relative-attractiveness order. Do not increase sell targets.
- If targets are below assets, add the unexplained remainder to final target cash. Do not increase quantities merely to spend cash.
- Apply latest account constraints after reconciliation.

## Allowed Values

| Field | Values |
|---|---|
| artifact status | `success`, `partial`, `failed` |
| score/confidence | integer `0` to `10` |
| eligibility | `eligible_for_verdict=true/false` |
| order direction | `buy`, `sell`, `none` |
| execution result | `submitted`, `skipped`, `blocked`, `failed` |
