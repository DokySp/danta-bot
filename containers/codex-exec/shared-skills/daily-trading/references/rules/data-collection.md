# Data Collection Rules

## One-Pass Rule

- The complete portfolio universe is collected in detail once per `run_id`.
- The complete portfolio universe is the union of configured/requested symbols from `$check-portfolio` and current live holdings from the initial read-only account snapshot.
- `market`, `financial`, and `news` collection sub-agents may run in parallel and each receives the same complete symbol list.
- `market` returns one JSON object containing all symbols. `financial` and `news` are optional best-effort artifacts.
- The launcher spec for the market collector must include the complete universe in `symbol_ids` so the wrapper can fail fast when market silently drops a symbol.
- `first-verdict` and `second-verdict` agents reuse the saved snapshots. They never recollect or call external tools.
- Retries are allowed only to recover failed KIS calls before that domain snapshot is finalized. Preserve all attempts in the domain JSON.
- If the market agent fails without valid JSON, the Main agent creates a `failed` envelope and adds the same required agent-level error to every symbol so no symbol silently disappears. If financial/news collection fails, record a non-blocking warning and continue with market/account evidence.

## KIS Call Backoff

- KIS calls made by collection agents are direct calls. Do not use a shared KIS call gate.
- Use validated parameter templates for known KIS APIs. Call `find_api_detail` only when introducing a new API type, when no validated template exists, or after an API rejects the template for parameter/schema reasons.
- Do not group multiple KIS calls from the same agent with `multi_tool_use.parallel`.
- For retryable KIS/MCP API error codes or messages, including rate-limit, temporary gateway/routing, transport, and timeout failures, retry the same API with the same parameters using exponential backoff up to 10 retries after the initial call.
- Recommended delay sequence is 1, 2, 4, 8, 16, then 30 seconds capped for remaining retries. Add small jitter when the runtime supports it.
- Record only APIs actually called in `attempts`, with API name, non-sensitive parameters, error code/message, delay, and final outcome. Do not add `attempts` entries for APIs that were considered but not called; record those as per-symbol `required_missing` or `errors` instead.
- Authentication, token, credential, and permission errors are not local backoff targets. Return them to the Main agent so `auth-token.md` can handle token reissue centrally.

## Domain Responsibilities

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
- daily, weekly, and monthly chart data sufficient for local signals when collected
- investor flow or an explicit missing/error result when collected

Required ETF/ETN information:

- resolvable identifier and name
- current or most recent valid price
- observation timestamp
- NAV or an explicit missing/error result

If the market collector is constrained by time or KIS call volume, prioritize identity and price coverage for every input symbol first. It may return `status="partial"` with chart, order book, investor flow, rank, industry, or NAV gaps in per-symbol `required_missing`, but it must not inflate `attempts` with uncalled APIs.

Forbidden:

- account, balance, order-available, fill-history, pending-order, reservation-order, and order APIs
- file writes and order submission

### Financial Collector

Must use `$collect-financial-information`. KIS and official sources only.

Financial collection is best-effort. Missing, failed, partial, no-data, or skipped financial collection must not stop merge, verdicts, target calculation, or order execution when market/account gates pass.

Financial collection returns compact Markdown text, not JSON. The Main agent writes the sanitized text to `financial.md` when available and may copy only short bullets into `decision-brief.json`.

Required text constraints:

- no JSON envelope
- no code fences
- no raw API payloads
- no account, token, app key, app secret, HTS ID, or other sensitive values
- no long source dumps
- one compact section per relevant symbol when data exists
- a short "no usable financial context" note is acceptable when the collector finds nothing

### News Collector

When news collection is attempted, use `$collect-news-information`. KIS news and disclosure-related APIs only.

News collection is best-effort. Missing, failed, partial, no-data, or skipped news collection must not stop merge, verdicts, target calculation, or order execution when market/account gates pass.

News collection returns compact Markdown text, not JSON. The Main agent writes the sanitized text to `news.md` when available and may copy only short bullets into `decision-brief.json`.

Forbidden:

- web search
- web news sources
- issuer, exchange, regulator, or government websites outside KIS-returned news/disclosure data
- full article text or long raw news payloads

News output is compressed to KIS identifiers, title, publication time, publisher/source, short factual summary, affected symbols, risk tags, opportunity tags, and errors.

## Account Collection

The Main agent performs read-only account lookup directly, then sanitizes and writes the account JSON artifacts.

The Main agent may query:

- account asset summary
- current live holdings
- pending orders
- reservation orders
- same-day fills
- buy-available amount or quantity for buy candidates
- sell-available quantity for sell candidates

Account lookup code must not submit, reserve, correct, cancel, or persist sensitive values.

Initial snapshot for `account-before-verdict.json`:

- account asset summary
- current live holdings
- pending orders
- reservation orders
- same-day fills

If this snapshot fails, continue collection and verdicts only for requested/configured symbols, mark current holdings unknown, and block target-to-order conversion and every order operation.

When this snapshot succeeds, add every current live holding to the complete portfolio universe even if that symbol is absent from `$check-portfolio`. Do not drop a live holding solely because it was not configured.

Latest snapshot for `account-before-order.json`:

- refresh all fields above
- buy-available amount and quantity for buy candidates
- sell-available quantity for sell candidates

Current live holdings already reflect same-day fills. Same-day fill quantities are retained only as a repeated-trade guard and are not subtracted from holdings.

If either account snapshot is missing, invalid, or `failed`, the Main agent must block order preparation and execution.

## Symbol Eligibility

The Main agent decides eligibility after merging required market/account evidence and any available financial/news snapshots.

Set `eligible_for_verdict=false` only when one of the following prevents even a price-based verdict:

- unresolved or ambiguous symbol identity
- missing usable price and observation time
- market collection failure that prevents price, trend, and risk assessment
- domain errors explicitly marked as required and unresolved for symbol identity or price

Financial collection failure, missing financial fields, absent financial artifact, news lookup failure, absent news artifact, and no-news results are not exclusion reasons by themselves. If a symbol has a resolved identifier, name, current-or-last price, and observation time, keep `eligible_for_verdict=true`.

Do not require `evidence_mode="price_only"` solely because financial/news data is missing. If `price_only` is used, it is descriptive only and must not lower eligibility, target quantities, or order permission by itself.

An empty but successfully completed news search is not a failure. Product-specific non-applicable fields are not missing data.

Ineligible symbols remain in every applicable artifact and the final report, but are excluded from both verdict stages, target quantities, and orders.

## decision-brief.json

The Main agent creates `decision-brief.json` directly from required market/account evidence and any available financial/news summaries.

Include:

- symbol id and name
- eligibility, evidence mode, warnings, and exclusion reasons
- current or most recent valid price and observation timestamp
- price snapshot mode
- core market signals
- core financial summary when available
- core KIS news/disclosure summary when available
- account exposure summary
- optional missing fields and non-sensitive errors

Exclude:

- long raw API payloads
- full article text
- repeated source details
- account numbers, account product codes, tokens, app keys, app secrets, HTS IDs, auth headers, and credential-like values

`first-verdict` and `second-verdict` agents use `decision-brief.json` as their input.

Keep `decision-brief.json` compact enough for fan-out verdict stages:

- include at most five market signals per symbol
- include at most three financial summary bullets per symbol
- include at most three KIS news/disclosure summary items per symbol
- include at most five warnings/errors per symbol
- summarize repeated optional missing-domain reasons once at the run or domain level when useful
- never include raw API payloads, raw account lookup attempts, full article text, or repeated source metadata

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
