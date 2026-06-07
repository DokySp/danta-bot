---
name: collect-news-information
description: "Collect portfolio-wide Korean stock and ETF news from KIS news and disclosure-related APIs only. Use for the news collection agent in daily-trading, or when a user explicitly requests KIS-sourced news without account, balance, or order operations."
---

# Collect News Information

## Scope

Collect current KIS news and disclosure context for the complete supplied symbol list in one pass. Return one compressed JSON object containing every symbol. Do not write files; the caller owns artifact persistence.

Allowed sources:

- KIS news APIs such as `domestic_stock(api_type="news_title")`
- KIS disclosure-related APIs or KIS-returned disclosure records

Forbidden sources:

- web search
- current web news search
- direct publisher websites
- blogs, social media, forums, community posts, unofficial aggregators, or unsourced summaries
- full article text or long raw news payloads

## Permissions

- External calls: allowed only through KIS MCP news/disclosure-related APIs.
- KIS calls: direct calls only, with bounded backoff for retryable KIS failures.
- Account, balance, order-available, fill-history, pending-order, reservation-order, and order APIs: forbidden.
- File writes and order submission: forbidden.
- Secrets such as account numbers, tokens, app keys, app secrets, and HTS IDs: never request or return.

## KIS Backoff

- Before the first use of each KIS API type, inspect current parameters with `find_api_detail`.
- For retryable KIS/MCP API error codes or messages, including rate-limit, temporary gateway/routing, transport, and timeout failures, retry the same API with the same parameters using exponential backoff up to 10 retries after the initial call.
- Recommended delay sequence is 1, 2, 4, 8, 16, then 30 seconds capped for remaining retries. Add small jitter when the runtime supports it.
- Preserve every attempt in `attempts`, including API name, non-sensitive parameters, error code/message, delay, and final outcome.
- Authentication, token, credential, and permission errors are not local backoff targets. Return those errors to the daily-trading Main agent; do not call `auth_token`.

## Workflow

1. Accept `run_id`, `started_at`, trading environment, and the complete symbol list.
2. Call KIS directly and apply the KIS backoff rules before finalizing any failed KIS result.
3. Search each symbol by identifier and unambiguous name using only KIS news/disclosure-related APIs.
4. Record market-wide KIS news separately from symbol-specific KIS news.
5. Deduplicate substantially identical KIS items while preserving distinct KIS identifiers when available.
6. Summarize each item factually and briefly. Do not include full article text.
7. Return the JSON envelope below. A completed KIS search with no relevant stories is valid and uses an empty `items` list. A failed KIS search is an error, but final daily-trading eligibility is decided by the Main agent after merge and may remain `price_only` when identifier and price snapshot are available.

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
  "market_news": [
    {
      "kis_id": "",
      "title": "",
      "published_at": "",
      "publisher": "",
      "source": "KIS news | KIS disclosure",
      "short_summary": "",
      "affected_symbols": [],
      "risk_tags": [],
      "opportunity_tags": [],
      "errors": []
    }
  ],
  "symbols": [
    {
      "symbol_id": "",
      "symbol_name": "",
      "kis_search_completed": true,
      "disclosure_search_completed": true,
      "eligible_for_verdict": true,
      "required_missing": [],
      "items": [
        {
          "kis_id": "",
          "title": "",
          "published_at": "",
          "publisher": "",
          "source": "KIS news | KIS disclosure",
          "short_summary": "",
          "affected_symbols": [],
          "risk_tags": [],
          "opportunity_tags": [],
          "errors": []
        }
      ],
      "errors": []
    }
  ]
}
```

Use only `success`, `partial`, or `failed` for `status`. A completed KIS search with no relevant news is valid; a failed KIS news or disclosure lookup is recorded in `required_missing` and `errors`. Do not mark a symbol permanently ineligible solely because news is absent; the daily-trading Main agent applies the final price-only eligibility rule. Do not fabricate titles, publication times, publishers, summaries, tags, or conclusions.
