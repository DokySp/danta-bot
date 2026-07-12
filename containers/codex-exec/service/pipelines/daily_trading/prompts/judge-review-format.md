# Judge Review Format

## Shared Rules

Review agents use only supplied immutable artifacts, persona text, and this format. They may use read-only local shell commands such as `cat` and `jq` only for explicitly listed artifact/persona/rule files. They must not call KIS, MCP, web, network, account/order APIs, or external data sources; read unrelated files; recollect; write files; or write canonical artifacts.

Debate exception: the judge may spawn bull/bear debate sub-agents as described in `judge.md`. Spawned debate sub-agents inherit every restriction above (no KIS/MCP/web/network, no file writes, listed files only) and their transcripts are never part of the returned JSON.

Review agents return compact JSON only. They must not emit Markdown, diffs, code fences, long prose, raw artifact excerpts, or raw source payloads. `human_markdown_path` is informational only; the Main agent creates one human-review Markdown sidecar from parsed JSON:

```text
reports/runs/<run_id>/reviews/judge-review--<agent_role>--<task_name>.md
```

Sanitize `agent_role` and `task_name` by replacing every character except ASCII letters, digits, `_`, `-`, and `.` with `-`. Do not add timestamps, symbol names, persona names, spaces, slashes, or suffixes.

Main-generated `judge-review` sidecar content:

- Korean prose.
- Exactly one per-symbol Markdown table.
- Header exactly:

  ```markdown
  | 종목 | 목표금액 | 최종수량 | 상대매력도 | 판단코드 | 의견(판단) |
  |---|---:|---:|---:|---|---|
  ```

- One row for every supplied judge-review asset.
- `종목` includes symbol id and name.
- `목표금액` is `target_position_value_krw`.
- `최종수량` is the non-negative integer final holding quantity derived by Main/pipeline.
- `상대매력도` is the integer rank from `relative_attractiveness_rank`.
- `판단코드` is `reason_code`.
- `의견(판단)` is `one_line_reason`.
- No extra per-symbol sections or sensitive values.

The sidecar is never machine input. JSON captured by the launcher is authoritative. Missing, malformed, or inconsistent sidecars are warnings only.

`judge-review` reads `review-core` plus a selected-symbol slice derived from `analyst-review.json`. Raw prompt fallback is forbidden. Do not load unrelated symbols, raw memory caches, optional source files, secrets, or unlisted paths.

## Candidate Bands (action preconditions)

The judge input set is selected deterministically by the pipeline from unrounded `final_first_score` (the simple mean of included analyst view scores):

- `final_first_score <= 4.0` on a held symbol → **sell candidate**: decide sell (partial or full) or hold. Buying is not allowed.
- `final_first_score >= 6.0` → **buy candidate**: decide buy (initiate or increase) or hold. Selling is not allowed.
- Every other symbol never reaches the judge; holding it is the only outcome. These bands are enforced preconditions, not advisory labels.

The spec supplies `candidate_directions` (symbol → `buy`/`sell`) and `portfolio_snapshot`. `portfolio_snapshot` is read-only context listing every held symbol with its score, quantity, valuation, and pnl so portfolio-level sizing (weights, duplicate exposure, cash allocation) stays informed; the judge must not return decisions for snapshot-only symbols.

## `judge-review`

Return JSON:

```json
{
  "agent_id": "",
  "persona": "final",
  "stage": "judge-review",
  "human_markdown_path": "reports/runs/<run_id>/reviews/judge-review--<agent_role>--<task_name>.md",
  "symbols": [
    {
      "symbol_id": "005930",
      "symbol_name": "삼성전자",
      "target_position_value_krw": 560000,
      "relative_attractiveness_rank": 1,
      "reason_code": "hold_final_quantity",
      "one_line_reason": "기준금액 수준의 목표금액을 유지한다."
    }
  ],
  "errors": []
}
```

Rules:

- `target_position_value_krw` is required for every judge-review symbol. It is the judge's target position value in KRW after this decision.
- `target_position_value_krw` must be numeric and non-negative. `0` is valid when the rationale explicitly says reduce-to-zero or exit.
- `final_holding_quantity` is optional in judge output and is not the judge's sizing decision. Main/pipeline derives it from `target_position_value_krw / price.current_or_last` using Decimal `ROUND_HALF_UP`.
- Use each symbol's `holding_quantity_context.expected_holding_quantity * price.current_or_last` as the explicit baseline position value.
- Direction preconditions are hard constraints validated by the pipeline: a **sell candidate** must return `target_position_value_krw <= baseline`; a **buy candidate** must return `target_position_value_krw >= baseline`. A violating symbol is rejected and produces no order.
- "No additional buy", "no extra exposure", or "추가 확대 없음" is a hold rationale: keep `target_position_value_krw` at the baseline level; do not set it to `0`.
- If `today_trade_timeline_context` shows a same-day buy fill and `target_position_value_krw` is above the baseline, include `additional_buy_reason` with the new evidence or materially changed price/portfolio context supporting the increase.
- `reason_code` and `one_line_reason` must describe the same reduce/hold/increase direction implied by `target_position_value_krw` versus the baseline.
- Consider relative attractiveness, duplicate exposure, current weight, price/chart conditions, `portfolio_snapshot`, and the supplied selected-symbol analyst-review results.
- If a symbol's analyst-review score is missing, unavailable, or unusable, hold it at the baseline instead of failing the judgment.
- For held sell candidates, distinguish `long_term_thesis_intact` from thesis damage: intact thesis favors holding despite the low score; sell when thesis damage, material adverse news/disclosure, or structural deterioration is supported by supplied evidence.
- Judge long-term thesis from supplied evidence only: core investment rationale, material news/disclosure risk, quality/value deterioration, whether a price shock indicates structural damage or short-term volatility, and portfolio weight/concentration.
- For buy candidates, increase only when add conditions are satisfied: quality/value advantage, acceptable risk/allocation, weight/concentration room, and no supplied material adverse news/disclosure. Otherwise hold at the baseline.
- When the evidence is insufficient or conflicting, the default decision is hold at the baseline.
- No fixed cash ratio or fixed investment ratio.
- The judge cannot add symbols outside the supplied candidate set.
- Do not return long `cash_rationale`, `duplicate_exposure_limits`, `price_chart_view`, `rationale`, `risks`, or prose arrays.

Validation by Main agent:

- Use the single valid `judge` target position values as the canonical `judge-review.json` `target_position_value_krw` values.
- Main/pipeline derives canonical `final_holding_quantity` values from `target_position_value_krw / price.current_or_last` using Decimal `ROUND_HALF_UP`.
- Symbols outside the supplied candidate set are dropped with an error and produce no order.
- A sell candidate whose target exceeds the baseline, or a buy candidate whose target is below the baseline, is rejected with an error and produces no order.
- If the valid judge result is missing a target position value for a symbol, set no final holding quantity and exclude it from orders.
- Do not reduce buy-side quantities solely because rounded target shares exceed `target_position_value_krw`; affordability and order availability are handled by existing account/order gates.
- If derived final holdings are below assets, leave the remainder as residual cash. Do not create, report, or optimize toward a cash target value.
- Preserve existing account/order execution checks such as orderable cash, active orders, same-day context, order validity, and market open checks.
