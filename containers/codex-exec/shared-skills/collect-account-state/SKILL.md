---
name: collect-account-state
description: "Collect read-only Korean account state for daily-trading account-before-verdict and account-before-order snapshots. Use only for account summary, holdings, fills, pending/reserved orders, buy-available, and sell-available checks; never for order submission, correction, or cancellation."
---

# Collect Account State

## Scope

Collect a sanitized, read-only account snapshot for the supplied daily-trading run. Return one JSON object to the caller. Do not write files; the daily-trading `Main agent` owns artifact persistence and sanitization verification.

Supported snapshot types:

- `account-before-verdict`: account summary, current holdings, pending orders, reserved orders, and same-day fills.
- `account-before-order`: every field above plus buy-available checks for buy candidates and sell-available checks for sell candidates.
- `post-order-state`: narrow post-submission verification. For reservation orders, check reservation-order state only; for intraday orders, check same-day order/fill and pending-order state only.

## Permissions

Allowed read-only KIS account APIs:

- account asset summary
- stock balance
- same-day order/fill history
- pending or cancellable-order lookup
- reservation-order lookup
- buy-available amount or quantity lookup
- sell-available quantity lookup

Rules:

- Use only read-only inquiry APIs. Inspect current parameters with `find_api_detail` before the first use of each API type.
- Reuse validated parameter templates within the same run. Do not probe known-invalid values for verification. For `inquire_account_balance`, use `inqr_dvsn_1="1"` and omit `env_dv` unless the current API detail explicitly supports it.
- Call KIS directly. For retryable rate-limit, gateway, transport, and timeout failures, use bounded backoff before marking a lookup failed.
- Do not provide account number, account product code, or HTS ID; the MCP wrapper supplies them.
- Do not call order submission, reservation submission, correction, cancellation, or order-revision APIs.
- Do not write files, submit orders, calculate final target quantities, or make trading decisions.
- Do not return account numbers, account product codes, access tokens, app keys, app secrets, HTS IDs, raw auth headers, or credential-like values.

## KIS Backoff

- For retryable KIS/MCP API error codes or messages, including rate-limit, temporary gateway/routing, transport, and timeout failures, retry the same API with the same parameters using exponential backoff up to 10 retries after the initial call.
- Recommended delay sequence is 1, 2, 4, 8, 16, then 30 seconds capped for remaining retries. Add small jitter when the runtime supports it.
- Preserve every attempt in `attempts`, including API name, non-sensitive parameters, error code/message, delay, and final outcome.
- Authentication, token, credential, and permission errors are not local backoff targets. Return those errors to the daily-trading Main agent; do not call `auth_token`.

## Failure Contract

If any required account lookup fails, return a `failed` or `partial` envelope to the Main agent. The Main agent must block order preparation and execution when a required `account-before-verdict` or `account-before-order` lookup fails.

Do not hide partial data. Preserve successful read-only results, non-sensitive API names, non-sensitive error codes, and non-sensitive messages.

## Required Output

```json
{
  "schema_version": "1",
  "run_id": "<run_id>",
  "started_at": "<Asia/Seoul ISO-8601>",
  "generated_at": "",
  "stage": "account-before-verdict | account-before-order | post-order-state",
  "snapshot_type": "account-before-verdict | account-before-order | post-order-state",
  "status": "success | partial | failed",
  "skipped": false,
  "skip_reason": "",
  "environment": "demo | real",
  "attempts": [],
  "account_summary": {
    "total_assets": null,
    "cash": null,
    "buy_available_cash": null,
    "currency": "KRW"
  },
  "holdings": [
    {
      "symbol_id": "",
      "symbol_name": "",
      "product_type": "stock | etf | etn | other | unresolved",
      "current_live_holding_quantity": 0,
      "average_price": null,
      "market_value": null,
      "unrealized_profit_loss": null
    }
  ],
  "same_day_fills": [
    {
      "symbol_id": "",
      "symbol_name": "",
      "direction": "buy | sell",
      "filled_quantity": 0,
      "filled_amount": null,
      "filled_at": ""
    }
  ],
  "pending_orders": [
    {
      "symbol_id": "",
      "symbol_name": "",
      "direction": "buy | sell",
      "remaining_quantity": 0,
      "order_type": "",
      "status": ""
    }
  ],
  "reserved_orders": [
    {
      "symbol_id": "",
      "symbol_name": "",
      "direction": "buy | sell",
      "reserved_quantity": 0,
      "order_type": "",
      "status": ""
    }
  ],
  "order_availability": [
    {
      "symbol_id": "",
      "symbol_name": "",
      "buy_available_quantity": null,
      "buy_available_amount": null,
      "sell_available_quantity": null,
      "errors": []
    }
  ],
  "errors": []
}
```

For `account-before-verdict`, `order_availability` may be an empty list. For `account-before-order`, include availability rows for every candidate that could become an order.

For `post-order-state`, return the same JSON shape with `snapshot_type="post-order-state"` and `stage="post-order-state"`. Keep unrelated arrays empty unless they were required for the narrow verification. Do not repeat account summary, full balance, buy-available, or sell-available lookups after accepted reservation submissions when a reservation lookup confirms the submitted reservations.
