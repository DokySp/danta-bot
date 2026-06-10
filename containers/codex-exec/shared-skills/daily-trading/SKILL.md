---
name: daily-trading
description: "[v20260608-02] 전체 한국 주식·ETF 포트폴리오의 시장·재무·뉴스와 read-only 계좌 상태를 한 번 수집해 재사용하고, price-only 근거도 평결 후보로 보존하며, `decision-brief.json` 기반 `first-verdict`, `second-verdict` 이후 실행 gate를 통과한 명시 승인 주문만 `Main agent`가 제출한다. Use for KIS MCP based portfolio analysis, daily-* and pre-open trading schedules, sub-agent collection and verdict orchestration, portfolio reports, demo orders, real orders, and reservation orders."
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
- `first-verdict` personas: selected files under `references/personas/analyst-*.md` plus `references/personas/juror-05-역발상.md`
- `second-verdict` personas: `references/personas/judge-midterm.md` and `references/personas/judge-longterm.md`

## Canonical Terms

| Concept | Canonical term |
|---|---|
| Main execution owner | `Main agent` |
| First independent symbol verdict stage | `first-verdict` |
| Second portfolio target verdict stage | `second-verdict` |
| Compact verdict input | `decision-brief.json` |

## Launcher Model Matrix

`scripts/run_subagent.py` maps each sub-agent stage to the `codex exec` model and effort shown below.

| Stage | Agent | Mandatory model | Mandatory effort |
|---|---|---|---|
| Main orchestration and account snapshots | `Main agent` | `gpt-5.5` | `medium` |
| Market collection | `market` sub-agent | `gpt-5.5` | `low` |
| News collection | `news` sub-agent | `gpt-5.5` | `low` |
| Financial summary collection | `financial` sub-agent | `gpt-5.5` | `low` |
| `first-verdict` | selected 7 personas | `gpt-5.5` | `low` |
| `second-verdict` | 2 judges | `gpt-5.5` | `low` |
| Account refresh, order preparation, and execution | `Main agent` | `gpt-5.5` | `medium` |

## Run Identity

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

- Orchestrate every stage and decide the portfolio universe.
- Create and update files under `reports/`.
- Handle KIS authentication boundaries using `auth-token.md`.
- Collect read-only account snapshots directly and write `account-before-verdict.json` and `account-before-order.json`.
- Sanitize every artifact and every sub-agent input.
- Merge collection output, decide exclusions, build `decision-brief.json`, calculate order candidates, confirm explicit user authorization, and submit authorized orders.
- Call actual order submission APIs only after every gate passes.
- Do not collect market, financial, or news domain data directly in place of the assigned collection sub-agents. Price snapshots, symbol market information, financial evidence, and news/disclosure evidence belong to `market`, `financial`, and `news` collection outputs.
- Never delegate account lookup, order submission, reservation submission, correction, cancellation, artifact persistence, or sensitive-field handling.

### Collection Sub-Agents

- Run the daily-trading sub-agent launcher in parallel for the three collection agents: `market`, `financial`, and `news`.
- They receive the complete portfolio universe and collect detailed data for every symbol in their domain.
- External calls are allowed only inside each domain's permission boundary.
- KIS calls are made directly at each call site. Transient gateway, timeout, and rate-limit failures must use the bounded backoff rules in `data-collection.md`.
- `market` must collect symbol market information and price snapshots. The Main agent must not replace this by directly calling price or quote APIs.
- `financial` must use `$collect-financial-information`.
- `news` must use `$collect-news-information` and is KIS news/disclosure only.
- Account, balance, order, order-available, fill-history, pending-order, reservation-order, correction, and cancellation APIs are forbidden.
- `market` returns JSON through launcher wrappers. `financial` and `news` return compact Markdown text through launcher wrappers. Collection sub-agents do not write canonical artifact files.

### Verdict Sub-Agents

- Run every verdict role through the daily-trading sub-agent launcher. Do not use `multi_agent_v1.spawn_agent` for daily-trading stage delegation because the launcher is the model/effort enforcement boundary.
- `first-verdict` and `second-verdict` agents cannot call KIS, web, MCP, network, shell, or any external data source.
- They cannot submit orders.
- They may write only their own human-review Markdown companion file under `reports/runs/<run_id>/verdicts/` using the fixed filename rules in `verdict-format.md`. They must not write or modify canonical JSON artifacts.
- Their input is `decision-brief.json`.
- They may use only the exact snapshots, personas, and verdict artifacts supplied by the Main agent.
- Missing data stays missing. Recollection, substitution from another symbol, target-quantity invention outside the assigned stage, and guessing are forbidden.

## Execution Flow

### 1. Initialize

1. Resolve configured/input symbols from the user request and, when requested, `$check-portfolio`.
2. Perform authentication preflight using `auth-token.md`.
3. The Main agent collects the initial read-only account snapshot and writes `account-before-verdict.json`.
4. Sanitize and persist only non-sensitive account fields.
5. The complete portfolio universe is the union of requested/configured symbols from `$check-portfolio` and current live holdings from `account-before-verdict.json`. Resolve names and identifiers without dropping unresolved inputs or live holdings.
6. If the initial account snapshot fails, continue collection and analysis only for requested/configured symbols, mark current holdings as unknown, and block order preparation and submission.

### 2. Collect Once

1. Run the daily-trading sub-agent launcher for the `market`, `financial`, and `news` collection agents in parallel.
2. Give each agent the same complete symbol list, `run_id`, `started_at`, environment, schema, and permission boundary. The launcher spec must include the same complete list in `symbol_ids` so wrapper validation can reject missing-symbol market outputs before the Main agent writes canonical artifacts.
3. `market` collection is required for verdict and trading. `financial` and `news` collection are best-effort report inputs; missing, failed, partial, or no-data financial/news results must not stop merge, verdicts, target calculation, or order execution.
4. The financial path returns compact Markdown only. Do not require a JSON envelope, schema validation, cache helper, or per-symbol coverage validation for financial context.
5. The news path returns compact Markdown only. It is KIS news/disclosure only; web search and web news sources are not allowed.
6. Write each returned result immediately and exactly once when available:
   - `market.json`
   - `financial.md`
   - `news.md`
7. If the market launcher wrapper is missing, `failed`, or contains no valid `parsed_json`, the Main agent writes a `failed` market envelope containing every symbol and the agent-level error. If the financial/news wrapper is missing, `failed`, has empty `parsed_text`, or returns no usable text, record a non-blocking warning and continue from market/account data.
8. If an agent or symbol partially fails, preserve its successful data and errors. Do not discard or overwrite an already-created snapshot.
9. Do not recollect between `first-verdict` and `second-verdict`. All verdict stages reuse saved artifacts.

The market collector gathers KIS price, chart, order-book/trade, flow, rank, industry, and ETF/NAV data. It must return canonical `symbol_id`, `symbol_name`, and `price.current_or_last` / `price.observed_at` fields for every input symbol. Alias-only fields such as `symbol` or `current_or_latest_price` are not valid canonical market evidence. The financial and news collectors follow their own skills.

### 3. Merge, Exclude, And Brief

1. Merge required market/account evidence and any available short bullets from `financial.md` and `news.md` directly into `decision-brief.json`.
2. Record source provenance and per-symbol errors in `decision-brief.json`.
3. A symbol receives `eligible_for_verdict=false` only when symbol identity is unresolved/ambiguous, usable price or observation time is missing, or market data is too broken to support even a price-only verdict.
4. Missing financial data, missing news/disclosure data, completed no-data searches, failed financial/news wrappers, or absent financial/news artifacts are not exclusion reasons by themselves. If identifier, name, current-or-last price, and observation time exist, keep the symbol eligible.
5. Missing, partial, failed, no-data, or absent financial/news evidence must not lower a score, remove a target, block target calculation, block `order_cash`, block `order_resv`, block demo submission, or block real submission by itself.
6. An ineligible symbol is excluded from every verdict stage, target-quantity calculation, and trading. Keep it in artifacts and the report with explicit exclusion reasons.
7. Build `decision-brief.json` from market/account evidence and optional financial/news summaries.
8. `decision-brief.json` includes symbol id/name, eligibility, evidence mode, price and observation time, core market signals, core financial summary when available, core KIS news summary when available, account exposure summary, and missing/error reasons.
9. `decision-brief.json` excludes long raw API payloads, full article text, repeated source detail, and sensitive information.
10. Keep `decision-brief.json` compact for verdict fan-out: at most five market signals, three financial summary bullets when available, three KIS news/disclosure items when available, and five warnings/errors per symbol. Summarize repeated domain-wide missing reasons once and reference the domain status from symbols.
11. Domain or run status uses only `success`, `partial`, or `failed` as defined in `run-artifacts.md`.

### 4. `first-verdict`: Independent Symbol Scores

1. Run the daily-trading sub-agent launcher for the selected seven `first-verdict` personas in parallel.
2. Give every first-verdict agent the same immutable `decision-brief.json` and only eligible symbols, including `evidence_mode="price_only"` symbols.
3. Tell each agent its required human-review Markdown companion path from `verdict-format.md`.
4. Each agent independently returns one `+2`, `+1`, `0`, `-1`, or `-2` score per symbol using `verdict-format.md`.
5. Agents cannot see other verdict-agent results.
6. The Main agent aggregates each symbol's valid scores using the thresholds in `verdict-format.md`.
7. Preserve raw responses, aggregation inputs, excluded symbols, final first scores, and any companion Markdown paths in `verdict-first.json`.
8. If no usable `first-verdict` result exists, still write `verdict-first.json` with `status="failed"` and stop before `second-verdict`.

### 5. `second-verdict`: Portfolio Targets

1. Build the `second-verdict` set from:
   - eligible symbols with first score `+2` or `+1`
   - every eligible current holding, regardless of first score
2. Run the daily-trading sub-agent launcher for the mid- and long-term judge personas in parallel.
3. Give each judge the same immutable `decision-brief.json` and `verdict-first.json`.
4. Tell each judge its required human-review Markdown companion path from `verdict-format.md`.
5. Judges perform portfolio-level comparison without external calls and return target quantities for every `second-verdict` symbol.
6. They must consider relative attractiveness, duplicate exposure, current weight, market conditions, and same-day fills from the brief.
7. Fixed minimum or maximum cash ratios are forbidden. Target cash is decided from the market and portfolio evidence.
8. The Main agent reconciles judge outputs using `verdict-format.md`, preserves any companion Markdown paths, and writes `verdict-second.json`.
9. If no usable `second-verdict` target exists, still write `verdict-second.json` with `status="failed"` and do not calculate orders.

### 6. Account Refresh, Order Preparation, And Execution

1. Read `trade-execution.md`.
2. If analysis only was requested, write successful skipped envelopes for `account-before-order.json` and `execution.json`; do not call order APIs.
3. For order preparation or execution, the Main agent refreshes the latest read-only account snapshot and writes `account-before-order.json`.
4. Sanitize and persist only non-sensitive account fields.
5. If `account-before-order.json` is missing, invalid, or `failed`, do not calculate or submit orders.
6. For each eligible symbol:

   ```text
   expected_holding_quantity =
     current_live_holding_quantity
     + pending_and_reserved_buy_quantity
     - pending_and_reserved_sell_quantity

   additional_required_quantity =
     target_holding_quantity
     - expected_holding_quantity
   ```

7. Same-day filled quantity is used to prevent repeated trading. It is not subtracted from current live holdings again.
8. `expected_holding_quantity` is the pre-candidate expected holding quantity after already-active pending and reserved quantities are considered. It is valid for `expected_holding_quantity` to differ from `target_holding_quantity`; consistency is checked with `target_holding_quantity = expected_holding_quantity + additional_required_quantity`.
9. Create the order candidate list from the additional required quantities and the latest account constraints.
10. Apply `trade-execution.md` gates directly in the `Main agent`; do not launch a separate final risk sub-agent.
11. Submit orders only when explicitly authorized and all execution gates pass.
12. Record the order API response, accepted order or reservation identifiers, blocked candidates, and failed submissions in `execution.json`.
13. Do not run routine verification lookups after accepted submissions. If an order result is uncertain because of timeout or transport ambiguity, perform only the minimum lookup needed to avoid duplicate submission and record that lookup as part of the order attempt.
14. Write `execution.json` even when all orders are skipped, blocked, or fail.

### 7. Report And Finalize

1. Write `reports/YYYY-MM-DD_포트폴리오.md` using `report-template.md`.
2. Update `run.json` with final status and artifact states. Preserve every partial or failed artifact.
3. Summarize collection status, exclusions, price-only warnings, `first-verdict` scores, `second-verdict` target quantities, cash decision, latest account state, and actual order submission.
4. Include the required `작업 시작` line.

## Sub-Agent Result Handling

- Use the daily-trading sub-agent launcher for collection and verdict roles. `fork_context=false` remains the conceptual isolation contract; transfer results through launcher wrapper files and canonical artifacts.
- Runner input specs are JSON files containing `run_id`, `started_at`, `stage`, `agent_role`, `task_name`, `prompt`, `workspace_dir`, and `output_dir`.
- The launcher writes `reports/runs/<run_id>/subagents/<task_name>.wrapper.json` and raw text only. The Main agent owns every canonical artifact under `reports/runs/<run_id>/`.
- Main agent may use only sanitized wrapper `parsed_json` for JSON artifacts and sanitized wrapper `parsed_text` for `financial.md` and `news.md`. Failed wrappers, invalid JSON for JSON-required stages, missing wrappers, or empty financial/news text must remain visible in wrapper files and the affected canonical artifact status.
- Preserve every raw verdict result inside the corresponding verdict JSON.
- Preserve any verdict-agent human-review Markdown companion file paths inside the corresponding verdict JSON, but do not use Markdown content as machine-validated input for aggregation, reconciliation, target calculation, or order gates.
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
