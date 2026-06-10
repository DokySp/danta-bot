# Target Quantity And Trade Execution Rules

## Scope

`second-verdict` decides portfolio target quantities. Only the Main agent converts those targets into order candidates, collects read-only account state, and calls actual order submission APIs inside the single `order-execution` stage.

- Analysis-only request: calculate and report targets, but do not call order APIs. Write skipped `account-before-order.json` and `execution.json`.
- Preparation or review request: refresh account state and create order tickets, but do not submit.
- Explicit demo or real execution request: refresh account state, validate every gate, and submit allowed orders.
- Explicit reservation request: use the reservation-order path.

The execution rules in this file apply uniformly to `order_cash` and `order_resv`, and to both demo and real environments, unless a rule explicitly names a narrower API constraint.

`CODEX_MCP_TRADING_ENV` overrides conflicting environment wording: `paper` maps to `demo`; `acct` maps to `real`.

## Account Snapshot Boundary

Collection, `first-verdict`, and `second-verdict` sub-agents cannot call any account or order API.

Before order calculation, the Main agent refreshes only the read-only account fields required to validate current candidates:

- `inquire_account_balance`
- `inquire_balance`
- `inquire_daily_ccld`
- pending-order lookup supported by the current KIS API
- `order_resv_ccnl` when supported
- `inquire_psbl_order` for buy candidates
- `inquire_psbl_sell` for sell candidates

The Main agent must use validated parameter templates for known read-only account APIs, must not provide account number, account product code, or HTS ID because the MCP wrapper supplies them, and must not run ledger APIs in parallel. It sanitizes the result and writes `account-before-order.json`.

Call `find_api_detail` only when no validated template exists, when introducing a new API type, or after the API rejects a template for parameter/schema reasons. In particular, `inquire_account_balance` uses `inqr_dvsn_1="1"` and does not include `env_dv` unless the currently inspected API detail supports it. If a parameter template was already validated earlier in the same run, reuse it and record `template_reused=true` in the attempt entry rather than repeating trial calls.

If `account-before-order.json` is missing, invalid, or `failed`, the Main agent must block order candidate calculation and submission.

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

## Order API Backoff

The Main agent calls order APIs directly. For retryable KIS/MCP API error codes or messages, including rate-limit and temporary gateway/routing errors that clearly indicate the order was not accepted, and transport failures that happened before request submission, retry the same validated order with the same parameters using exponential backoff up to 10 retries after the initial call.

Recommended delay sequence is 1, 2, 4, 8, 16, then 30 seconds capped for remaining retries. Record every order attempt in `execution.json` with non-sensitive API name, symbol, direction, quantity, error code/message, delay, and final outcome.

For timeout, transaction-timeout, or any uncertain order result, do not blindly retry. First refresh order/fill/reservation state through read-only lookup, confirm whether the order or reservation already exists, and retry only if the latest state proves that no order was accepted.

Authentication, token, credential, and permission errors are not order backoff targets. Use `auth-token.md` for the single central token reissue retry, and block the order if that retry fails.

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

`expected_holding_quantity` is the pre-candidate expected holding quantity after already-active pending and reserved quantities are considered. It is not the post-submission target quantity, and it must not be treated as invalid merely because it differs from `target_holding_quantity`.

Candidate consistency is checked with the delta formula above:

```text
target_holding_quantity = expected_holding_quantity + additional_required_quantity
validated_order_quantity = abs(additional_required_quantity) for buy/sell candidates
```

If a post-submission quantity is needed for audit text, derive it separately as `post_order_expected_holding_quantity`. Do not overload or rewrite `expected_holding_quantity` for that purpose.

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
- order price and order type were validated using known templates or current API details when templates were unavailable/rejected
- missing financial/news evidence is allowed when the symbol remains eligible and has a valid price observation
- demo or real submission was explicitly requested

If any gate fails, do not submit that order. Record `blocked` and the exact non-sensitive reason.

Missing, partial, failed, skipped, or no-data financial/news evidence is not an execution gate by itself. The Main agent must not block, fail, reduce, or send an order to review solely because financial/news evidence is absent. This applies equally to `order_cash`, `order_resv`, demo submission, and real submission.

## Price And Order Type

- User-specified valid price and order type take priority.
- Explicit reservation requests use `order_resv`.
- Explicit intraday immediate execution may use `order_cash`.
- If price or order type remains ambiguous after current API-detail inspection, block the order.

## Submission Order

1. Validate all candidates against the same latest account snapshot.
2. Process sells before buys.
3. Do not count unfilled sell proceeds as buy cash.
4. Submit orders sequentially and apply the order API backoff rules at each call site.
5. A failed or blocked order does not transfer its quantity or budget to another symbol in the same run.
6. Do not run routine verification lookups after accepted submissions. If a submission result is uncertain because of timeout or transport ambiguity, use the minimum read-only lookup needed to prove whether that specific order was accepted before retrying or marking it failed.

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
  ]
}
```

Never store account numbers, tokens, app keys, app secrets, HTS IDs, or raw authentication headers.
