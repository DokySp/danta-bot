# Target Quantity And Trade Execution Rules

## Scope

The second verdict decides portfolio target quantities. Only the main agent converts those targets into orders and calls account or order APIs.

- Analysis-only request: calculate and report targets, but do not call order APIs. Write skipped `account-before-order.json` and `execution.json`.
- Preparation or review request: refresh account state and create order tickets, but do not submit.
- Explicit demo or real execution request: refresh account state, validate every gate, and submit allowed orders.
- Explicit reservation request: use the reservation-order path.

`CODEX_MCP_TRADING_ENV` overrides conflicting environment wording: `paper` maps to `demo`; `acct` maps to `real`.

## Main-Agent API Boundary

Collection and verdict sub-agents cannot call any API in this section.

Before order calculation, the main agent sequentially refreshes:

- `inquire_account_balance`
- `inquire_balance`
- `inquire_daily_ccld`
- pending-order lookup supported by the current KIS API
- `order_resv_ccnl` when supported
- `inquire_psbl_order` for buy candidates
- `inquire_psbl_sell` for sell candidates

Inspect current parameters with `find_api_detail`. Do not provide account number, account product code, or HTS ID because the MCP wrapper supplies them. Do not run ledger APIs in parallel.

## Quantity Calculation

For each eligible second-verdict symbol:

```text
expected_holding_quantity =
  current_live_holding_quantity
  + pending_and_reserved_buy_quantity
  - pending_and_reserved_sell_quantity

additional_required_quantity =
  target_holding_quantity
  - expected_holding_quantity
```

- Positive `additional_required_quantity`: buy candidate.
- Negative `additional_required_quantity`: sell candidate using the absolute value.
- Zero: no order.
- Current live holdings already include same-day fills. Never subtract same-day filled quantity again.
- Pending and reserved quantities include only active, non-cancelled quantities.

## Same-Day Fill Guard

Same-day fills prevent repeated trading; they do not change the quantity formula.

- Record same-day buy and sell fills per symbol in both account snapshots.
- If a new order has the same direction as a same-day fill, submit it only when `verdict-second.json` explicitly shows that the reconciled target still requires the remaining delta after considering that fill.
- If that explicit justification is absent, set the order result to `blocked` with reason `same_day_repeat_guard`.
- Never reverse a same-day fill solely because another judge used a different horizon.

## Cash Decision

- `verdict-second.json` supplies the reconciled target cash amount.
- There is no fixed minimum cash ratio, maximum cash ratio, or fixed investment ratio.
- Reconciled target quantities plus target cash must not exceed the initial account total assets. Any unexplained remainder is target cash, not an automatic buy budget.
- Buy orders cannot exceed the latest order-available amount after active pending/reserved buys and user limits.
- Expected proceeds from unfilled sells are not available buy cash.
- If latest account cash is lower than the target or constraints require more cash, reduce or block buys; do not silently increase sells beyond target quantities.

## Order Constraints

Every order must satisfy all applicable constraints:

- symbol remains eligible and has a valid reconciled target
- latest account snapshot succeeded
- direction and quantity match the target delta
- buy quantity does not exceed `inquire_psbl_order`
- sell quantity does not exceed current holdings or `inquire_psbl_sell`
- active pending/reserved quantities were included exactly once
- same-day fill guard passed
- user maximum amount, maximum quantity, prohibited symbols, and price limits passed
- order price and order type were validated using current API details
- real submission was explicitly requested

If any gate fails, do not submit that order. Record `blocked` and the exact non-sensitive reason.

## Price And Order Type

- User-specified valid price and order type take priority.
- Explicit reservation requests use `order_resv`.
- Outside market hours, use `order_resv` when supported; if unsupported, create a ticket and record `blocked`.
- Explicit intraday immediate execution may use `order_cash`.
- If price or order type remains ambiguous after current API-detail inspection, block the order.

## Submission Order

1. Validate all candidates against the same latest account snapshot.
2. Process sells before buys.
3. Do not count unfilled sell proceeds as buy cash.
4. Submit orders sequentially with the KIS-required interval.
5. A failed or blocked order does not transfer its quantity or budget to another symbol in the same run.
6. After submissions, refresh available order/fill state sequentially and record it in `execution.json`.

## Execution JSON Fields

`execution.json` uses the common envelope from `run-artifacts.md` and contains:

```json
{
  "request_type": "analysis | prepare | demo-submit | real-submit",
  "target_cash_amount": 0,
  "latest_available_cash": 0,
  "orders": [
    {
      "symbol_id": "",
      "symbol_name": "",
      "direction": "buy | sell | none",
      "current_live_holding_quantity": 0,
      "pending_and_reserved_buy_quantity": 0,
      "pending_and_reserved_sell_quantity": 0,
      "same_day_buy_filled_quantity": 0,
      "same_day_sell_filled_quantity": 0,
      "expected_holding_quantity": 0,
      "target_holding_quantity": 0,
      "additional_required_quantity": 0,
      "validated_order_quantity": 0,
      "order_price": 0,
      "order_type": "",
      "result": "submitted | skipped | blocked | failed",
      "reason": "",
      "order_or_reservation_id": ""
    }
  ],
  "post_order_state": {}
}
```

Never store account numbers, tokens, app keys, app secrets, HTS IDs, or raw authentication headers.
