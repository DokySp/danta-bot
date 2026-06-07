---
name: daily-trading
description: "[v20260606-01] 전체 한국 주식·ETF 포트폴리오의 시장·재무·뉴스 정보를 한 번 수집해 재사용하고, 외부 호출 없는 1차 종목 평결과 2차 포트폴리오 평결을 거쳐 목표 보유수량과 주문 목록을 산출한다. Use for KIS MCP based portfolio analysis, daily-* and pre-open trading schedules, sub-agent collection and verdict orchestration, portfolio reports, demo orders, real orders, and reservation orders."
---

# Daily Trading Portfolio Orchestrator

## Required References

Read only the files needed for the current stage.

- Collection and permissions: `references/rules/data-collection.md`
- Authentication: `references/rules/auth-token.md`
- Strategy signals: `references/rules/strategy-mapping.md`
- Run artifacts and JSON schemas: `references/rules/run-artifacts.md`
- Verdict output: `references/rules/verdict-format.md`
- Portfolio report: `references/rules/report-template.md`
- Target quantity and execution: `references/rules/trade-execution.md`
- First-verdict personas: `references/personas/analyst-*.md`, `references/personas/juror-*.md`
- Second-verdict personas: `references/personas/judge-*.md`

## Run Identity And Telegram Start Time

The Codex execution prompt normally injects `run_id` and `started_at`.

1. Preserve the injected values unchanged for the complete run.
2. If either value is absent, generate both immediately before any daily-trading work:
   - `run_id`: filesystem-safe unique value
   - `started_at`: current Asia/Seoul time with timezone
3. Immediately create `reports/runs/<run_id>/run.json` using `run-artifacts.md`.
4. Use `reports/runs/<run_id>/` for every intermediate JSON file.
5. When this skill is actually used, include the following line in the final response on both success and failure:

   ```text
   작업 시작: YYYY-MM-DD HH:MM:SS KST
   ```

6. Do not show that line for work that did not use this skill.

These rules apply to direct and indirect use, including `daily-*`, `pre-open`, `$execute-trade`, and direct or indirect `$daily-trading` execution.

## Authority Boundaries

### Main Agent Only

- Create and update files under `reports/`.
- Determine the complete portfolio universe.
- Perform KIS authentication preflight and token reissue.
- Call account, balance, order-available, fill-history, pending-order, reservation-order, and order APIs.
- Merge collection output, decide exclusions, calculate final target quantities, create final orders, and submit explicitly authorized orders.
- Remove sensitive fields before writing artifacts or sending sub-agent input.

### Collection Sub-Agents

- Three collection agents run in parallel: `market`, `financial`, and `news`.
- They receive the complete portfolio universe and collect detailed data for every symbol in their domain.
- External calls are allowed.
- Every KIS call must use `$gate-kis-calls`.
- `financial` must use `$collect-financial-information`.
- `news` must use `$collect-news-information`.
- Account, balance, order, order-available, fill-history, pending-order, and reservation-order APIs are forbidden.
- They return JSON to the main agent and do not write files.

### Verdict Sub-Agents

- First-verdict and second-verdict agents cannot call KIS, web, MCP, network, shell, or any external data source.
- They cannot write files or submit orders.
- They may use only the exact snapshots supplied by the main agent.
- Missing data stays missing. Recollection, substitution from another symbol, and guessing are forbidden.

## Execution Flow

### 1. Initialize

1. Resolve input symbols from the user request and, when requested, `$check-portfolio`.
2. Perform authentication preflight using `auth-token.md`.
3. The main agent captures a sanitized initial account snapshot to identify current holdings and existing orders.
4. The complete portfolio universe is the union of requested/configured symbols and current holdings. Resolve names and identifiers without dropping unresolved inputs.
5. Write `account-before-verdict.json`. Account APIs are never delegated.
6. If the initial account snapshot fails, continue collection and analysis only for requested/configured symbols, mark current holdings as unknown, and block all order preparation and submission.

### 2. Collect Once

1. Spawn the `market`, `financial`, and `news` collection agents in parallel.
2. Give each agent the same complete symbol list, `run_id`, `started_at`, environment, required schema, and permission boundary.
3. Each agent performs one complete domain collection pass for all symbols.
4. Write each returned payload immediately and exactly once:
   - `market.json`
   - `financial.json`
   - `news.json`
5. If an agent returns no valid JSON, the main agent writes a `failed` domain envelope containing every symbol and the agent-level error.
6. If an agent or symbol partially fails, preserve its successful data and errors. Do not discard or overwrite an already-created snapshot.
7. Do not recollect between first and second verdicts. Both verdict stages reuse these snapshots.

The market collector gathers KIS price, chart, order-book/trade, flow, rank, industry, and ETF/NAV data. The financial and news collectors follow their own skills.

### 3. Merge And Exclude

1. Merge domain snapshots and the sanitized initial account snapshot into `merged.json`.
2. Record source provenance and per-symbol errors.
3. A symbol with insufficient required information receives `eligible_for_verdict=false`.
4. An ineligible symbol is excluded from both verdict stages, target-quantity calculation, and trading. Keep it in artifacts and the report with explicit exclusion reasons.
5. Domain or run status uses only `success`, `partial`, or `failed` as defined in `run-artifacts.md`.

### 4. First Verdict: Independent Symbol Scores

1. Spawn the seven analyst and ten juror personas in parallel.
2. Give every first-verdict agent the same immutable `merged.json` snapshot and only eligible symbols.
3. Each agent independently returns one `+2`, `+1`, `0`, `-1`, or `-2` score per symbol using `verdict-format.md`.
4. Agents cannot see other verdict-agent results.
5. The main agent aggregates each symbol's valid scores using the thresholds in `verdict-format.md`.
6. Preserve raw responses, aggregation inputs, excluded symbols, and final first scores in `verdict-first.json`.
7. If no usable first-verdict result exists, still write `verdict-first.json` with `status="failed"` and stop before the second verdict.

### 5. Second Verdict: Portfolio Targets

1. Build the second-verdict set from:
   - eligible symbols with first score `+2` or `+1`
   - every eligible current holding, regardless of first score
2. Spawn the short-, mid-, and long-term judge personas in parallel.
3. Give each judge the same immutable collection data, `merged.json`, `verdict-first.json`, and sanitized `account-before-verdict.json`.
4. Judges perform portfolio-level comparison without external calls and return target quantities for every second-verdict symbol.
5. They must consider relative attractiveness, duplicate exposure, current weight, market conditions, and same-day fills.
6. Fixed minimum or maximum cash ratios are forbidden. Target cash is decided from the market and portfolio evidence.
7. The main agent reconciles judge outputs using `verdict-format.md` and writes `verdict-second.json`.
8. If no usable second-verdict target exists, still write `verdict-second.json` with `status="failed"` and do not calculate orders.

### 6. Latest Account Validation And Orders

1. Read `trade-execution.md`.
2. If analysis only was requested, write successful `account-before-order.json` and `execution.json` envelopes with `skipped=true` and do not call order APIs.
3. For order preparation or execution, the main agent refreshes account, holdings, order-available, pending, reservation, and same-day fill state and writes `account-before-order.json`.
4. For each eligible symbol:

   ```text
   expected_holding_quantity =
     current_live_holding_quantity
     + pending_and_reserved_buy_quantity
     - pending_and_reserved_sell_quantity

   additional_required_quantity =
     target_holding_quantity
     - expected_holding_quantity
   ```

5. Same-day filled quantity is used to prevent repeated trading. It is not subtracted from current live holdings again.
6. Create the final order list from the additional required quantities and the latest account constraints.
7. Submit orders only when explicitly authorized and all execution gates pass.
8. Write `execution.json` even when all orders are skipped, blocked, or fail.

### 7. Report And Finalize

1. Write `reports/YYYY-MM-DD_포트폴리오.md` using `report-template.md`.
2. Update `run.json` with final status and artifact states. Preserve every partial or failed artifact.
3. Summarize collection status, exclusions, first scores, second-verdict target quantities, cash decision, latest account state, and actual order submission.
4. Include the required `작업 시작` line.

## Sub-Agent Result Handling

- Use `fork_context=false` when available.
- Preserve every raw verdict result inside the corresponding verdict JSON.
- If a verdict response is structurally incomplete, request one correction without adding new data.
- After one failed correction, record the response error and continue with remaining valid responses.
- Never let a failed collector or verdict agent silently disappear from artifacts.

## Failure Rules

- Collection failure: preserve partial snapshots and exclude only symbols lacking required information.
- Authentication failure: reissue once using `auth-token.md`; if it still fails, block affected KIS, account, and order operations.
- Initial account snapshot failure: continue analysis only for requested/configured symbols, mark current holdings unknown, and do not calculate or submit orders.
- Latest account snapshot failure: do not calculate or submit orders. Preserve the failure in `account-before-order.json`.
- Order failure: do not reallocate the failed order's quantity or budget to another symbol during the same run.
- Sensitive data found in any payload: remove it before persistence or sub-agent transfer and record a sanitization error without recording the sensitive value.

## Storage Rules

- All JSON files are UTF-8 and valid JSON.
- All Markdown report text is Korean.
- Never store account numbers, account product codes, access tokens, app keys, app secrets, HTS IDs, or raw authentication headers.
- Missing data and errors remain explicit.
- The report must state that it is decision-support analysis, not investment advice.
