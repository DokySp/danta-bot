---
name: collect-news-information
description: "Collect portfolio-wide Korean stock and ETF news from KIS and current web news sources. Use for the news collection agent in daily-trading, or when a user explicitly requests sourced news without account, balance, or order operations."
---

# Collect News Information

## Scope

Collect current news and event context for the complete supplied symbol list in one pass. Return one JSON object containing every symbol. Do not write files; the caller owns artifact persistence.

Allowed sources:

- KIS news and disclosure-related APIs
- Issuer, exchange, regulator, and government announcements
- Current web news with identifiable publisher, publication time, and URL

## Permissions

- External KIS and web calls: allowed.
- KIS calls: allowed only through `$gate-kis-calls`.
- Account, balance, order-available, fill-history, pending-order, reservation-order, and order APIs: forbidden.
- File writes and order submission: forbidden.
- Secrets such as account numbers, tokens, app keys, app secrets, and HTS IDs: never request or return.

## Workflow

1. Accept `run_id`, `started_at`, trading environment, and the complete symbol list.
2. Before every KIS call, use `$gate-kis-calls`; inspect current parameters with `find_api_detail` before the first use of each API.
3. Search each symbol by identifier and unambiguous name using both KIS news APIs such as `domestic_stock(api_type="news_title")` and current web news search. Record market-wide news separately from symbol-specific news.
4. Deduplicate substantially identical stories while preserving all distinct source URLs.
5. Record publisher, publication time, URL, short factual summary, affected symbols, and risk/opportunity tags.
6. Return the JSON envelope below. A completed search with no relevant stories is valid and uses an empty `items` list. A failed search is an error.

## Required Output

```json
{
  "schema_version": "1",
  "run_id": "<run_id>",
  "started_at": "<Asia/Seoul ISO-8601>",
  "generated_at": "",
  "stage": "news-collection",
  "domain": "news",
  "status": "success | partial | failed",
  "skipped": false,
  "skip_reason": "",
  "attempts": [],
  "errors": [],
  "market_news": [],
  "symbols": [
    {
      "symbol_id": "",
      "symbol_name": "",
      "kis_search_completed": true,
      "web_search_completed": true,
      "eligible_for_verdict": true,
      "required_missing": [],
      "items": [
        {
          "publisher": "",
          "published_at": "",
          "url": "",
          "summary": "",
          "tags": []
        }
      ],
      "errors": []
    }
  ]
}
```

Use only `success`, `partial`, or `failed` for `status`. A completed search with no relevant news is valid; a failed KIS or web search is recorded in `required_missing` and `errors`. Do not fabricate article text, publication times, URLs, or conclusions.
