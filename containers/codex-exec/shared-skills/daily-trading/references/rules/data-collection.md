# Data Collection Rules

## One-Pass Rule

- The complete portfolio universe is collected in detail once per `run_id`.
- `market`, `financial`, and `news` collection sub-agents run in parallel and each receives the same complete symbol list.
- Each domain returns one JSON object containing all symbols.
- First and second verdict agents reuse the saved snapshots. They never recollect or call external tools.
- A retry is allowed only to recover a failed collection call before that domain snapshot is finalized. Preserve all attempts in the domain JSON.
- If a domain agent fails without valid JSON, the main agent creates a `failed` envelope and adds the same required agent-level error to every symbol so no symbol silently disappears.

## KIS Call Gate

- Every KIS call made by a collection agent must use `$gate-kis-calls`.
- One gate lease permits exactly one KIS call.
- Inspect current parameters with the relevant tool's `find_api_detail` before the first call to each API type.
- Do not group KIS calls with `multi_tool_use.parallel`.
- `EGW00201` and rate-limit errors are recorded and retried only within the gate rules.

## Domain Responsibilities

### Market Collector

Allowed:

- KIS price, daily/weekly/monthly chart, intraday chart, order book, trade, investor flow, rank, industry, and ETF/ETN NAV APIs
- Local calculations derived only from collected market data

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

### News Collector

Must use `$collect-news-information`. KIS and current web news are allowed.

## Account Collection

Only the main agent calls account or ledger APIs.

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

## Symbol Eligibility

The main agent decides eligibility after merging all three domain snapshots.

Set `eligible_for_verdict=false` when any of the following prevents an evidence-based verdict:

- unresolved or ambiguous symbol identity
- missing usable price and observation time
- market collection failure that prevents trend/risk assessment
- stock financial collection failure that prevents fundamental assessment
- news search failure that prevents event-risk assessment
- domain errors explicitly marked as required and unresolved

An empty but successfully completed news search is not a failure. Product-specific non-applicable fields are not missing data.

Ineligible symbols remain in every applicable artifact and the final report, but are excluded from both verdict stages, target quantities, and orders.

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
      "price": {},
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
