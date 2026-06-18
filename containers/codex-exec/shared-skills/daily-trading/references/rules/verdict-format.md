# Verdict Format

## Shared Rules

Verdict agents use only supplied immutable artifacts, persona text, and this format. They may use read-only local shell commands such as `cat` and `jq` only for explicitly listed artifact/persona/rule files. They must not call KIS, MCP, web, network, account/order APIs, or external data sources; read unrelated files; recollect; write files; or write canonical artifacts.

Verdict agents return compact JSON only. They must not emit Markdown, diffs, code fences, long prose, raw artifact excerpts, or raw source payloads. `human_markdown_path` is informational only; the Main agent creates one human-review Markdown sidecar from parsed JSON:

```text
reports/runs/<run_id>/verdicts/<stage>--<agent_role>--<task_name>.md
```

`<stage>` is `first-verdict` or `second-verdict`. Sanitize `agent_role` and `task_name` by replacing every character except ASCII letters, digits, `_`, `-`, and `.` with `-`. Do not add timestamps, symbol names, persona names, spaces, slashes, or suffixes.

Main-generated `first-verdict` sidecar content:

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

Main-generated `second-verdict` sidecar content:

- Korean prose.
- Exactly one per-symbol Markdown table.
- Header exactly:

  ```markdown
  | 종목 | 목표수량 | 상대매력도 | 판단코드 | 의견(판단) |
  |---|---:|---:|---|---|
  ```

- One row for every supplied second-verdict asset.
- `종목` includes symbol id and name.
- `목표수량` is the non-negative integer target.
- `상대매력도` is the integer rank from `relative_attractiveness_rank`.
- `판단코드` is `reason_code`.
- `의견(판단)` is `one_line_reason`.
- No extra per-symbol sections or sensitive values.

The sidecar is never machine input. JSON captured by the launcher is authoritative. Missing, malformed, or inconsistent sidecars are warnings only.

`decision-brief.json` is the canonical verdict input. It should contain compact price/chart, optional financial/news/market-status summaries, account exposure, eligibility, evidence mode, and errors. Absence of optional financial/news/market-status data is context only; it must not lower score, lower confidence, exclude a symbol, remove a target, or block orders by itself.

Verdict sub-agents receive launcher-created lossless `verdict-inputs/` slices containing only the listed `symbol_ids`. `first-verdict` reads a `verdict-core` slice derived from `decision-brief.json`; `second-verdict` reads `verdict-core` plus a selected-symbol slice derived from `verdict-first.json`. Raw prompt fallback is forbidden for verdict stages. Verdict sub-agents may use read-only local shell commands such as `cat` and `jq` only for explicitly listed artifact/persona/rule files. Do not load unrelated symbols, raw memory caches, optional source files, secrets, or unlisted paths.

## `first-verdict`

Selected four first-verdict personas independently score every eligible symbol: `analyst-blackrock`, `analyst-fidelity`, `analyst-jpmorgan`, and `analyst-morganstanley`. `analyst-fidelity` includes quality, value, momentum, and low-volatility factor checks formerly covered by `analyst-statestreet`.

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
      "reason_code": "hold_neutral",
      "one_line_reason": "",
      "missing_data": []
    }
  ],
  "errors": []
}
```

Rules:

- `score` and `confidence` are integers from `0` to `10`.
- `reason_code` is a short snake_case label. `one_line_reason` is one concise Korean sentence citing only supplied evidence when useful.
- Do not return long `evidence`, `risks`, `rationale`, or prose arrays.
- One symbol's data cannot support another symbol.
- Agents cannot see other verdict outputs.
- `human_markdown_path` is informational.

Aggregation by Main agent:

```text
confidence_weight = confidence / 10
confidence_adjusted_score = 5 + ((score - 5) * confidence_weight)
mean_score = sum(valid scores) / count(valid scores)
mean_confidence_adjusted_score = sum(valid confidence_adjusted_scores) / count(valid confidence_adjusted_scores)
final_first_score = round_half_up(mean_confidence_adjusted_score)
```

`confidence_adjusted_score` pulls low-confidence scores toward neutral `5`; `confidence=0` becomes `5`, and `confidence=10` preserves the original `score`.
If no valid score exists, exclude that symbol from `second-verdict` and trading.

## `second-verdict`

Input set = eligible symbols with `final_first_score >= 7` plus every eligible `holding` symbol from `$check-portfolio`. Only `judge-midterm` compares that set at portfolio level. If its required output is missing or unusable, retry only the failed `judge-midterm` task at most two times.

Return JSON:

```json
{
  "agent_id": "",
  "persona": "mid-term",
  "stage": "second-verdict",
  "human_markdown_path": "reports/runs/<run_id>/verdicts/second-verdict--<agent_role>--<task_name>.md",
  "symbols": [
    {
      "symbol_id": "",
      "symbol_name": "",
      "target_holding_quantity": 0,
      "relative_attractiveness_rank": 1,
      "reason_code": "hold_target",
      "one_line_reason": ""
    }
  ],
  "errors": []
}
```

Rules:

- `target_holding_quantity` is a non-negative integer.
- Every second-verdict symbol receives a target quantity, including reduce-to-zero holdings.
- Consider relative attractiveness, duplicate exposure, current weight, price/chart conditions, and the supplied selected-symbol first-verdict results.
- Treat `final_first_score` as the confidence-adjusted first-verdict score: `5` is neutral, below `5` is a sell/reduce opinion, and above `5` is a buy/increase opinion.
- If a symbol's first-verdict score is missing, unavailable, or unusable, treat its score as neutral `5` instead of failing the judgment.
- First-verdict scores are judgment inputs, not hard buy/sell gates.
- No fixed cash ratio or fixed investment ratio.
- The judge cannot add symbols outside the supplied set.
- Do not return long `cash_rationale`, `duplicate_exposure_limits`, `price_chart_view`, `rationale`, `risks`, or prose arrays.

Validation by Main agent:

- Use the single valid `judge-midterm` target quantities as the canonical `verdict-second.json` target.
- If the valid judge result is missing for a symbol, set no final target and exclude it from orders.
- Validate target holdings against total assets and the latest available account/order gate using immutable price snapshot valuations.
- If targets exceed assets, reduce only buy-side quantities in reverse relative-attractiveness order. Do not increase sell targets.
- If targets are below assets, leave the remainder as residual cash. Do not create, report, or optimize toward a target cash value.
- Preserve total-asset/cash, duplicate exposure, high-price concentration, active order, same-day repeat, and market open gates.
- Apply latest account constraints after target validation.

## Allowed Values

| Field | Values |
|---|---|
| artifact status | `success`, `partial`, `failed` |
| score/confidence | integer `0` to `10` |
| eligibility | `eligible_for_verdict=true/false` |
| order direction | `buy`, `sell`, `none` |
| execution result | `submitted`, `skipped`, `blocked`, `failed` |
