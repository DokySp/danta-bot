---
name: collect-financial-information
description: "Collect compact Korean stock and ETF financial context from KIS and official sources only. Use for the financial collection agent in daily-trading, or when a user explicitly requests official financial context without news, account, balance, or order operations."
---

# Collect Financial Information

## Scope

Collect financial context for the supplied symbol list in one pass. Return compact Markdown text only. Do not return a JSON envelope, and do not write files; the caller owns artifact persistence.

Allowed sources:

- KIS market, financial, and estimate APIs
- DART, KRX, Korean government or regulator publications
- Issuer-operated investor-relations pages and official filings

Do not use blogs, social media, community posts, unofficial aggregators, or unsourced summaries.

## Permissions

- External calls: allowed only for the sources above.
- KIS calls: direct calls only, with bounded backoff for retryable KIS failures.
- Account, balance, order-available, fill-history, pending-order, reservation-order, and order APIs: forbidden.
- File writes: forbidden.
- Order submission, reservation submission, correction, cancellation, and order revision: forbidden.
- Secrets such as account numbers, tokens, app keys, app secrets, and HTS IDs: never request or return.

## KIS Backoff

- Use existing validated parameter templates for known KIS API calls.
- Call `find_api_detail` only when no validated template exists, a new API type is introduced, or KIS rejects the template.
- For retryable KIS/MCP API error codes or messages, including rate-limit, temporary gateway/routing, transport, and timeout failures, retry the same API with the same parameters using exponential backoff up to 10 retries after the initial call.
- Recommended delay sequence is 1, 2, 4, 8, 16, then 30 seconds capped for remaining retries. Add small jitter when the runtime supports it.
- Preserve attempt summaries in the Markdown only when they materially change confidence. Do not include sensitive parameters.
- Authentication, token, credential, and permission errors are not local backoff targets. Return a concise note to the daily-trading Main agent; do not call `auth_token`.

## Workflow

1. Accept `run_id`, `started_at`, trading environment, and the requested symbol list.
2. For every symbol, attempt the applicable KIS financial search and official-source search.
3. For stocks, summarize available valuation, earnings, profitability, leverage, dividend, estimate, and filing context.
4. For ETFs/ETNs, summarize issuer facts and official product data that are financial in nature. Mark stock-only fields as not applicable only when useful.
5. Keep source names, observation dates, and raw KIS field names only when they clarify the summary.
6. Missing financial data is not a trading blocker. Record it briefly as missing or no usable context.
7. Return Markdown text only. Do not emit JSON, code fences, trailing comments, or long raw payloads.

## Required Output

Use this compact shape:

```text
# Financial Context

- status: success | partial | no-data | failed
- generated_at: <Asia/Seoul ISO-8601 if known>

## <symbol_id> <symbol_name>
- valuation: <one short factual bullet or "not found">
- earnings: <one short factual bullet or "not found">
- balance/profitability: <one short factual bullet or "not found">
- dividend/estimate: <one short factual bullet or "not found">
- sources: <up to 3 source names with observation dates>
- missing/errors: <concise non-sensitive note, if any>
```

If the complete financial context risks becoming too large, prefer shorter Markdown over richer detail. A compact partial summary is more useful than a malformed JSON response.
