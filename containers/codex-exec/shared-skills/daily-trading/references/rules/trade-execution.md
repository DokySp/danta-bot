# Target Quantity And Trade Execution Rules

## Scope

`second-verdict` decides portfolio target quantities. Deterministic helpers convert those targets into order candidates, and `scripts/execute_orders.py` refreshes read-only order gates and calls actual immediate or reservation order APIs inside the single `order-execution` stage when explicitly authorized.

- Analysis-only request: calculate and report targets, but do not call order APIs. Write skipped `account-before-order.json` and `execution.json`.
- Preparation or review request: refresh account state and create order tickets, but do not submit.
- Explicit demo or real execution request: refresh account state, reconcile active pending/reserved orders, validate every gate, and submit allowed orders.
- Explicit reservation request: use the reservation-order path.
- Explicit immediate request: use the immediate cash-order path.

The execution rules in this file apply to `order_cash` and `order_resv`, and to both demo and real environments, unless a rule explicitly names a narrower API constraint.

`CODEX_MCP_TRADING_ENV` overrides conflicting environment wording: `paper` maps to `demo`; `acct` maps to `real`.

## Account Snapshot Boundary

Collection, `first-verdict`, and `second-verdict` sub-agents cannot call any account or order API.

Before order calculation, `scripts/execute_orders.py` refreshes only the read-only account fields required to validate current candidates. The first balance snapshot may be produced by `scripts/collect_main_evidence.py`; any missing active-order or order-available fields must be refreshed before candidate calculation:

- `inquire_account_balance`
- `inquire_balance`
- `inquire_daily_ccld`
- pending-order lookup supported by the current KIS API
- `order_resv_ccnl` when supported
- `inquire_psbl_order` for buy candidates
- `inquire_psbl_sell` plus current live holdings minus active sell reservations for sell candidates

The order runner must use validated parameter templates for known read-only account APIs, must not expose account number, account product code, or HTS ID in artifacts, prompts, reports, or user responses, and must not run ledger APIs in parallel. Direct helpers may read account configuration from the runtime environment, but must sanitize the result before writing `account-before-order.json`.

Call `find_api_detail` only when no validated template exists, when introducing a new MCP API type, or after an MCP API rejects a template for parameter/schema reasons. In particular, MCP `inquire_account_balance` uses `inqr_dvsn_1="1"` and does not include `env_dv` unless the currently inspected API detail supports it. If a parameter template was already validated earlier in the same run, reuse it and record `template_reused=true` in the attempt entry rather than repeating trial calls.

If `account-before-order.json` is missing, invalid, `failed`, or shows `active_order_lookup_performed=false` or `order_available_lookup_performed=false` for a requested order path, `scripts/execute_orders.py` must refresh those read-only fields before submission or block the run.

## Order Runner API Boundary

Only `scripts/execute_orders.py` may call actual order APIs after explicit authorization and every gate has passed.

Allowed only when explicitly authorized and validated:

- `order_resv`
- `order_cash`
- `order_rvsecncl`
- `order_resv_rvsecncl`
- active order keep/cancel/correct/replace decisions when all required KIS fields are available.

Forbidden to every sub-agent:

- order submission
- reservation submission
- correction
- cancellation
- order revision

## Active Order Reconciliation

Before submitting any new order, `scripts/execute_orders.py` must compare active, non-cancelled pending/reserved orders from `account-before-order.json` with the validated `target_holding_quantity` from the single `judge-midterm` result. Use the `active_orders` fields defined in `run-artifacts.md`; if any required active-order field is missing or ambiguous, block cancellation, correction, replacement, and conflicting new submission for that symbol.

For each symbol:

1. Keep an existing active pending/reserved order only when its symbol, direction, remaining quantity, price, execution environment, order API, and reservation/immediate path already match the desired candidate.
2. If an active order is same-symbol but its direction, remaining quantity, price, order API, or order path no longer matches the desired candidate, correct it when direction/API/path match and required original-order identifiers are present; otherwise cancel it before submitting a replacement.
3. If the target delta is zero but an active order remains, keep it only when it already matches the target delta; otherwise cancel it when required original-order identifiers are present.
4. If more than one active order exists for the same symbol and direction, block replacement if a safe one-order target cannot be proven.
5. Do not use proceeds from cancelled or still-unfilled sell orders as buy cash until the latest read-only account state proves the cash is available.

Cancellation, correction, and replacement require the same explicit demo/real execution authorization as new submissions. They also require a current API detail or previously validated template for the exact KIS function. If no supported cancellation/correction API can be identified, do not submit a conflicting replacement; record `blocked` with reason `active_order_adjustment_unavailable`.

After every accepted cancellation or correction, refresh only the minimum read-only order/fill/reservation state needed to prove the active order state before calculating replacement quantity. Never assume cancellation succeeded from a submitted request alone.

## Order API Backoff

The order runner calls order APIs directly. For retryable KIS API error codes or messages, including rate-limit and temporary gateway/routing errors that clearly indicate the order was not accepted, and transport failures that happened before request submission, retry the same validated order with the same parameters using bounded backoff.

Recommended delay sequence is 1, 2, 4, 8, 16, then 30 seconds capped for remaining retries. Record every order and active-order adjustment attempt in `execution.json` with non-sensitive API name, symbol, direction, quantity, error code/message, delay, and final outcome.

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

## Same-Day Fills

Same-day fills are account evidence only; they do not change the quantity formula.

- Record same-day buy and sell fills per symbol in both account snapshots.
- Current live holdings already include same-day fills, so never subtract same-day filled quantity again.

## Cash Availability

- `verdict-second.json` supplies target quantities only. It does not supply a target cash amount, cash ratio, or cash judgment code.
- Validated target quantities must not exceed the initial account total assets.
- If validated target quantities are below assets, the remainder is residual cash, not an automatic buy budget and not a reported target.
- Buy orders must be reduced to the latest order-available quantity or remaining cash amount after active pending/reserved buys. If the reduced quantity is zero, block the order.
- Expected proceeds from unfilled sells are not available buy cash.
- If buy candidates exceed available cash or account constraints, reduce buys to the executable quantity in reverse relative-attractiveness order or block them when the executable quantity is zero; do not silently increase sells beyond target quantities. Record the requested and adjusted quantities.

## Order Constraints

Every order must satisfy all applicable constraints:

- symbol remains eligible and has a valid target
- latest account snapshot succeeded
- direction and quantity match the target delta
- buy quantity has been reduced to not exceed `inquire_psbl_order` and remaining cash
- sell quantity has been reduced to not exceed current live holdings minus active sell reservations or the latest `inquire_psbl_sell` result
- active pending/reserved quantities were included exactly once
- conflicting active pending/reserved orders were kept, cancelled, corrected, or blocked according to Active Order Reconciliation
- order price, order API, and reservation/immediate path were validated using known templates or current API details when templates were unavailable/rejected
- missing financial/news evidence is allowed when the symbol remains eligible and has a valid price observation
- demo or real submission was explicitly requested

If any non-quantity gate fails, do not submit that order. Record `blocked` and the exact non-sensitive reason. If a quantity gate leaves a positive executable quantity, submit only the reduced quantity and record `requested_order_quantity` plus `quantity_adjustment`; if the executable quantity is zero, block the order. If an active-order adjustment fails or remains uncertain, do not submit a replacement order for that symbol in the same run.

Missing, partial, failed, skipped, or no-data financial/news evidence is not an execution gate by itself. The order runner must not block, fail, reduce, or send an order to review solely because financial/news evidence is absent. This applies to `order_cash`, `order_resv`, demo submission, and real submission.

## Price And Order Path

- User-specified valid price and order path take priority.
- Explicit reservation requests use `order_resv`.
- Explicit intraday immediate requests use `order_cash`.
- When the user or schedule explicitly requests real/demo limit reservation trading, the reservation path and `order_resv` API are explicit. If the user did not provide per-symbol limit prices, use the deterministic `execution-plan` `order_price` derived from the latest sanitized account price or `decision-brief.json` price as the default limit price candidate. Do not block solely because that candidate price was generated by the pipeline rather than typed again by the user.
- Block a candidate when `order_price` is missing, zero or negative, stale/unsupported by the current order API detail, or inconsistent with the latest refreshed order-available response.
- If price, order API, or reservation/immediate path remains ambiguous after current API-detail inspection, block the order.

## Submission Order

1. Validate all candidates against the same latest account snapshot.
2. Process sells before buys.
3. Do not count unfilled sell proceeds as buy cash.
4. Submit orders sequentially and apply the order API backoff rules at each call site.
5. A failed or blocked order does not transfer its quantity or budget to another symbol in the same run.
6. Treat accepted submission responses as the execution result without narrating routine verification lookups. If a submission result is uncertain because of timeout or transport ambiguity, use the minimum read-only lookup needed to prove whether that specific order was accepted before retrying or marking it failed.

## Execution JSON Fields

`execution.json` uses the common envelope from `run-artifacts.md` and contains:

```json
{
  "symbols": [],
  "request_type": "analysis | prepare | demo-submit | real-submit",
  "latest_available_cash": 0,
  "order_adjustments": [
    {
      "symbol_id": "",
      "symbol_name": "",
      "existing_order_id": "",
      "existing_order_kind": "pending | reservation",
      "existing_direction": "buy | sell",
      "existing_remaining_quantity": 0,
      "existing_order_price": 0,
      "existing_order_api": "order_cash | order_resv",
      "existing_execution_environment": "demo | real",
      "existing_order_path": "immediate | reservation",
      "existing_active_status": "active | cancelled | filled | expired | unknown",
      "action": "keep | cancel | correct | replace | block",
      "reason": "",
      "result": "submitted | skipped | blocked | failed",
      "adjustment_api_name": "",
      "adjustment_request_id": "",
      "confirmation_status": "confirmed | unconfirmed | not_required",
      "confirmed_at": "",
      "confirmation_artifact": "account-before-order.json",
      "replacement_required": false,
      "replacement_order_id": "",
      "attempts": [
        {
          "api_name": "",
          "attempt": 1,
          "delay_seconds": 0,
          "error_code": "",
          "message": "",
          "result": "submitted | skipped | blocked | failed"
        }
      ]
    }
  ],
  "orders": [
    {
      "symbol_id": "",
      "symbol_name": "",
      "direction": "buy | sell | none",
      "current_live_holding_quantity": 0,
      "pending_and_reserved_buy_quantity": 0,
      "pending_and_reserved_sell_quantity": 0,
      "expected_holding_quantity": 0,
      "target_holding_quantity": 0,
      "additional_required_quantity": 0,
      "validated_order_quantity": 0,
      "order_price": 0,
      "order_path": "immediate | reservation",
      "order_api": "order_cash | order_resv",
      "result": "submitted | skipped | blocked | failed",
      "reason": "",
      "order_or_reservation_id": "",
      "attempts": [
        {
          "api_name": "",
          "attempt": 1,
          "delay_seconds": 0,
          "error_code": "",
          "message": "",
          "result": "submitted | skipped | blocked | failed"
        }
      ]
    }
  ]
}
```

Never store account numbers, tokens, app keys, app secrets, HTS IDs, or raw authentication headers.
