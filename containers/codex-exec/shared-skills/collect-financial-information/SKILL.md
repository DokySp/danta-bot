---
name: collect-financial-information
description: "Collect portfolio-wide Korean stock and ETF financial information from KIS and official sources only. Use for the financial collection agent in daily-trading, or when a user explicitly requests official financial data without news, account, balance, or order operations."
---

# Collect Financial Information

## Scope

Collect financial information for the supplied symbol list in one pass. Return one JSON object containing every requested symbol. When invoked by `daily-trading`, the caller supplies the complete portfolio universe and owns canonical artifact persistence. When invoked standalone, this skill owns only its financial cache through `scripts/financial_cache.py`.

Allowed sources:

- KIS market and estimate APIs
- DART, KRX, Korean government or regulator publications
- Issuer-operated investor-relations pages and official filings

Do not use blogs, social media, community posts, unofficial aggregators, or unsourced summaries.

## Permissions

- External calls: allowed only for the sources above.
- KIS calls: direct calls only, with bounded backoff for retryable KIS failures.
- Account, balance, order-available, fill-history, pending-order, reservation-order, and order APIs: forbidden.
- File writes outside the financial cache helper: forbidden.
- Order submission, reservation submission, correction, cancellation, and order revision: forbidden.
- Cache writes are allowed only in standalone mode through `scripts/financial_cache.py put` after validation passes. When this skill is running as a `daily-trading` sub-agent, return cache update candidates to the Main agent instead of writing files directly.
- Secrets such as account numbers, tokens, app keys, app secrets, and HTS IDs: never request or return.

## KIS Backoff

- Before the first use of each KIS API type, inspect current parameters with `find_api_detail`.
- For retryable KIS/MCP API error codes or messages, including rate-limit, temporary gateway/routing, transport, and timeout failures, retry the same API with the same parameters using exponential backoff up to 10 retries after the initial call.
- Recommended delay sequence is 1, 2, 4, 8, 16, then 30 seconds capped for remaining retries. Add small jitter when the runtime supports it.
- Preserve every attempt in `attempts`, including API name, non-sensitive parameters, error code/message, delay, and final outcome.
- Record only APIs actually called in `attempts`. Do not add placeholder attempts for APIs that were considered but skipped; use per-symbol `required_missing` or `errors` for skipped data classes.
- Authentication, token, credential, and permission errors are not local backoff targets. Return those errors to the daily-trading Main agent; do not call `auth_token`.

## Cache-First Rule

Financial data is valid for one Korea trading day.

- Cache location: `~/.cache/codex/collect-financial-information/<YYYY-MM-DD>.json`
- Use `FINANCIAL_CACHE_DIR` when set.
- The date is the Korea trading date for the run.
- If a valid same-trading-day cache exists or the caller supplies a valid same-trading-day cache payload and `force_refresh=false`, return a `financial.json`-shaped envelope from cache and do not make external financial calls.
- If no valid cache payload exists, the cache is stale, or `force_refresh=true`, collect from KIS and official sources.
- On cache miss collection, include `cache_update` in the returned envelope. In `daily-trading`, the Main agent persists it. In standalone mode, validate and persist it with `scripts/financial_cache.py put`.
- Use `scripts/financial_cache.py get` and `scripts/financial_cache.py put` for direct cache access. Do not hand-roll cache validation or write cache files with shell redirection.
- Failed, malformed, empty, wrong-date, wrong-stage, or missing-requested-symbol cache payloads must not be used as cache hits and must not overwrite an existing cache.

## Cache Helper

Run helper commands from the workspace root by using the installed or bundled skill directory:

```bash
python3 <collect-financial-information-skill-dir>/scripts/financial_cache.py get --date YYYY-MM-DD --symbols "005930,000660"
python3 <collect-financial-information-skill-dir>/scripts/financial_cache.py put --date YYYY-MM-DD --symbols "005930,000660" < financial.json
python3 <collect-financial-information-skill-dir>/scripts/financial_cache.py eval --date YYYY-MM-DD --symbols "005930,000660" < financial.json
```

When the current working directory is the skill directory itself, `scripts/financial_cache.py` is also valid.

The helper accepts either a direct `financial-collection` envelope or the wrapper format it writes:

```json
{
  "schema_version": "1",
  "trading_date": "YYYY-MM-DD",
  "cache_update": {},
  "payload": { "stage": "financial-collection" }
}
```

Validation requires `schema_version="1"`, `stage="financial-collection"`, `domain="financial"`, `status` of `success` or `partial`, matching `trading_date`, and a non-empty `symbols` list. When `--symbols` or `--symbols-file` is provided, those symbols are treated as the requested subset: the cache is valid if every requested symbol is present, and additional cached symbols are allowed. `get` returns only the requested symbols when a subset is provided; otherwise it returns the full cached payload.

## Workflow

1. Accept `run_id`, `started_at`, trading environment, and the requested symbol list.
2. Accept `trading_date`, `force_refresh`, and any caller-supplied cache payload.
3. In standalone mode, run `scripts/financial_cache.py get` for the trading date and requested symbol list. In `daily-trading`, use the caller-supplied cache payload that the Main agent already validated with the same helper.
4. Validate the cache payload by trading date, schema version, stage, domain, status, non-empty symbols, requested-symbol presence, and absence of failure status.
5. On valid cache hit, return from cache with `cache.status="hit"` and no external calls.
6. On cache miss, call KIS directly and apply the KIS backoff rules before finalizing any failed KIS result.
7. For every symbol, attempt both the applicable KIS financial search and an official-source search.
8. For stocks, collect available valuation, earnings, balance-sheet, profitability, leverage, dividend, estimate, and official filing data.
9. For ETFs/ETNs, collect issuer facts and official product data that are financial in nature. Mark stock-only fields `not_applicable`.
10. Preserve source names, source URLs when available, observation dates, raw KIS field names, missing fields, and per-symbol errors.
11. Return the JSON envelope below. Missing financial data is recorded in `required_missing` or `errors`; final daily-trading eligibility is decided by the Main agent after merge and may remain `price_only` when identifier and price snapshot are available.
12. Before final output, validate that the response is a single valid JSON object. Do not emit Markdown, code fences, explanatory text, trailing comments, or partial JSON.
13. Keep the output compact. Do not include long raw API payloads. Keep raw KIS field evidence to the minimal fields needed to support the summary, cap source lists to five entries per symbol, cap `required_missing` and `errors` to concise unique values, and place reusable source metadata once when possible.
14. In standalone mode after a successful cache-miss collection, run `scripts/financial_cache.py put` with the same trading date and requested symbol list. If validation fails, report the helper error and do not write the cache.

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
    "cache_path": "~/.cache/codex/collect-financial-information/<YYYY-MM-DD>.json",
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

If the complete financial payload risks becoming too large or malformed, prefer a valid compact `partial` envelope over a verbose invalid response. A valid `partial` response with per-symbol missing fields is more useful than a failed wrapper with no `parsed_json`.
