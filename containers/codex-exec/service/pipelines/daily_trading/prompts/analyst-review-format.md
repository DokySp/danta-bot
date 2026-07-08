# Review Format

## Shared Rules

Review agents use only supplied immutable artifacts, persona text, and this format. They may use read-only local shell commands such as `cat` and `jq` only for explicitly listed artifact/persona/rule files. They must not call KIS, MCP, web, network, account/order APIs, or external data sources; read unrelated files; recollect; write files; or write canonical artifacts.

Review agents return compact JSON only. They must not emit Markdown, diffs, code fences, long prose, raw artifact excerpts, or raw source payloads. `human_markdown_path` is informational only; the Main agent creates one human-review Markdown sidecar from parsed JSON:

```text
reports/runs/<run_id>/reviews/<stage>--<agent_role>--<task_name>.md
```

`<stage>` is `analyst-review` or `judge-review`. Sanitize `agent_role` and `task_name` by replacing every character except ASCII letters, digits, `_`, `-`, and `.` with `-`. Do not add timestamps, symbol names, persona names, spaces, slashes, or suffixes.

Main-generated `analyst-review` sidecar content:

- Korean prose.
- Exactly one per-symbol Markdown table.
- Header exactly:

  ```markdown
  | 관점 | 종목 | 점수 | confidence(확신도) | 의견(판단) |
  |---|---|---:|---:|---|
  ```

- One row for every supplied eligible asset.
- Combined execution output has one row per view per supplied eligible asset.
- `종목` includes symbol id and name.
- `관점` is the canonical view role such as `analyst-quality-value`.
- `점수` and `confidence(확신도)` are `0` to `10`.
- `의견(판단)` is concise and cites only supplied evidence.
- No extra per-symbol sections or sensitive values.

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

`decision-brief.json` is the canonical review input. It should contain compact price/chart, optional top-level market_index_snapshot, optional financial/news summaries, account exposure, eligibility, evidence mode, and errors. Absence of optional market_index_snapshot or financial/news data is context only; it must not lower score, lower confidence, exclude a symbol, remove a final holding quantity, or block orders by itself.

Review sub-agents receive launcher-created `review-inputs/` slices containing only the listed `symbol_ids`. `analyst-review` reads a role-scoped `review-core` slice derived from `decision-brief.json` and filtered to the execution agent's output view input profiles. `judge-review` reads `review-core` plus a selected-symbol slice derived from `analyst-review.json`. Raw prompt fallback is forbidden for review stages. Review sub-agents may use read-only local shell commands such as `cat` and `jq` only for explicitly listed artifact/persona/rule files. Do not load unrelated symbols, raw memory caches, optional source files, secrets, or unlisted paths.

## `analyst-review`

Selected analyst-review execution personas produce four canonical independent scores for every eligible symbol. `analyst-quality-risk` runs once and must return two independent views: `analyst-quality-value` and `analyst-risk-allocation`. `analyst-momentum-news` runs once and must return two independent views: `analyst-momentum-cycle` and `analyst-news-flow`.

- `analyst-quality-value` covers financial stability, earnings growth, valuation, and quality/value factors.
- `analyst-momentum-cycle` covers price trend, supply/demand, sector cycle, macro sensitivity, theme/event momentum, and earnings momentum.
- `analyst-risk-allocation` covers volatility, liquidity, stop-loss room, duplicate ETF/index exposure, concentration, and portfolio fit.
- `analyst-news-flow` covers supplied KIS news/disclosure direction, materiality, freshness, and mixed-news risk. If no usable news/disclosure summary is supplied, it must return `score=5`, `confidence=5`, and `reason_code="no_news_excluded"` for audit; Main helper excludes that row from analyst-review aggregation.

When `agent_role` is `analyst-quality-risk` or `analyst-momentum-news`, return each symbol with a `views` object instead of top-level `score` and `confidence`:

```json
{
  "symbol_id": "005930",
  "symbol_name": "삼성전자",
  "views": {
    "analyst-quality-value": {
      "score": 6,
      "confidence": 7,
      "reason_code": "hold_neutral",
      "one_line_reason": "quality/value-only reason",
      "missing_data": []
    },
    "analyst-risk-allocation": {
      "score": 5,
      "confidence": 6,
      "reason_code": "risk_neutral",
      "one_line_reason": "risk/allocation-only reason",
      "missing_data": []
    }
  }
}
```

For `analyst-momentum-news`, use the same shape with `views.analyst-momentum-cycle` and `views.analyst-news-flow`.

The two views in each combined execution agent must be evaluated independently. Do not copy one view's score, confidence, reason_code, or one_line_reason into the other view.

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
  "stage": "analyst-review",
  "human_markdown_path": "reports/runs/<run_id>/reviews/analyst-review--<agent_role>--<task_name>.md",
  "symbols": [
    {
      "symbol_id": "",
      "symbol_name": "",
      "views": {
        "analyst-momentum-cycle": {
          "score": 5,
          "confidence": 5,
          "reason_code": "hold_neutral",
          "one_line_reason": "",
          "missing_data": []
        },
        "analyst-news-flow": {
          "score": 5,
          "confidence": 5,
          "reason_code": "no_news_excluded",
          "one_line_reason": "뉴스 정보가 없어 평균에서 제외",
          "missing_data": ["news_summary"]
        }
      }
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
- Agents cannot see other review outputs.
- `human_markdown_path` is informational.

Aggregation by Main agent:

```text
effective_confidence = clamp(((confidence - 4) / 4) * 10, 0, 10)
confidence_weight = effective_confidence / 10
confidence_adjusted_score = 5 + ((score - 5) * confidence_weight)
mean_score = sum(included valid scores) / count(included valid scores)
mean_confidence_adjusted_score = sum(included valid confidence_adjusted_scores) / count(included valid confidence_adjusted_scores)
final_first_score = mean_confidence_adjusted_score
```

`confidence_adjusted_score` pulls low-confidence scores toward neutral `5`; raw `confidence<=4` becomes neutral weight `0`, raw `confidence=5` becomes effective confidence `2.5`, raw `confidence=6` becomes `5`, raw `confidence=7` becomes `7.5`, and raw `confidence>=8` preserves the original `score`.
In the normal successful path, the aggregation uses four canonical views: `analyst-quality-value`, `analyst-risk-allocation`, `analyst-momentum-cycle`, and `analyst-news-flow`. If `analyst-news-flow` has no usable news/disclosure summary, preserve its row with `excluded_from_aggregation=true` and aggregate the remaining included views only. If any other view score is missing or unusable, keep the valid-score aggregation rule above and surface the missing score as an artifact error.
If no valid score exists, exclude that symbol from `judge-review` and trading.

## `judge-review`

Input set = eligible symbols with decimal `final_first_score >= 6` plus every eligible `holding` symbol from `$check-portfolio`. Only `judge` compares that set at portfolio level. If its required output is missing or unusable, retry only the failed `judge` task at most two times.

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
- A reduce rationale must set `target_position_value_krw` below that baseline; an increase rationale must set it above; a hold rationale must keep it at the baseline level.
- "No additional buy", "no extra exposure", or "추가 확대 없음" is a hold rationale: do not raise `target_position_value_krw` above the baseline, and do not set it to `0`.
- If `today_trade_timeline_context` shows a same-day buy fill and `target_position_value_krw` is above the baseline, include `additional_buy_reason` with the new evidence or materially changed price/portfolio context supporting the increase.
- `reason_code` and `one_line_reason` must describe the same reduce/hold/increase direction implied by `target_position_value_krw` versus the baseline.
- Every judge-review symbol receives a target position value, including valid `0` values when the rationale explicitly says reduce/exit/sell.
- Consider relative attractiveness, duplicate exposure, current weight, price/chart conditions, and the supplied selected-symbol analyst-review results.
- Treat `final_first_score` as the unrounded confidence-adjusted analyst-review score: `>= 6` is a buy/increase candidate, `<= 4` is a reduce/exit candidate, and `5` is neutral.
- When referring to per-analyst scores in `agent_scores`, use `confidence_adjusted_score` as the score. `score` and `confidence` are supporting inputs explaining that adjusted score.
- If a symbol's analyst-review score is missing, unavailable, or unusable, treat its score as neutral `5` instead of failing the judgment.
- `analyst-review` scores are judgment inputs, not hard buy/sell gates.
- For holding symbols, distinguish `long_term_thesis_intact` from `add_allowed`: intact thesis suppresses unnecessary sell/reduce decisions, but it is not by itself permission to increase final holding quantity.
- Judge long-term thesis from supplied evidence only: core investment rationale, material news/disclosure risk, quality/value deterioration, whether a price shock indicates structural damage or short-term volatility, and portfolio weight/concentration.
- Increase target position value only when add conditions are also satisfied: quality/value advantage, acceptable risk/allocation, weight/concentration room, explicit reassessment of any same-day/recent trade context, and no supplied material adverse news/disclosure.
- Do not take profit solely because a position is up or the current day is sharply positive when thesis remains intact. If overextension, overweight, and a clearly better alternative are all present, prefer partial reduction over full exit.
- Do not use same-day fills or `recent_trade_context` as a default hold/block reason. Same-direction additional trades and opposite-direction final holding changes are allowed only after explicitly reassessing price movement, `final_first_score`, risk, order/fill state, and thesis evidence; encode that reassessment in `reason_code` and `one_line_reason`.
- No fixed cash ratio or fixed investment ratio.
- The judge cannot add symbols outside the supplied set.
- Do not return long `cash_rationale`, `duplicate_exposure_limits`, `price_chart_view`, `rationale`, `risks`, or prose arrays.

Validation by Main agent:

- Use the single valid `judge` target position values as the canonical `judge-review.json` `target_position_value_krw` values.
- Main/pipeline derives canonical `final_holding_quantity` values from `target_position_value_krw / price.current_or_last` using Decimal `ROUND_HALF_UP`.
- If the valid judge result is missing a target position value for a symbol, set no final holding quantity and exclude it from orders.
- Do not reduce buy-side quantities solely because rounded target shares exceed `target_position_value_krw`; affordability and order availability are handled by existing account/order gates.
- If derived final holdings are below assets, leave the remainder as residual cash. Do not create, report, or optimize toward a cash target value.
- Preserve existing account/order execution checks such as orderable cash, active orders, same-day context, order validity, and market open checks.
- Apply latest account constraints after final holding derivation.

## Allowed Values

| Field | Values |
|---|---|
| artifact status | `success`, `partial`, `failed` |
| score/confidence | integer `0` to `10` |
| eligibility | `eligible_for_review=true/false` |
| order direction | `buy`, `sell`, `none` |
| execution result | `submitted`, `skipped`, `blocked`, `failed` |
