# Judge Review Format

## Shared Rules

Review agents use only supplied immutable artifacts, persona text, and this format. They may use read-only local shell commands such as `cat` and `jq` only for explicitly listed artifact/persona/rule files. They must not call KIS, MCP, web, network, account/order APIs, or external data sources; read unrelated files; recollect; write files; or write canonical artifacts.

Opposing-view boundary: judge-review does its own bounded internal comparison of the strongest exposure-increase/maintain case and the strongest exposure-reduce/avoid case per symbol (see `opposing_view` below). The judge must not spawn or resume a separate debate agent, request another round, or include long transcripts in the returned JSON.

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

`judge-review` reads `review-core` (including compact prior-decision and Analyst-history context) plus a selected-symbol slice derived from the current `analyst-review.json`. Raw prompt fallback is forbidden. Do not load unrelated symbols, raw memory caches, optional source files, secrets, or unlisted paths.

## Review Scope (no direction precondition)

The judge input set is selected deterministically by the pipeline: every eligible held symbol is always included (regardless of score, or when a symbol has no usable score at all), every eligible symbol with an active order or directly linked `symbol_news_summary` is included, and up to `unheld_review_top_k` remaining unheld symbols with valid scores are selected by descending continuous `final_first_score` then symbol id. The spec's `review_scope_reasons` records why each symbol is in scope (`held_position`, `active_order`, `symbol_news`, or `unheld_score_rank`), for audit only. There is no score band and no assigned buy/sell candidate direction: the judge proposes one `target_position_value_krw` per symbol, and Main/pipeline mechanically derives the resulting action from that target versus the baseline. Both increase and decrease proposals are evaluated symmetrically for every supplied symbol.

The `review-core` supplies each selected symbol's `holding_quantity_context` and top-level `account_exposure_summary.orderable_cash_amount`. Holdings are the baseline for target-value comparison; orderable cash is only the aggregate incremental-buy budget.

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
      "decision_basis": "none",
      "reason_code": "hold_final_quantity",
      "one_line_reason": "기준금액 수준의 목표금액을 유지한다.",
      "opposing_view": {
        "increase_case": {
          "summary": "quality moat and pricing power support maintaining exposure",
          "evidence_refs": ["analyst-review:005930:analyst-quality-value"]
        },
        "reduce_case": {
          "summary": "margin compression risk flagged by momentum/news view",
          "evidence_refs": ["analyst-review:005930:analyst-news-flow"]
        }
      },
      "thesis_definition": {
        "core_rationale": "quality moat and pricing power",
        "invalidation_conditions": [
          {"condition_id": "margin-compression", "description": "gross margin drops below prior guidance"}
        ]
      },
      "thesis_assessment": {
        "status": "damaged",
        "matched_invalidation_condition_ids": ["margin-compression"]
      }
    }
  ],
  "errors": []
}
```

`decision_basis`, `thesis_definition`, and `thesis_assessment` are optional audit fields per symbol. `opposing_view` is required for every symbol.

Rules:

- `target_position_value_krw` is required for every judge-review symbol. It is the judge's target position value in KRW after this decision.
- `target_position_value_krw` must be numeric and non-negative. `0` is valid when the rationale explicitly says reduce-to-zero or exit.
- `final_holding_quantity` is optional in judge output and is not the judge's sizing decision. Main/pipeline derives it from `target_position_value_krw / price.current_or_last` using Decimal `ROUND_HALF_UP`.
- Use each symbol's `holding_quantity_context.expected_holding_quantity * price.current_or_last` as the explicit baseline position value.
- `decision_basis` is an optional audit label: `none` (target equals baseline), `thesis`, or `profit_protection`. It explains the target but does not authorize or block it.
- `opposing_view` is required: `increase_case` and `reduce_case`, each with a short `summary` and its own `evidence_refs` drawn only from supplied usable evidence. This is the bounded, auditable record of the internal comparison that replaced the standalone bull/bear debate; the resolved decision is already represented by `target_position_value_krw`, so do not add a second action, resolution, confidence, transcript, or hidden reasoning field.
- "No additional buy", "no extra exposure", or "추가 확대 없음" is a hold rationale: keep `target_position_value_krw` at the baseline level, use `decision_basis="none"`, and do not set it to `0`.
- When increasing after a same-day buy, `additional_buy_reason` may record the new evidence or changed price/market context. It is optional audit text, not an authorization field.
- Read `prior_decision_context` and `analyst_history_context` first; the latter contains the latest previous-session snapshot and up to three same-session stance changes, as interpretation history rather than fresh evidence, votes, averages, or gates. Treat the previous target as an ongoing plan over its thesis/rationale horizon and change it only when new evidence supports enough expected portfolio improvement after transaction and opportunity costs; urgent material risk may override it immediately. For a change or reversal, put the new evidence, horizon, and net benefit or urgency in `one_line_reason`; never judge from realized P&L or hindsight alone. This is a Judge objective, not a code gate, and unavailable outcomes must not be estimated.
- `reason_code` and `one_line_reason` must describe the same reduce/hold/increase direction implied by `target_position_value_krw` versus the baseline.
- Consider relative attractiveness, price/chart conditions, and the supplied selected-symbol analyst-review results that contain usable evidence.
- Treat top-level `market_news_context` as a full-review market signal, never as an automatic order for every candidate. Apply an item to an individual symbol only with an explicit sector, geographic revenue, supply-chain, rate, currency, commodity, or policy linkage; otherwise it is non-directional for that symbol.
- Optional evidence marked `missing`, `failed`, `empty`, `unavailable`, or `excluded_from_aggregation` is non-directional. Do not use its absence to justify hold, reduce, or increase decisions, and do not cite it as decisive evidence in `reason_code` or `one_line_reason`.
- Do not use optional-domain coverage counts or completeness to decide whether the supplied usable evidence is sufficient. Judge sufficiency from the directional strength and conflict of the usable evidence that is actually supplied.
- If a held symbol's analyst-review score is missing, unavailable, or unusable, keep it in scope and treat the score absence as non-directional. Judge from the other supplied usable evidence; the missing score alone must neither force baseline nor support an increase or reduction.
- For a held symbol with a low score, assess thesis integrity from supplied usable evidence. An intact thesis is one positive input and thesis damage is one negative input, but thesis damage is not required for reduction: relative attractiveness, profit/loss risk, opportunity cost, or a stronger alternative may independently support reducing or exiting. Lack of damage evidence alone neither establishes an intact thesis nor forces a hold.
- Judge long-term thesis from supplied evidence only: core investment rationale, material news/disclosure risk, quality/value deterioration, and whether a price shock indicates structural damage or short-term volatility.
- Propose an increase when the supplied quality/value, trend, symbol-specific risk, and explicitly linked market or sector evidence provide a net positive case. Current weight, concentration, duplicate exposure, or sector room are not add conditions; unavailable news is neutral rather than favorable or adverse.
- Conflict alone is not a hold rule. Compare materiality, freshness, source quality, and symbol-specific investment impact, then set the target change magnitude in proportion to the supported net advantage. Hold only when neither case has enough supported net advantage to justify a change, without citing optional evidence absence as the reason.
- No fixed cash ratio or fixed investment ratio.
- Use `account_exposure_summary.orderable_cash_amount` only to keep aggregate incremental-buy targets within the available budget. Do not reward residual cash or use cash as evidence for a symbol.
- Do not calculate or apply current-weight, concentration, duplicate-exposure, or sector-allocation targets or caps.
- The judge cannot add symbols outside the supplied review scope.
- Do not return long `cash_rationale`, `duplicate_exposure_limits`, `price_chart_view`, `rationale`, `risks`, or prose arrays.

### Position-thesis fields (audit context)

- Each symbol's `review-core` input carries `prior_decision_context`: `status` (`available`/`no_prior_decision`) and, when available, `previous_session`, `latest_decision`, `current_session_target_path`, and the latest valid historical `thesis_definition`. `previous_session.realized_pnl.scope=symbol_session` is a broker fact, not a score or lesson. Use the context to compare decisions; it does not mechanically authorize or block a target change.
- `thesis_definition` is valid only when `core_rationale` is non-empty and at least one `invalidation_conditions[]` entry has both a non-empty `condition_id` and a non-empty `description`. An empty, missing, or otherwise malformed `thesis_definition` is never treated as valid.
- Return `thesis_definition` when an explicit core rationale and invalidation criteria improve future review context. Main/pipeline persists only a valid structure and otherwise ignores it without changing the target.
- Return `thesis_assessment` when the prior thesis materially affects the decision. Price loss, index/regime panic, a low score, or missing optional evidence alone never justify `status: damaged`.

Validation by Main agent:

- Main/pipeline preserves the judge's raw `requested_target_position_value_krw` and normalizes `target_position_value_krw` only to a whole-share quantity at the supplied price.
- Main/pipeline derives canonical `final_holding_quantity` values from the canonical `target_position_value_krw / price.current_or_last` using Decimal `ROUND_HALF_UP`.
- Symbols outside the supplied review scope are dropped with an error and produce no order.
- If the valid judge result is missing a target position value for a symbol, set no final holding quantity and exclude it from orders.
- Do not reduce buy-side quantities solely because rounded target shares exceed `target_position_value_krw`; affordability and order availability are handled by existing account/order gates.
- If derived final holdings are below assets, leave the remainder as residual cash. Do not create, report, or optimize toward a cash target value.
- Preserve existing broker/order execution checks such as explicit submit authorization, orderable cash, holdings and sell capacity, active orders, order validity, and market open checks.
