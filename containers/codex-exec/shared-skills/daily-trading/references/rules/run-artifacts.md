# Run Artifact Rules

## Directory And Files

Every daily-trading run uses the injected or generated `run_id`.

```text
reports/
├── YYYY-MM-DD_포트폴리오.md
└── runs/
    └── <run_id>/
        ├── run.json
        ├── market.json
        ├── financial.json
        ├── news.json
        ├── account-before-verdict.json
        ├── merged.json
        ├── verdict-first.json
        ├── verdict-second.json
        ├── account-before-order.json
        └── execution.json
```

Create `run.json` immediately when daily-trading begins. Write every other file when its stage completes or fails. Domain snapshots are write-once; retries and partial results are retained in an `attempts` array rather than replacing earlier evidence.

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

## Common Envelope

Every JSON file contains:

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

## File Responsibilities

- `run.json`: input scope, environment, timestamps, stage statuses, final status, and artifact paths.
- `market.json`, `financial.json`, `news.json`: one domain file each containing the complete symbol universe and per-symbol errors.
- `account-before-verdict.json`: sanitized initial account, current holdings, pending/reserved orders, and same-day fills.
- `merged.json`: immutable verdict input, source provenance, eligibility, and exclusion reasons.
- `verdict-first.json`: raw first-verdict responses and aggregated `+2..-2` score per eligible symbol.
- `verdict-second.json`: second-verdict set, raw judge responses, reconciled target quantities, target cash, and rationale.
- `account-before-order.json`: sanitized latest account snapshot or a skipped envelope.
- `execution.json`: quantity calculations, final order list, submissions, failures, and post-order state, or a skipped envelope.

## Sanitization

Before writing any artifact or sending data to a sub-agent, remove:

- account number and account product code
- access token, refresh token, app key, and app secret
- HTS ID
- authorization headers and cookies
- any field whose value is a credential

Record that sanitization occurred without recording the removed value.
