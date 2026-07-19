# Judge Review Format

## Shared Rules

Review agents use only supplied immutable artifacts, persona text, and this format. They may use read-only local shell commands such as `cat` and `jq` only for explicitly listed artifact/persona/rule files. They must not call KIS, MCP, web, network, account/order APIs, or external data sources; read unrelated files; recollect; write files; or write canonical artifacts.

Debate boundary: the Python pipeline runs persistent bull/bear sessions before judge-review and supplies one normalized `debate_artifact`. The judge must not spawn or resume debate agents, request another round, or include debate transcripts in the returned JSON.

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
      "one_line_reason": "기준금액 수준의 목표금액을 유지한다.",
      "thesis_definition": {
        "core_rationale": "quality moat and pricing power",
        "invalidation_conditions": [
          {"condition_id": "margin-compression", "description": "gross margin drops below prior guidance"}
        ]
      },
      "thesis_assessment": {
        "status": "damaged",
        "matched_invalidation_condition_ids": ["margin-compression"],
        "cited_argument_ids": ["005930-bear-opening-1"]
      }
    }
  ],
  "errors": []
}
```

`thesis_definition` and `thesis_assessment` are optional per symbol and used only as described below.

Rules:

- `target_position_value_krw` is required for every judge-review symbol. It is the judge's target position value in KRW after this decision.
- `target_position_value_krw` must be numeric and non-negative. `0` is valid when the rationale explicitly says reduce-to-zero or exit.
- `final_holding_quantity` is optional in judge output and is not the judge's sizing decision. Main/pipeline derives it from `target_position_value_krw / price.current_or_last` using Decimal `ROUND_HALF_UP`.
- Use each symbol's `holding_quantity_context.expected_holding_quantity * price.current_or_last` as the explicit baseline position value.
- Direction preconditions are hard constraints validated by the pipeline: a **sell candidate** must return `target_position_value_krw <= baseline`; a **buy candidate** must return `target_position_value_krw >= baseline`. A violating symbol is rejected and produces no order.
- "No additional buy", "no extra exposure", or "추가 확대 없음" is a hold rationale: keep `target_position_value_krw` at the baseline level; do not set it to `0`.
- If `today_trade_timeline_context` confirms a same-day buy, or its `collection_status` is `partial`/`unavailable` so same-day buy history is unknown, `target_position_value_krw` may exceed the baseline only when `additional_buy_reason` supplies new evidence or materially changed price/portfolio context supporting the increase. Missing history alone is never the reason.
- Treat an empty `recent_trade_context.recent_submitted_trades` list as confirmed absence only when `coverage_status=complete`; with `partial`/`unavailable` coverage, recent trade history is unknown and its absence is non-directional.
- `reason_code` and `one_line_reason` must describe the same reduce/hold/increase direction implied by `target_position_value_krw` versus the baseline.
- Consider relative attractiveness, duplicate exposure, current weight, price/chart conditions, `portfolio_snapshot`, and the supplied selected-symbol analyst-review results that contain usable evidence.
- Optional evidence marked `missing`, `failed`, `empty`, `unavailable`, or `excluded_from_aggregation` is non-directional. Do not use its absence to justify hold, reduce, or increase decisions, and do not cite it as decisive evidence in `reason_code` or `one_line_reason`.
- Do not use optional-domain coverage counts or completeness to decide whether the supplied usable evidence is sufficient. Judge sufficiency from the directional strength and conflict of the usable evidence that is actually supplied.
- If a symbol's analyst-review score is missing, unavailable, or unusable, hold it at the baseline instead of failing the judgment.
- For held sell candidates, distinguish `long_term_thesis_intact` from thesis damage using supplied usable evidence: supported intact thesis favors holding despite the low score; sell when thesis damage, material adverse news/disclosure, or structural deterioration is supported by supplied evidence. Lack of damage evidence alone does not establish an intact thesis.
- Judge long-term thesis from supplied evidence only: core investment rationale, material news/disclosure risk, quality/value deterioration, whether a price shock indicates structural damage or short-term volatility, and portfolio weight/concentration.
- For buy candidates, increase only when add conditions are satisfied: quality/value advantage, acceptable risk/allocation, and weight/concentration room. Apply material adverse news/disclosure only when usable news/disclosure evidence is supplied; unavailable news is neutral rather than favorable or adverse. Otherwise hold at the baseline.
- When the supplied usable evidence itself is insufficient or conflicting, the default decision is hold at the baseline without citing optional evidence absence as the decision reason.
- No fixed cash ratio or fixed investment ratio.
- The judge cannot add symbols outside the supplied candidate set.
- Do not return long `cash_rationale`, `duplicate_exposure_limits`, `price_chart_view`, `rationale`, `risks`, or prose arrays.

### Position-thesis fields (protected-loss reductions)

- Each symbol's `review-core` input carries `prior_thesis_context`: `status` (`available`/`no_prior_thesis`) and, when available, a `thesis_definition` (`core_rationale`, `invalidation_conditions[]` with `condition_id`/`description`) recorded by an earlier successful run. Those prior conditions are immutable for evaluating a reduction in the current run: a newly returned definition cannot be applied retroactively to that assessment. A valid definition returned for an actual buy/increase becomes the successor prior for later runs. When `prior_thesis_context.status` is `no_prior_thesis`, a definition or assessment newly produced in the current run can never authorize that same run's reduction. When a valid prior exists, the current run's `thesis_assessment` is exactly the semantic input that may authorize a reduction, once matched against the prior's `invalidation_conditions` and verified against `debate_artifact` (see Validation by Main agent below).
- `thesis_definition` is valid only when `core_rationale` is non-empty and at least one `invalidation_conditions[]` entry has both a non-empty `condition_id` and a non-empty `description`. An empty, missing, or otherwise malformed `thesis_definition` is never treated as valid.
- When you decide to buy or increase `target_position_value_krw` above baseline, return a valid `thesis_definition` so a later run has explicit invalidation criteria to assess against. Main/pipeline mechanically rejects the increase (no order, no updated target) when a valid `thesis_definition` is missing or malformed for that symbol.
- For any held symbol whose `symbol_strategy_context.loss_position` is `true` (or `pnl_rate < 0`), always return `thesis_assessment`: `status` (`intact`/`damaged`/`uncertain`), `matched_invalidation_condition_ids[]`, and `cited_argument_ids[]` (decisive `debate_artifact` argument ids). Price loss, index/regime panic, a low score, or missing optional evidence alone never justify `status: damaged`. This applies regardless of whether the symbol is a buy candidate, a sell candidate, or holding at baseline.
- When `prior_thesis_context.status` is `no_prior_thesis` for such a held loss position, also return a valid `thesis_definition` even though the decision is to hold at baseline (or a reduction gets blocked): this bootstraps a real prior for a future run. Main/pipeline never invents this definition; an invalid/missing one is simply not persisted, and the next run still sees `no_prior_thesis`.

Validation by Main agent:

- Use the single valid `judge` target position values as the canonical `judge-review.json` `target_position_value_krw` values.
- Main/pipeline derives canonical `final_holding_quantity` values from `target_position_value_krw / price.current_or_last` using Decimal `ROUND_HALF_UP`.
- Symbols outside the supplied candidate set are dropped with an error and produce no order.
- A sell candidate whose target exceeds the baseline, or a buy candidate whose target is below the baseline, is rejected with an error and produces no order.
- If the valid judge result is missing a target position value for a symbol, set no final holding quantity and exclude it from orders.
- Do not reduce buy-side quantities solely because rounded target shares exceed `target_position_value_krw`; affordability and order availability are handled by existing account/order gates.
- For a held loss position (`symbol_strategy_context.loss_position` or `pnl_rate < 0`), regardless of whether it is a buy candidate, sell candidate, or holding at baseline, a reduction below baseline is only accepted when `thesis_assessment.status` is `damaged`, `matched_invalidation_condition_ids` reference condition ids that already exist in the immutable prior `thesis_definition`, and `cited_argument_ids` reference argument ids that already exist in `judge-debate.json` for that symbol; otherwise Main/pipeline forces `target_position_value_krw` back to baseline and records `protected_loss_gate`. A symbol with no prior `thesis_definition` is bootstrapped the same way, at or below baseline: this run's own assessment cannot both define and invalidate the thesis, and only a valid judge-supplied `thesis_definition` is persisted (never a synthesized placeholder).
- A buy/increase (`target_position_value_krw` above baseline) with a missing or malformed `thesis_definition` is rejected: Main/pipeline records an error and produces no order for that symbol instead of executing the increase.
- If derived final holdings are below assets, leave the remainder as residual cash. Do not create, report, or optimize toward a cash target value.
- Preserve existing account/order execution checks such as orderable cash, active orders, same-day context, order validity, and market open checks.
