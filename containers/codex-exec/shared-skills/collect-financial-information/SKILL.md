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
- KIS calls: allowed only through `$gate-kis-calls`.
- Account, balance, order-available, fill-history, pending-order, reservation-order, and order APIs: forbidden.
- File writes and order submission: forbidden.
- Secrets such as account numbers, tokens, app keys, app secrets, and HTS IDs: never request or return.

## Workflow

1. Accept `run_id`, `started_at`, trading environment, and the complete symbol list.
2. Before every KIS call, use `$gate-kis-calls`; inspect current parameters with `find_api_detail` before the first use of each API.
3. For every symbol, attempt both the applicable KIS financial search and an official-source search.
4. For stocks, collect available valuation, earnings, balance-sheet, profitability, leverage, dividend, estimate, and official filing data.
5. For ETFs/ETNs, collect issuer facts and official product data that are financial in nature. Mark stock-only fields `not_applicable`.
6. Preserve source names, source URLs when available, observation dates, raw KIS field names, missing fields, and per-symbol errors.
7. Return the JSON envelope below. A symbol with insufficient required financial data must have `eligible_for_verdict=false`.

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
  ]
}
```

Use only `success`, `partial`, or `failed` for `status`. No-data results from a completed official-source search are not invented; record them in `required_missing` or `errors`.
