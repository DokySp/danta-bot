---
name: collect-news-information
description: "Collect compact Korean stock and ETF news context from KIS news and disclosure-related APIs only. Use for the news collection agent in daily-trading, or when a user explicitly requests KIS-sourced news without account, balance, or order operations."
---

# Collect News Information

## Scope

Collect current KIS news and disclosure context for the complete supplied symbol list in one pass. Return compact Markdown text only. Do not return a JSON envelope, and do not write files; the caller owns artifact persistence.

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

- Use existing validated parameter templates for known KIS API calls.
- Call `find_api_detail` only when no validated template exists, a new API type is introduced, or KIS rejects the template.
- For retryable KIS/MCP API error codes or messages, including rate-limit, temporary gateway/routing, transport, and timeout failures, retry the same API with the same parameters using exponential backoff up to 10 retries after the initial call.
- Recommended delay sequence is 1, 2, 4, 8, 16, then 30 seconds capped for remaining retries. Add small jitter when the runtime supports it.
- Preserve attempt summaries in the Markdown only when they materially change confidence. Do not include sensitive parameters.
- Authentication, token, credential, and permission errors are not local backoff targets. Return a concise note to the daily-trading Main agent; do not call `auth_token`.

## Workflow

1. Accept `run_id`, `started_at`, trading environment, and the complete symbol list.
2. Search each symbol by identifier and unambiguous name using only KIS news/disclosure-related APIs.
3. Record market-wide KIS news separately from symbol-specific KIS news when useful.
4. Deduplicate substantially identical KIS items while preserving distinct KIS identifiers when available.
5. Summarize each item factually and briefly. Do not include full article text.
6. A completed KIS search with no relevant stories is valid; write a short no-data note.
7. Return Markdown text only. Do not emit JSON, code fences, trailing comments, or long raw payloads.

## Required Output

Use this compact shape:

```text
# News Context

- status: success | partial | no-data | failed
- generated_at: <Asia/Seoul ISO-8601 if known>

## Market-Wide
- <up to 5 short KIS news/disclosure bullets, or "no relevant KIS items">

## <symbol_id> <symbol_name>
- <title or KIS id> | <published_at if known> | <publisher/source>: <short factual summary>
- risk/opportunity tags: <short comma-separated tags, if useful>
- missing/errors: <concise non-sensitive note, if any>
```

If the complete news context risks becoming too large, prefer the most recent and directly symbol-linked KIS items. A compact partial summary is more useful than a malformed JSON response.
