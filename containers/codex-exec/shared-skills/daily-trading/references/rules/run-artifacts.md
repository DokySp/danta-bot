# Run Artifact Rules

## Directory And Files

Every daily-trading run uses the injected or generated `run_id`.

```text
reports/
├── cache/
│   └── financial/
│       └── <YYYY-MM-DD>.json
├── YYYY-MM-DD_포트폴리오.md
└── runs/
    └── <run_id>/
        ├── subagents/
        │   ├── <task_name>.wrapper.json
        │   └── <task_name>.raw.txt
        ├── run.json
        ├── stage-metrics.json
        ├── market.json
        ├── financial.json
        ├── news.json
        ├── account-before-verdict.json
        ├── merged.json
        ├── decision-brief.json
        ├── verdict-first.json
        ├── verdict-second.json
        ├── account-before-order.json
        ├── final-order-verdict.json
        └── execution.json
```

Create `run.json` and initialize `stage-metrics.json` immediately when daily-trading begins. Write every other file when its stage completes or fails. Domain snapshots are write-once; retries and partial results are retained in an `attempts` array rather than replacing earlier evidence.

Financial cache files live outside `reports/runs/<run_id>/` because they are reused by date. By default they live in `~/.cache/codex/collect-financial-information/<YYYY-MM-DD>.json`; `FINANCIAL_CACHE_DIR` overrides the directory. The cache key is the Korea trading date, and the validity period is one Korea trading day. Read and write financial cache files only through `collect-financial-information/scripts/financial_cache.py`; this helper validates date, schema, stage, domain, status, non-empty symbols, and requested-symbol presence before a cache hit or cache write is allowed. Additional cached symbols are allowed.

The daily-trading sub-agent launcher writes only `subagents/<task_name>.wrapper.json` and `subagents/<task_name>.raw.txt`. It does not write canonical artifacts such as `market.json`, `financial.json`, `news.json`, or verdict files. The Main agent reads each wrapper, sanitizes `parsed_json`, writes the canonical artifact, and copies the wrapper metric into `stage-metrics.json`.

## Status Values

Only these values are allowed:

- `success`: the stage produced all required usable output.
- `partial`: the stage produced usable output but has one or more symbol or non-fatal stage errors.
- `failed`: the stage produced no usable output or a required main-agent gate failed.

For a deliberately unneeded stage, use `status="success"`, `skipped=true`, and a non-empty `skip_reason`.

Final `run.json` status:

- `success`: every required analysis stage succeeded and every explicitly requested execution stage completed without failure.
- `partial`: a usable report exists, but at least one symbol or non-fatal stage is partial, excluded, blocked, or failed.
- `failed`: no usable merged verdict/report exists, or a required explicitly requested account/order stage failed so the requested result could not be produced.

If runtime artifact validation fails, set `validation_status="failed"`, keep the validation summary in `run.json`, and mark final `status="failed"` with `status_reason="validation_failed"`.

## Common Envelope

Every artifact JSON file except `stage-metrics.json` contains:

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

## Stage Metrics

`stage-metrics.json` records the operational envelope for every major stage:

```json
{
  "schema_version": "1",
  "run_id": "",
  "started_at": "",
  "generated_at": "",
  "stage": "stage-metrics",
  "status": "success | partial | failed",
  "metrics": [
    {
      "stage": "initialize | account-before-verdict | market-collection | financial-collection | news-collection | merge-and-brief | first-verdict | second-verdict | account-before-order | final-risk-verdict | execution | post-order-state | report",
      "agent_role": "main | account | market | financial | news | analyst | juror | judge | final-risk",
      "recommended_model": "",
      "recommended_effort": "low | medium | high",
      "actual_model": "",
      "actual_effort": "low | medium | high",
      "started_at": "",
      "ended_at": "",
      "duration_ms": null,
      "status": "success | partial | failed",
      "token_usage": {
        "input_tokens": null,
        "output_tokens": null,
        "total_tokens": null
      },
      "token_source": "actual | unavailable",
      "token_unavailable_reason": ""
    }
  ],
  "errors": []
}
```

If exact token usage is available, record actual integer values and `token_source="actual"`. If exact token usage is not available, keep all token fields `null`, set `token_source="unavailable"`, and record a non-sensitive reason such as `runtime did not expose per-stage token usage`.

Launcher wrappers must be treated as failed stage evidence when the wrapper is missing, has `status="failed"`, has a non-zero `returncode`, or has `parsed_json=null`. Failed wrappers must be reflected in `stage-metrics.json` with the launcher's `actual_model` and `actual_effort`; the Main agent must write a failed canonical envelope for the affected stage and block order candidate calculation or order submission when the failed stage is required.

## File Responsibilities

- `run.json`: input scope, environment, timestamps, stage statuses, final status, artifact paths, `market_holiday.status` (`open`, `closed`, or `unknown`), and validation status.
- `stage-metrics.json`: stage timing, recommended and actual model/effort, status, and token usage availability.
- `market.json`, `financial.json`, `news.json`: one domain file each containing the complete symbol universe and per-symbol errors.
- `account-before-verdict.json`: sanitized initial account, current holdings, pending/reserved orders, and same-day fills.
- `merged.json`: immutable verdict input, source provenance, eligibility, and exclusion reasons.
- `decision-brief.json`: compact verdict input derived from `merged.json`, account exposure, and domain summaries; excludes raw payloads, full article text, repeated source detail, and sensitive fields.
- `verdict-first.json`: raw `first-verdict` responses and aggregated `+2..-2` score per eligible symbol.
- `verdict-second.json`: `second-verdict` set, raw judge responses, reconciled target quantities, target cash, and rationale.
- `account-before-order.json`: sanitized latest account snapshot or a skipped envelope.
- `final-order-verdict.json`: `final-risk-verdict` approval result for order candidates; result is `approved`, `blocked`, or `needs_review`.
- `execution.json`: quantity calculations, final order list, submissions, failures, and narrow post-order state, or a skipped envelope. Post-order state should contain only the minimum read-only verification needed for submitted orders or reservations.

## `decision-brief.json` Shape

`decision-brief.json` uses the common envelope and includes:

```json
{
  "brief_type": "verdict-input",
  "source_artifacts": ["market.json", "financial.json", "news.json", "account-before-verdict.json", "merged.json"],
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
      "market_signals": [],
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

Financial or news absence alone does not make a symbol ineligible. If `symbol_id`, `symbol_name`, `price.current_or_last`, and `price.observed_at` exist, keep `eligible_for_verdict=true`, set `evidence_mode="price_only"`, and record the missing domains in `required_missing` or `warnings`.

`price_only` is a fallback for actual financial/news missing, failed, or no-data results after those domain paths ran or a valid financial cache path was checked. It is not a shortcut for `closed` market status, previous-trading-day snapshot mode, or Main-agent-only price lookup.

## `final-order-verdict.json` Shape

`final-order-verdict.json` uses the common envelope and contains:

```json
{
  "stage": "final-risk-verdict",
  "result": "approved | blocked | needs_review",
  "order_candidate_count": 0,
  "approved_order_count": 0,
  "risk_checks": [],
  "blocking_reasons": [],
  "review_reasons": []
}
```

If this file is missing, invalid, `failed`, `blocked`, or `needs_review`, the Main agent must block order submission.

## Sanitization

Before writing any artifact or sending data to a sub-agent, remove:

- account number and account product code
- access token, refresh token, app key, and app secret
- HTS ID
- authorization headers and cookies
- any field whose value is a credential

Record that sanitization occurred without recording the removed value.
