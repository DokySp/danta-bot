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
- Use `$gate-kis-calls` before every KIS call when that skill is available.
- Do not provide account number, account product code, or HTS ID; the MCP wrapper supplies them.
- Do not call order submission, reservation submission, correction, cancellation, or order-revision APIs.
- Do not write files, submit orders, calculate final target quantities, or make trading decisions.
- Do not return account numbers, account product codes, access tokens, app keys, app secrets, HTS IDs, raw auth headers, or credential-like values.

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
  "stage": "account-before-verdict | account-before-order",
  "snapshot_type": "account-before-verdict | account-before-order",
  "status": "success | partial | failed",
  "skipped": false,
  "skip_reason": "",
  "environment": "demo | real",
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
