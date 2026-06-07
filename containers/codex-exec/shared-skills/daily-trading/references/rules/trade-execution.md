# Target Quantity And Trade Execution Rules

## Scope

`second-verdict` decides portfolio target quantities. Only the Main agent converts those targets into order candidates and calls actual order submission APIs. Read-only account state is collected through `$collect-account-state`.

- Analysis-only request: calculate and report targets, but do not call order APIs. Write skipped `account-before-order.json` and `execution.json`.
- Preparation or review request: refresh account state and create order tickets, but do not submit.
- Explicit demo or real execution request: refresh account state, validate every gate, and submit allowed orders.
- Explicit reservation request: use the reservation-order path.

`CODEX_MCP_TRADING_ENV` overrides conflicting environment wording: `paper` maps to `demo`; `acct` maps to `real`.

## Account Snapshot Boundary

Collection, `first-verdict`, `second-verdict`, and `final-risk-verdict` sub-agents cannot call any account or order API.

Before order calculation, the Main agent requests `$collect-account-state` with `snapshot_type="account-before-order"` to refresh:

- `inquire_account_balance`
- `inquire_balance`
- `inquire_daily_ccld`
- pending-order lookup supported by the current KIS API
- `order_resv_ccnl` when supported
- `inquire_psbl_order` for buy candidates
- `inquire_psbl_sell` for sell candidates

The account sub-agent must inspect current parameters with `find_api_detail`, must not provide account number, account product code, or HTS ID because the MCP wrapper supplies them, and must not run ledger APIs in parallel. It returns JSON only. The Main agent sanitizes and writes `account-before-order.json`.

If `account-before-order.json` is missing, invalid, or `failed`, the Main agent must block order candidate calculation, `final-risk-verdict`, and submission.

## Main-Agent Order API Boundary

Only the Main agent may call actual order APIs after explicit authorization and every gate has passed.

Allowed only when explicitly authorized and validated:

- `order_cash`
- `order_resv`

Forbidden to every sub-agent:

- order submission
- reservation submission
- correction
- cancellation
- order revision

## Quantity Calculation

For each eligible `second-verdict` symbol:

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
- `final-risk-verdict` result is `approved`
- real submission was explicitly requested

If any gate fails, do not submit that order. Record `blocked` and the exact non-sensitive reason.

## `final-risk-verdict` Gate

After order candidates are created and before any submission, the Main agent spawns the `final-risk-verdict` sub-agent.

Inputs:

- `decision-brief.json`
- `verdict-second.json`
- sanitized `account-before-order.json`
- order candidates

The `final-risk-verdict` sub-agent can return only `approved`, `blocked`, or `needs_review`.

- `approved`: the Main agent may continue to explicit-authorization and order API gates.
- `blocked`: the Main agent records blocked execution and submits nothing.
- `needs_review`: the Main agent records review-needed execution and submits nothing.
- missing, invalid, or `failed`: the Main agent records blocked execution and submits nothing.

The `final-risk-verdict` sub-agent must not recalculate target quantities, change order candidates, replace judge output, call APIs, or write files.

## Price And Order Type

- User-specified valid price and order type take priority.
- Explicit reservation requests use `order_resv`.
- Outside market hours, use `order_resv` when supported; if unsupported, create a ticket and record `blocked`.
- Explicit intraday immediate execution may use `order_cash`.
- If price or order type remains ambiguous after current API-detail inspection, block the order.

## Submission Order

1. Validate all candidates against the same latest account snapshot.
2. Obtain an `approved` result in `final-order-verdict.json`.
3. Process sells before buys.
4. Do not count unfilled sell proceeds as buy cash.
5. Submit orders sequentially with the KIS-required interval.
6. A failed or blocked order does not transfer its quantity or budget to another symbol in the same run.
7. After submissions, refresh available order/fill state through `$collect-account-state` and record it in `execution.json`.

## Execution JSON Fields

`execution.json` uses the common envelope from `run-artifacts.md` and contains:

```json
{
  "request_type": "analysis | prepare | demo-submit | real-submit",
  "final_order_verdict_result": "approved | blocked | needs_review | skipped",
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
