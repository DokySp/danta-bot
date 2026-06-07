---
name: collect-financial-information
description: "Collect portfolio-wide Korean stock and ETF financial information from KIS and official sources only. Use for the financial collection agent in daily-trading, or when a user explicitly requests official financial data without news, account, balance, or order operations."
---

# Collect Financial Information

## Scope

Collect financial information for the complete supplied symbol list in one pass. Return one JSON object containing every symbol. Do not write files; the caller owns artifact persistence.

Allowed sources:

- KIS market and estimate APIs
- DART, KRX, Korean government or regulator publications
- Issuer-operated investor-relations pages and official filings

Do not use blogs, social media, community posts, unofficial aggregators, or unsourced summaries.

## Permissions

- External calls: allowed only for the sources above.
- KIS calls: direct calls only, with bounded backoff for retryable KIS failures.
- Account, balance, order-available, fill-history, pending-order, reservation-order, and order APIs: forbidden.
- File writes and order submission: forbidden.
- Cache writes: forbidden. Return cache update candidates to the caller instead.
- Secrets such as account numbers, tokens, app keys, app secrets, and HTS IDs: never request or return.

## KIS Backoff

- Before the first use of each KIS API type, inspect current parameters with `find_api_detail`.
- For retryable KIS/MCP API error codes or messages, including rate-limit, temporary gateway/routing, transport, and timeout failures, retry the same API with the same parameters using exponential backoff up to 10 retries after the initial call.
- Recommended delay sequence is 1, 2, 4, 8, 16, then 30 seconds capped for remaining retries. Add small jitter when the runtime supports it.
- Preserve every attempt in `attempts`, including API name, non-sensitive parameters, error code/message, delay, and final outcome.
- Authentication, token, credential, and permission errors are not local backoff targets. Return those errors to the daily-trading Main agent; do not call `auth_token`.

## Cache-First Rule

Financial data is valid for one Korea trading day.

- Cache location: `reports/cache/financial/<YYYY-MM-DD>.json`
- The date is the Korea trading date for the run.
- If the caller supplies a valid same-trading-day cache payload and `force_refresh=false`, return a `financial.json`-shaped envelope from cache and do not make external financial calls.
- If no valid cache payload exists, the cache is stale, or `force_refresh=true`, collect from KIS and official sources.
- On cache miss collection, include `cache_update` in the returned envelope so the daily-trading `Main agent` can persist it.
- Do not read cache files directly. Never write cache files. The caller provides any cache payload as input and owns filesystem persistence.

## Workflow

1. Accept `run_id`, `started_at`, trading environment, and the complete symbol list.
2. Accept `trading_date`, `force_refresh`, and any caller-supplied cache payload.
3. Validate the cache payload by trading date, schema version, symbol coverage, and absence of failure status.
4. On valid cache hit, return from cache with `cache.status="hit"` and no external calls.
5. On cache miss, call KIS directly and apply the KIS backoff rules before finalizing any failed KIS result.
6. For every symbol, attempt both the applicable KIS financial search and an official-source search.
7. For stocks, collect available valuation, earnings, balance-sheet, profitability, leverage, dividend, estimate, and official filing data.
8. For ETFs/ETNs, collect issuer facts and official product data that are financial in nature. Mark stock-only fields `not_applicable`.
9. Preserve source names, source URLs when available, observation dates, raw KIS field names, missing fields, and per-symbol errors.
10. Return the JSON envelope below. Missing financial data is recorded in `required_missing` or `errors`; final daily-trading eligibility is decided by the Main agent after merge and may remain `price_only` when identifier and price snapshot are available.

## Required Output

```json
{
  "schema_version": "1",
  "run_id": "<run_id>",
  "started_at": "<Asia/Seoul ISO-8601>",
  "generated_at": "",
  "stage": "financial-collection",
  "domain": "financial",
  "status": "success | partial | failed",
  "skipped": false,
  "skip_reason": "",
  "cache": {
    "status": "hit | miss | refresh",
    "trading_date": "<YYYY-MM-DD>",
    "cache_path": "reports/cache/financial/<YYYY-MM-DD>.json",
    "used_external_calls": false,
    "reason": ""
  },
  "attempts": [],
  "errors": [],
  "symbols": [
    {
      "symbol_id": "",
      "symbol_name": "",
      "product_type": "stock | etf | etn | other | unresolved",
      "kis_search_completed": true,
      "official_search_completed": true,
      "eligible_for_verdict": true,
      "required_missing": [],
      "valuation": {},
      "earnings": {},
      "balance_sheet": {},
      "profitability": {},
      "leverage": {},
      "dividend": {},
      "estimates": {},
      "official_filings": [],
      "sources": [],
      "errors": []
    }
  ],
  "cache_update": {}
}
```

Use only `success`, `partial`, or `failed` for `status`. No-data results from a completed official-source search are not invented; record them in `required_missing` or `errors`. Do not mark a symbol permanently ineligible solely because financial data is absent; the daily-trading Main agent applies the final price-only eligibility rule.
