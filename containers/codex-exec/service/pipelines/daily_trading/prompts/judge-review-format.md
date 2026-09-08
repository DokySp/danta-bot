# Judge Review Format

## Input Contract

`judge-review` reads the supplied `review-core` (including compact `investment_context`, `prior_decision_context` and `analyst_history_context`), the selected-symbol slice from the current `analyst-review.json`, the Judge persona, and this format. `agent_scores` excluded from aggregation are intentionally omitted from the selected slice. Raw prompt fallback and unrelated symbols or files are outside the input contract.

## Review Scope (no direction precondition)

The input set is selected deterministically: every eligible held symbol, every eligible symbol with an active order, same-day `unresolved_buy_intent`, or directly linked `symbol_news_summary`, and up to `unheld_review_top_k` remaining unheld symbols with valid scores ranked by continuous `final_first_score` then symbol id. `review_scope_reasons` records `held_position`, `active_order`, `unresolved_buy_intent`, `symbol_news`, or `unheld_score_rank` for audit only. There is no score band or assigned buy/sell direction.

The `review-core` supplies each selected symbol's `holding_quantity_context`, top-level `account_exposure_summary.orderable_cash_amount`, and advisory `account_performance_context`. Holdings are the baseline for target-value comparison; orderable cash is only the aggregate incremental-buy budget. Performance goals and risk references inform sizing but never authorize, block, or mechanically force a trade.

`final_first_score` is the simple mean of the included Analyst view scores; the included per-view scores carry its evidence.

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
      "plan_review": {
        "business_quality": "기존 실적과 경쟁력 근거는 유지된다.",
        "entry_price": "현재 가격에서 추가 확대할 편익은 확인되지 않는다.",
        "change_type": "no_material_change",
        "comparison": "진입 당시 실적 근거와 현재 관측 사이에 중요한 변화가 없어 수량을 유지한다.",
        "evidence_refs": ["analyst-review:005930:analyst-quality-value"]
      },
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

## Field Contract

- Return `stage="judge-review"` and exactly one `symbols[]` entry for every supplied symbol; do not add symbols outside the supplied scope.
- `human_markdown_path` is the supplied informational path. Main creates the sidecar from parsed JSON; the Judge does not return Markdown.
- `target_position_value_krw` is required, numeric, and non-negative. `0` is valid for an explicit exit.
- `final_holding_quantity` is optional and not a sizing decision. Main derives it from `target_position_value_krw / price.current_or_last` with Decimal `ROUND_HALF_UP`.
- The baseline position value is `holding_quantity_context.expected_holding_quantity * price.current_or_last`.
- `relative_attractiveness_rank` is the symbol's integer rank within the supplied review scope.
- `decision_basis` is optional audit metadata: `none` when target equals baseline, `thesis`, or `profit_protection`. It does not authorize or block the target.
- `reason_code` and `one_line_reason` must match the reduce/hold/increase direction implied by the target versus baseline.
- `opposing_view` is required. `increase_case` and `reduce_case` each contain a short `summary` and its own supplied usable-evidence `evidence_refs`. Do not add a second action, resolution, confidence, transcript, or hidden reasoning field.
- `additional_buy_reason` is optional audit text for an increase after a same-day buy, not an authorization field.
- Return a compact `plan_review` for every symbol. `business_quality` and `entry_price` are separate short assessments (up to 200 characters each); `comparison` is a short prior/current/decision explanation (up to 300 characters); `evidence_refs` contains up to 4 supplied source references. Use explicit uncertainty rather than invented values or a reconstructed old plan.
- `plan_review.change_type` is one of `new_information`, `reassessment`, `no_material_change`, `unavailable`. These are the Judge's claims about the comparison, not independently verified facts. `reassessment` discloses a changed interpretation or risk judgment without pretending a new event occurred; `unavailable` includes a missing comparison baseline.
- Missing or malformed `plan_review` is recorded as incomplete audit metadata, not grounds to reject a valid target or prevent an exit. `plan_review_audit` is pipeline-owned; do not return it. An audit flag never authorizes or blocks an order.
- Do not return long `cash_rationale`, `duplicate_exposure_limits`, `price_chart_view`, `rationale`, `risks`, prose arrays, or raw source payloads.

### Position-thesis fields (audit context)

- Each symbol's `prior_decision_context` contains `status` (`available`/`no_prior_decision`) and, when available, `previous_session`, `latest_decision`, and `current_session_target_path`. Its `thesis_definition`, when present, is the confirmed active investment's entry thesis, not the newest unfilled or hold-only advice. An active `unresolved_buy_intent` is a same-day cash-gated buy target with no confirmed submission or fill; use its target as the default baseline unless current material investment evidence invalidates it, and name that changed evidence in `one_line_reason` when reducing or withdrawing it. `previous_session.realized_pnl.scope=symbol_session` identifies a broker fact.
- `investment_context` is launcher-owned fill/account provenance. `active_investment.entry` is immutable entry evidence; `changes` contains up to 12 latest confirmed changes with original decision reasons and `change_count` discloses the full count. `last_closed_investment` is separate from a new entry. Missing entry/conditions/evaluation point or incomplete history is unknown, never permission to invent an original plan. `reported_at` can be the broker's order timestamp; `confirmed_at` is the observation time, not guaranteed exact fill time. `simulated_fills` is virtual evidence, never a broker claim. Do not return or rewrite `investment_context`; it never authorizes or blocks a target.
- `thesis_definition` is valid only when `core_rationale` is non-empty and at least one `invalidation_conditions[]` entry has both a non-empty `condition_id` and a non-empty `description`. An empty, missing, or otherwise malformed `thesis_definition` is never treated as valid.
- `thesis_definition` is optional; only a valid structure is persisted. `thesis_assessment` is optional with `status` (`intact`/`damaged`/`uncertain`) and `matched_invalidation_condition_ids`.
- Optional `thesis_definition.evaluation_point` records an explicitly chosen evaluation date or event (short text). Omit it when unspecified; do not infer one for an old holding. It is audit metadata, not a deadline or a holding/selling rule.
