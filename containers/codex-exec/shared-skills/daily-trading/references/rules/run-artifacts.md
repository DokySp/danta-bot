# Run Artifact Rules

## Directory And Files

Every daily-trading run uses the injected or generated `run_id`.

```text
reports/
├── YYYY-MM-DD_포트폴리오.md
└── runs/
    └── <run_id>/
        ├── subagents/
        │   ├── <task_name>.wrapper.json
        │   └── <task_name>.raw.txt
        ├── verdicts/
        │   └── <stage>--<agent_role>--<task_name>.md
        ├── run.json
        ├── price-chart.json
        ├── account-before-verdict.json
        ├── decision-brief.json
        ├── verdict-first.json
        ├── verdict-second.json
        ├── account-before-order.json
        └── execution.json
```

Create `run.json` immediately when daily-trading begins. Write every other file when its stage completes or fails. Domain snapshots are write-once; retries and partial results are retained in an `attempts` array rather than replacing earlier evidence.

The daily-trading sub-agent launcher writes only `subagents/<task_name>.wrapper.json` and `subagents/<task_name>.raw.txt`. It does not write canonical artifacts such as verdict JSON files. Financial and news collection return `parsed_text`, but that text is a cache path or the fixed missing-cache message rather than body text. Market-status collection also returns `parsed_text`, but that text is the concise `$get-market-status` Markdown summary rather than a file path. The Main agent calls `kis-trade-mcp` directly for price/chart data and writes `price-chart.json`. Financial context is stored as a reusable YAML memory cache at `memory/collect-financial-information/financial-YYYY-MM-DD.yaml`; news context is stored as a reusable YAML memory cache at `memory/collect-news-information/news-YYYY-MM-DD.yaml`. The Main agent must not create `reports/runs/<run_id>/financial.md`, `reports/runs/<run_id>/news.md`, or `reports/runs/<run_id>/market-status.md`.

Verdict sub-agents may write only their own human-review companion Markdown file under `verdicts/` using the fixed filename rules in `verdict-format.md`. These Markdown files are for human inspection only. Missing, malformed, or incomplete companion Markdown must be recorded as a non-blocking warning, but must not fail wrapper parsing, JSON artifact validation, score aggregation, target reconciliation, target calculation, or order gates.

## Status Values

Only these values are allowed:

- `success`: the stage produced all required usable output.
- `partial`: the stage produced usable output but has one or more symbol or non-fatal stage errors.
- `failed`: the stage produced no usable output or a required main-agent gate failed.

For a deliberately unneeded stage, use `status="success"`, `skipped=true`, and a non-empty `skip_reason`.

Final `run.json` status:

- `success`: every required analysis stage succeeded and every explicitly requested execution stage completed without failure.
- `partial`: a usable report exists, but at least one symbol or non-fatal stage is partial, excluded, blocked, or failed.
- `failed`: no usable verdict/report exists, or a required explicitly requested account/order stage failed so the requested result could not be produced.

## Common Envelope

Every artifact JSON file contains:

```json
{
  "schema_version": "1",
  "run_id": "",
  "started_at": "",
  "generated_at": "",
  "stage": "",
  "status": "success | partial | failed",
  "skipped": false,
  "skip_reason": "",
  "errors": [],
  "symbols": []
}
```

Each error contains:

```json
{
  "stage": "",
  "symbol_id": "",
  "source": "",
  "code": "",
  "message": "",
  "required": true
}
```

Omit `symbol_id` only for run-wide errors. Do not include sensitive values in error messages.

Launcher wrappers must be treated as failed stage evidence when the wrapper is missing, has `status="failed"`, or has a non-zero `returncode`. Financial, news, and market-status text stages fail in the launcher only when `parsed_text` is empty; the launcher must not add a separate file-existence validation for returned cache paths or market-status text. A `run-group` result is `failed` only when a required stage fails; if only financial/news/market-status stages fail, the group result is `partial`. Failed wrappers must remain in `subagents/`; the Main agent must write a failed canonical envelope for a failed required stage and block order candidate calculation or order submission only when the failed stage is required.

## File Responsibilities

- `run.json`: input scope, environment, timestamps, stage statuses, final status, and artifact paths.
- `price-chart.json`: required sanitized canonical price/chart file from direct `kis-trade-mcp` calls, containing the complete symbol universe and per-symbol errors.
- Financial memory path: optional best-effort cache reference to `memory/collect-financial-information/financial-YYYY-MM-DD.yaml`. Missing, failed, partial, no-data, malformed YAML, or absent financial cache must not fail validation or block verdict/order flow by itself.
- News memory path: optional best-effort cache reference to `memory/collect-news-information/news-YYYY-MM-DD.yaml`. Missing, failed, partial, no-data, malformed YAML, or absent news cache must not fail validation or block verdict/order flow by itself.
- Market-status launcher text: optional best-effort compact summary returned by `$get-market-status`. Missing, failed, partial, no-data, malformed, or absent market-status text must not fail validation or block verdict/order flow by itself.
- `account-before-verdict.json`: sanitized initial account, current holdings, pending/reserved orders, and same-day fills.
- `decision-brief.json`: compact canonical verdict input derived from price/chart, account evidence, and optional financial/news/market-status summaries; contains source provenance, eligibility, exclusion reasons, account exposure, and domain summaries; excludes raw payloads, full article text, repeated source detail, and sensitive fields.
- `verdict-first.json`: raw `first-verdict` responses, companion Markdown paths when present, and aggregated integer `0` to `10` score per eligible symbol.
- `verdict-second.json`: `second-verdict` set, raw judge responses, companion Markdown paths when present, reconciled target quantities, target cash, and rationale.
- `verdicts/<stage>--<agent_role>--<task_name>.md`: optional human-review companion Markdown written by the corresponding verdict sub-agent only. It is not canonical machine input.
- `account-before-order.json`: sanitized latest account snapshot or a skipped envelope from the `order-execution` stage.
- `execution.json`: quantity calculations, final order list, submissions, failures, blocked candidates, or a skipped envelope from the `order-execution` stage.

## `decision-brief.json` Shape

`decision-brief.json` uses the common envelope and includes:

```json
{
  "brief_type": "verdict-input",
  "source_artifacts": ["price-chart.json", "account-before-verdict.json"],
  "market_status_summary": {},
  "account_exposure_summary": {},
  "symbols": [
    {
      "symbol_id": "",
      "symbol_name": "",
      "product_type": "stock | etf | etn | other | unresolved",
      "eligible_for_verdict": true,
      "evidence_mode": "full | price_only",
      "exclusion_reasons": [],
      "price": {
        "current_or_last": null,
        "observed_at": "",
        "snapshot_mode": "live | previous_trading_day"
      },
      "price_chart_signals": [],
      "financial_summary": {},
      "news_summary": [],
      "account_exposure": {},
      "required_missing": [],
      "warnings": [],
      "errors": []
    }
  ]
}
```

Financial, news, or market-status absence alone does not make a symbol ineligible. If `symbol_id`, `symbol_name`, `price.current_or_last`, and `price.observed_at` exist, keep `eligible_for_verdict=true`. Missing, failed, partial, no-data, or absent financial/news/market-status artifacts must not fail validation, lower eligibility, or block order execution by themselves.

Use financial/news/market-status summaries when present. When absent, leave those summaries empty or add non-blocking notes; do not create required evidence gates from financial/news/market-status absence.

## Sanitization

Before writing any artifact or sending data to a sub-agent, remove:

- account number and account product code
- access token, refresh token, app key, and app secret
- HTS ID
- authorization headers and cookies
- any field whose value is a credential

Record that sanitization occurred without recording the removed value.
