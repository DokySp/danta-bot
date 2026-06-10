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
        ├── market.json
        ├── financial.md    # optional best-effort plain text
        ├── news.md         # optional best-effort plain text
        ├── account-before-verdict.json
        ├── decision-brief.json
        ├── verdict-first.json
        ├── verdict-second.json
        ├── account-before-order.json
        └── execution.json
```

Create `run.json` immediately when daily-trading begins. Write every other file when its stage completes or fails. Domain snapshots are write-once; retries and partial results are retained in an `attempts` array rather than replacing earlier evidence.

The daily-trading sub-agent launcher writes only `subagents/<task_name>.wrapper.json` and `subagents/<task_name>.raw.txt`. It does not write canonical artifacts such as `market.json`, `financial.md`, `news.md`, or verdict JSON files. The Main agent reads each wrapper, sanitizes `parsed_json` for JSON stages or `parsed_text` for financial/news text stages, and writes the canonical artifact.

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

Launcher wrappers must be treated as failed stage evidence when the wrapper is missing, has `status="failed"`, or has a non-zero `returncode`. JSON-required stages also fail when `parsed_json=null`. Financial/news text stages fail only when `parsed_text` is empty or unusable. A `run-group` result is `failed` only when a required stage fails; if only financial/news text stages fail, the group result is `partial`. Failed wrappers must remain in `subagents/`; the Main agent must write a failed canonical envelope for a failed required stage and block order candidate calculation or order submission only when the failed stage is required.

## File Responsibilities

- `run.json`: input scope, environment, timestamps, stage statuses, final status, and artifact paths.
- `market.json`: required market file containing the complete symbol universe and per-symbol errors.
- `financial.md`, `news.md`: optional best-effort plain-text context files. Missing, failed, partial, no-data, malformed Markdown, or absent financial/news text must not fail validation or block verdict/order flow by themselves.
- `account-before-verdict.json`: sanitized initial account, current holdings, pending/reserved orders, and same-day fills.
- `decision-brief.json`: compact canonical verdict input derived from market/account evidence and optional financial/news summaries; contains source provenance, eligibility, exclusion reasons, account exposure, and domain summaries; excludes raw payloads, full article text, repeated source detail, and sensitive fields.
- `verdict-first.json`: raw `first-verdict` responses, companion Markdown paths when present, and aggregated `+2..-2` score per eligible symbol.
- `verdict-second.json`: `second-verdict` set, raw judge responses, companion Markdown paths when present, reconciled target quantities, target cash, and rationale.
- `verdicts/<stage>--<agent_role>--<task_name>.md`: optional human-review companion Markdown written by the corresponding verdict sub-agent only. It is not canonical machine input.
- `account-before-order.json`: sanitized latest account snapshot or a skipped envelope from the `order-execution` stage.
- `execution.json`: quantity calculations, final order list, submissions, failures, blocked candidates, or a skipped envelope from the `order-execution` stage.

## `decision-brief.json` Shape

`decision-brief.json` uses the common envelope and includes:

```json
{
  "brief_type": "verdict-input",
      "source_artifacts": ["market.json", "account-before-verdict.json"],
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

Financial or news absence alone does not make a symbol ineligible. If `symbol_id`, `symbol_name`, `price.current_or_last`, and `price.observed_at` exist, keep `eligible_for_verdict=true`. Missing, failed, partial, no-data, or absent financial/news artifacts must not fail validation, lower eligibility, or block order execution by themselves.

Use financial/news summaries when present. When absent, leave those summaries empty or add non-blocking notes; do not create required evidence gates from financial/news absence.

## Sanitization

Before writing any artifact or sending data to a sub-agent, remove:

- account number and account product code
- access token, refresh token, app key, and app secret
- HTS ID
- authorization headers and cookies
- any field whose value is a credential

Record that sanitization occurred without recording the removed value.
