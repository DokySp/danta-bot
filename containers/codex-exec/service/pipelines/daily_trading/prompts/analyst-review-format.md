# Analyst Review Format

## Shared Rules

Review agents use only supplied immutable artifacts, persona text, and this format. They may use read-only local shell commands such as `cat` and `jq` only for explicitly listed artifact/persona/rule files. They must not call KIS, MCP, web, network, account/order APIs, or external data sources; read unrelated files; recollect; write files; or write canonical artifacts.

Review agents return compact JSON only. They must not emit Markdown, diffs, code fences, long prose, raw artifact excerpts, or raw source payloads. `human_markdown_path` is informational only; the Main agent creates one human-review Markdown sidecar from parsed JSON:

```text
reports/runs/<run_id>/reviews/<stage>--<agent_role>--<task_name>.md
```

`<stage>` is `analyst-review`. Sanitize `agent_role` and `task_name` by replacing every character except ASCII letters, digits, `_`, `-`, and `.` with `-`. Do not add timestamps, symbol names, persona names, spaces, slashes, or suffixes.

Main-generated `analyst-review` sidecar content:

- Korean prose.
- Exactly one per-symbol Markdown table.
- Header exactly:

  ```markdown
  | 관점 | 종목 | 점수 | 의견(판단) |
  |---|---|---:|---|
  ```

- One row for every supplied eligible asset.
- Combined execution output has one row per view per supplied eligible asset.
- `종목` includes symbol id and name.
- `관점` is the canonical view role such as `analyst-quality-value`.
- `점수` is `0` to `10`.
- `의견(판단)` is concise and cites only supplied evidence.
- No extra per-symbol sections or sensitive values.

The sidecar is never machine input. JSON captured by the launcher is authoritative. Missing, malformed, or inconsistent sidecars are warnings only.

`decision-brief.json` is the canonical review input. It should contain compact price/chart, optional top-level market_index_snapshot, optional financial/news summaries, account exposure, eligibility, evidence mode, and errors. Absence of optional market_index_snapshot or financial/news data is context only; it must not lower score, exclude a symbol, or block orders by itself.

Review sub-agents receive launcher-created `review-inputs/` slices containing only the listed `symbol_ids`. `analyst-review` reads a role-scoped `review-core` slice derived from `decision-brief.json` and filtered to the execution agent's output view input profiles. Raw prompt fallback is forbidden for review stages. Review sub-agents may use read-only local shell commands such as `cat` and `jq` only for explicitly listed artifact/persona/rule files. Do not load unrelated symbols, raw memory caches, optional source files, secrets, or unlisted paths.

## `analyst-review`

Selected analyst-review execution personas produce four canonical independent scores for every eligible symbol. `analyst-quality-risk` runs once and must return two independent views: `analyst-quality-value` and `analyst-risk-allocation`. `analyst-momentum-news` runs once and must return two independent views: `analyst-momentum-cycle` and `analyst-news-flow`.

- `analyst-quality-value` covers financial stability, earnings growth, valuation, and quality/value factors.
- `analyst-momentum-cycle` covers price trend, supply/demand, sector cycle, macro sensitivity, theme/event momentum, and earnings momentum.
- `analyst-risk-allocation` covers volatility, liquidity, stop-loss room, duplicate ETF/index exposure, concentration, and portfolio fit.
- `analyst-news-flow` covers supplied KIS news/disclosure direction, materiality, freshness, and mixed-news risk. If no usable news/disclosure summary is supplied, it must return `score=5` and `reason_code="no_news_excluded"` for audit; Main helper excludes that row from analyst-review aggregation.

When `agent_role` is `analyst-quality-risk` or `analyst-momentum-news`, return each symbol with a `views` object instead of a top-level `score`:

```json
{
  "symbol_id": "005930",
  "symbol_name": "삼성전자",
  "views": {
    "analyst-quality-value": {
      "score": 6,
      "reason_code": "hold_neutral",
      "one_line_reason": "quality/value-only reason",
      "missing_data": []
    },
    "analyst-risk-allocation": {
      "score": 5,
      "reason_code": "risk_neutral",
      "one_line_reason": "risk/allocation-only reason",
      "missing_data": []
    }
  }
}
```

For `analyst-momentum-news`, use the same shape with `views.analyst-momentum-cycle` and `views.analyst-news-flow`.

The two views in each combined execution agent must be evaluated independently. Do not copy one view's score, reason_code, or one_line_reason into the other view.

Score scale:

| Score | Meaning |
|---:|---|
| `9-10` | strong buy candidate |
| `7-8` | buy candidate |
| `5-6` | hold / neutral |
| `3-4` | reduce / sell candidate |
| `0-2` | strong sell candidate |

The score itself must carry the strength of the evidence: when evidence is thin, stale, mixed, or conflicting, keep the score close to neutral `5`. Do not express uncertainty anywhere else; there is no separate confidence field.

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
          "reason_code": "hold_neutral",
          "one_line_reason": "",
          "missing_data": []
        },
        "analyst-news-flow": {
          "score": 5,
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

- `score` is an integer from `0` to `10`.
- `reason_code` is a short snake_case label. `one_line_reason` is one concise Korean sentence citing only supplied evidence when useful.
- Do not return long `evidence`, `risks`, `rationale`, or prose arrays.
- One symbol's data cannot support another symbol.
- Agents cannot see other review outputs.
- `human_markdown_path` is informational.

Aggregation by Main agent:

```text
mean_score = sum(included valid scores) / count(included valid scores)
final_first_score = mean_score
```

In the normal successful path, the aggregation uses four canonical views: `analyst-quality-value`, `analyst-risk-allocation`, `analyst-momentum-cycle`, and `analyst-news-flow`. If `analyst-news-flow` has no usable news/disclosure summary, preserve its row with `excluded_from_aggregation=true` and aggregate the remaining included views only. If any other view score is missing or unusable, keep the valid-score aggregation rule above and surface the missing score as an artifact error.
If no valid score exists, exclude that symbol from `judge-review` and trading.
