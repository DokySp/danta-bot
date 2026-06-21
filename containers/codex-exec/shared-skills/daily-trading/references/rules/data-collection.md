# Data Collection Rules

## Core Contract

- Collect the complete portfolio universe once per `run_id`.
- Universe = `$check-portfolio` JSON `universe`, which already combines `recommanded`, `specified`, and direct KIS `holding` symbols.
- Main agent runs `scripts/collect_main_evidence.py` to collect required price/chart evidence through direct KIS REST and writes `price-chart.json`.
- `financial` and `news` reuse valid same-date memory caches when those caches cover the complete symbol universe. When a same-date cache is missing or incomplete, call the matching helper `get`, run the matching collector once, then call `get` again. If it is still missing, continue without that optional domain; if it exists but remains incomplete, pass the partial cache path into `decision-brief`. Do not retry the same optional collector more than once in a pipeline run.
- Verdict agents reuse saved artifacts. They never recollect and never call external tools.
- Preserve partial successes and errors. Do not drop a symbol silently.

## KIS Backoff

- Direct KIS calls may retry only retryable gateway, rate-limit, transport, timeout, or temporary routing failures.
- Retry the same API and parameters with exponential backoff up to 10 retries after the first call; suggested delays: 1, 2, 4, 8, 16, then 30 seconds.
- Record only APIs actually called in `attempts`, using non-sensitive parameters and final outcome.
- Authentication, token, credential, and permission errors are not local backoff targets; return them to the Main agent and follow `auth-token.md`.
- Use `find_api_detail` only for a new API type, a missing validated template, or an API schema rejection.

## Required Price/Chart Evidence

Main agent owns price/chart lookup through `scripts/collect_main_evidence.py`. It must write one canonical row for every input symbol:

- `schema_version="1"`
- `symbol_id`
- `symbol_name`
- `product_type`
- `price.current_or_last`
- `price.observed_at`
- `price.snapshot_mode`
- `eligible_for_verdict`
- `required_missing`, `local_signals`, `sources`, `errors`

Use `symbol_id`, `symbol_name`, and nested `price.*`; alias-only fields such as `symbol`, `stock_code`, `pdno`, `current_or_latest_price`, or numeric schema versions are not canonical.

If time or KIS volume is constrained, prioritize identity plus current-or-last price coverage for every symbol. Chart/NAV gaps may be recorded as missing evidence, but uncalled APIs must not be represented as collected evidence.

Price/chart collection may use quote, ETF/ETN, and daily/weekly/monthly chart APIs plus local calculations derived from those results. It must not call order APIs. The direct main-evidence helper may also collect the sanitized read-only account snapshot defined below, but must keep it in `account-before-order.json` rather than `price-chart.json`.

## Optional Domain Collectors

All optional collectors receive the full symbol universe, `run_id`, paths, and permission boundary when they are needed. Missing, failed, partial, skipped, cache-hit, or no-data optional output must not block merge, verdicts, target calculation, demo order, real order, or reservation order when required price/chart and account gates pass.

| Stage | Required skill/source | Launcher output | Main-agent use |
|---|---|---|---|
| `financial-collection` | Existing same-date full-universe cache, otherwise one collector attempt; KIS financial/estimate APIs only | `memory/collect-financial-information/financial-YYYY-MM-DD.yaml` path or fixed missing-cache message | cache path plus at most three short per-symbol bullets |
| `news-collection` | Existing same-date full-universe cache, otherwise one collector attempt; KIS news/disclosure only | `memory/collect-news-information/news-YYYY-MM-DD.yaml` path or fixed missing-cache message | cache path plus at most three short per-symbol items |

Optional launcher text must be only a cache path or fixed missing-cache message. It must contain no JSON envelope, code fences, raw API payloads, raw quote/news dumps, long source dumps, account data, tokens, app keys, app secrets, HTS IDs, or credential-like values. Main agent must not create `financial.md` or `news.md` run artifacts.

News cache entries are keyed by symbol code. Article fields are `article_date`, `sentiment`, and `content`; `sentiment` is `positive`, `neutral`, `negative`, or `mixed`. Do not store `title`, `symbol_id`, `updated_at`, or `errors` in the cache.

## Account Evidence

For universe construction, the Main agent uses `$check-portfolio` JSON and must not separately re-read current live holdings only to expand the universe.

Before order calculation or execution, `scripts/collect_main_evidence.py` creates the first sanitized account snapshot and `scripts/execute_orders.py` refreshes the read-only order gates needed for explicit submit runs. They may query:

- account asset summary
- current live holdings
- pending orders
- reservation orders
- same-day fills
- buy-available amount/quantity for buy candidates
- sell-available quantity for sell candidates, cross-checked with current live holdings minus active sell reservations

If `$check-portfolio` holdings lookup fails, current holdings are unknown; do not guess holdings or silently drop that source.

Latest `account-before-order.json` is required before order calculation. If missing, invalid, or `failed`, block order preparation and execution.

If `account-before-order.json` shows that active-order lookup or order-available lookup was not performed, order preparation and execution must remain blocked until `scripts/execute_orders.py` refreshes the required fields with validated read-only APIs for explicit submit runs.

Current live holdings already include same-day fills. Retain same-day fills as account evidence, but do not subtract them again.

## Symbol Eligibility

Set `eligible_for_verdict=false` only when one of these blocks even a price-based verdict:

- unresolved or ambiguous identity
- missing usable price or observation time
- price/chart failure that prevents price, trend, and risk assessment
- required identity/price errors remain unresolved

Financial/news absence, no-news results, non-applicable fields, failed optional wrappers, or optional cache gaps are not exclusion reasons by themselves. If identifier, name, price, and observation time exist, keep the symbol eligible. `price_only` is descriptive only and must not lower score, confidence, target quantity, or order permission.

Ineligible symbols remain in artifacts and report, but are excluded from verdict stages, target quantities, and orders.

## `decision-brief.json`

`decision-brief.json` is the compact canonical verdict input for the run record. Verdict sub-agents receive launcher-created lossless `verdict-core` slices derived from it; `second-verdict` also receives a selected-symbol first-verdict slice instead of full `verdict-first.json`.

Include only:

- symbol id/name/product type
- eligibility, evidence mode, exclusion reasons, warnings
- current-or-last price, observation timestamp, snapshot mode
- up to five price/chart signals per symbol
- optional financial/news summaries within the limits above
- account exposure summary
- non-sensitive missing/error context

Exclude raw API payloads, full article text, raw account attempts, repeated source details, and sensitive values. Summarize repeated optional missing-domain reasons once at run/domain level when useful.

## Sensitive Data

Never return, persist, or send to sub-agents account numbers, account product codes, access tokens, refresh tokens, app keys, app secrets, HTS IDs, authorization headers, cookies, or credential-like values.
