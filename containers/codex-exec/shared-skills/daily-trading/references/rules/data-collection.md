# Data Collection Rules

## One-Pass Rule

- The complete portfolio universe is collected in detail once per `run_id`.
- `market`, `financial`, and `news` collection sub-agents run in parallel and each receives the same complete symbol list.
- Each domain returns one JSON object containing all symbols.
- The launcher spec for each collection agent must include the complete universe in `symbol_ids` so the wrapper can fail fast when a domain silently drops a symbol.
- `first-verdict` and `second-verdict` agents reuse the saved snapshots. They never recollect or call external tools.
- Retries are allowed only to recover failed KIS calls before that domain snapshot is finalized. Preserve all attempts in the domain JSON.
- If a domain agent fails without valid JSON, the Main agent creates a `failed` envelope and adds the same required agent-level error to every symbol so no symbol silently disappears.

## KIS Call Backoff

- KIS calls made by collection agents are direct calls. Do not use a shared KIS call gate.
- Inspect current parameters with the relevant tool's `find_api_detail` before the first call to each API type.
- Do not group multiple KIS calls from the same agent with `multi_tool_use.parallel`.
- For retryable KIS/MCP API error codes or messages, including rate-limit, temporary gateway/routing, transport, and timeout failures, retry the same API with the same parameters using exponential backoff up to 10 retries after the initial call.
- Recommended delay sequence is 1, 2, 4, 8, 16, then 30 seconds capped for remaining retries. Add small jitter when the runtime supports it.
- Record every attempt with API name, non-sensitive parameters, error code/message, delay, and final outcome.
- Authentication, token, credential, and permission errors are not local backoff targets. Return them to the Main agent so `auth-token.md` can handle token reissue centrally.

## Domain Responsibilities

### Market Status Preflight

Before account snapshots, collection, verdicts, or order preparation, the Main agent runs `$check-holiday` for the Asia/Seoul target date. Record the normalized `status` as `open`, `closed`, or `unknown` in `run.json`, `decision-brief.json`, and the report.

- `open`: collect normal live market snapshots.
- `closed`: collect or reuse the most recent valid trading-day price snapshot and label `price.snapshot_mode="previous_trading_day"`. This only changes the market snapshot mode and execution path; it does not skip financial collection, news/disclosure collection, or any collection sub-agent.
- `unknown`: collection and analysis may continue, but real order and reservation submission are blocked.

### Market Collector

Allowed:

- KIS price, daily/weekly/monthly chart, intraday chart, order book, trade, investor flow, rank, industry, and ETF/ETN NAV APIs
- Local calculations derived only from collected market data

The market collector owns symbol market information and price snapshot lookup. The Main agent must not directly call price, quote, chart, order-book, flow, rank, industry, or ETF/NAV APIs to substitute for the market collector.

The market collector must return the canonical `Market JSON Shape` below. Alias-only fields are not sufficient for canonical artifacts:

- Use `symbol_id`, not `symbol`, `pdno`, `stock_code`, or `code`.
- Use `symbol_name`, not only `market_identity.name`.
- Use `price.current_or_last`, `price.observed_at`, and `price.snapshot_mode`, not only `current_or_latest_price`.
- Use `schema_version="1"`, not numeric `1`, `"1.0"`, or other variants.
- Include one row for every input `symbol_ids` entry. When a symbol cannot be priced, keep its row, set `eligible_for_verdict=false`, and record the required per-symbol error instead of dropping it.

Required stock information:

- resolvable identifier and name
- current or most recent valid price
- observation timestamp
- daily, weekly, and monthly chart data sufficient for local signals
- investor flow or an explicit missing/error result

Required ETF/ETN information:

- resolvable identifier and name
- current or most recent valid price
- observation timestamp
- NAV or an explicit missing/error result

Forbidden:

- account, balance, order-available, fill-history, pending-order, reservation-order, and order APIs
- file writes and order submission

### Financial Collector

Must use `$collect-financial-information`. KIS and official sources only.

Market status `closed` is not a financial-data condition. Unless a valid cache hit is used, the financial collector must still attempt the portfolio-wide financial collection and return per-symbol results or per-symbol errors.

Financial collection is cache-first:

- Cache path: `reports/cache/financial/<YYYY-MM-DD>.json`
- Validity: one Korea trading day
- Cache hit: the Main agent creates `financial.json` from the cache without external financial calls
- Cache miss or explicit force refresh: the financial collector uses KIS and official sources, returns the fresh envelope, and the Main agent updates the cache
- The financial collector may return a cache update candidate, but it must not write files directly

### News Collector

Must use `$collect-news-information`. KIS news and disclosure-related APIs only.

Market status `closed` is not a news-data condition. The news collector must still attempt KIS news/disclosure collection and return per-symbol summaries, empty successful searches, or per-symbol errors.

Forbidden:

- web search
- web news sources
- issuer, exchange, regulator, or government websites outside KIS-returned news/disclosure data
- full article text or long raw news payloads

News output is compressed to KIS identifiers, title, publication time, publisher/source, short factual summary, affected symbols, risk tags, opportunity tags, and errors.

## Account Collection

The Main agent delegates read-only account lookup to `$collect-account-state`, then sanitizes and writes the returned JSON.

The account sub-agent may query:

- account asset summary
- current live holdings
- pending orders
- reservation orders
- same-day fills
- buy-available amount or quantity for buy candidates
- sell-available quantity for sell candidates

The account sub-agent must not submit, reserve, correct, cancel, write files, return sensitive values, or make trading decisions.

Initial snapshot for `account-before-verdict.json`:

- account asset summary
- current live holdings
- pending orders
- reservation orders
- same-day fills

If this snapshot fails, continue collection and verdicts only for requested/configured symbols, mark current holdings unknown, and block target-to-order conversion and every order operation.

Latest snapshot for `account-before-order.json`:

- refresh all fields above
- buy-available amount and quantity for buy candidates
- sell-available quantity for sell candidates

Current live holdings already reflect same-day fills. Same-day fill quantities are retained only as a repeated-trade guard and are not subtracted from holdings.

If either account snapshot is missing, invalid, or `failed`, the Main agent must block order preparation and execution.

## Symbol Eligibility

The Main agent decides eligibility after merging all three domain snapshots.

Set `eligible_for_verdict=false` only when one of the following prevents even a price-based verdict:

- unresolved or ambiguous symbol identity
- missing usable price and observation time
- market collection failure that prevents price, trend, and risk assessment
- domain errors explicitly marked as required and unresolved for symbol identity or price

Financial collection failure, missing financial fields, news lookup failure, and no-news results are not exclusion reasons by themselves. If a symbol has a resolved identifier, name, current-or-last price, and observation time, keep `eligible_for_verdict=true`, set `evidence_mode="price_only"`, and record financial/news gaps in `required_missing`, `warnings`, or non-sensitive `errors`.

Use `evidence_mode="price_only"` only after the relevant financial/news collector or cache path actually returned missing, failed, or no-data evidence for that symbol. Do not mark a symbol `price_only` merely because the market is `closed`, because the run uses previous-trading-day prices, or because the Main agent skipped a required collector.

An empty but successfully completed news search is not a failure. Product-specific non-applicable fields are not missing data.

Ineligible symbols remain in every applicable artifact and the final report, but are excluded from both verdict stages, target quantities, and orders.

## decision-brief.json

After `merged.json` is complete, the Main agent creates `decision-brief.json` for verdict agents.

Include:

- symbol id and name
- eligibility, evidence mode, warnings, and exclusion reasons
- current or most recent valid price and observation timestamp
- market status and live/previous-trading-day snapshot mode
- core market signals
- core financial summary when available
- core KIS news/disclosure summary when available
- account exposure summary
- missing fields and non-sensitive errors

Exclude:

- long raw API payloads
- full article text
- repeated source details
- account numbers, account product codes, tokens, app keys, app secrets, HTS IDs, auth headers, and credential-like values

`first-verdict`, `second-verdict`, and `final-risk-verdict` agents use `decision-brief.json` as their default input instead of raw `merged.json`.

## Market JSON Shape

```json
{
  "schema_version": "1",
  "run_id": "<run_id>",
  "started_at": "<Asia/Seoul ISO-8601>",
  "generated_at": "",
  "stage": "market-collection",
  "domain": "market",
  "status": "success | partial | failed",
  "skipped": false,
  "skip_reason": "",
  "attempts": [],
  "errors": [],
  "symbols": [
    {
      "symbol_id": "",
      "symbol_name": "",
      "product_type": "stock | etf | etn | other | unresolved",
      "eligible_for_verdict": true,
      "required_missing": [],
      "market_context": {},
      "price": {
        "current_or_last": null,
        "observed_at": "",
        "snapshot_mode": "live | previous_trading_day"
      },
      "charts": {"daily": [], "weekly": [], "monthly": []},
      "order_book": {},
      "trades": [],
      "investor_flow": {},
      "rank_and_industry": {},
      "etf_etn": {},
      "local_signals": [],
      "sources": [],
      "errors": []
    }
  ]
}
```

## Sensitive Data

Never return or persist account numbers, account product codes, access tokens, app keys, app secrets, HTS IDs, or raw authentication headers.
