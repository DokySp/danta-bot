---
name: daily-trading
description: "[v20260608-02] `$check-holiday` preflight로 한국장 open/closed/unknown을 기록한 뒤, 전체 한국 주식·ETF 포트폴리오의 시장·재무·뉴스와 read-only 계좌 상태를 한 번 수집해 재사용하고, price-only 근거도 평결 후보로 보존하며, `decision-brief.json` 기반 `first-verdict`, `second-verdict` 이후 실행 gate를 통과한 명시 승인 주문만 `Main agent`가 제출한다. Use for KIS MCP based portfolio analysis, daily-* and pre-open trading schedules, sub-agent collection and verdict orchestration, portfolio reports, demo orders, real orders, and reservation orders."
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
- `second-verdict` personas: `references/personas/judge-*.md`

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
| Market collection | `market` sub-agent | `gpt-5.3-codex-spark` | `low` |
| News collection | `news` sub-agent | `gpt-5.3-codex-spark` | `low` |
| Financial collection/cache | `financial` sub-agent | `gpt-5.3-codex-spark` | `low` |
| `first-verdict` | selected 7 personas | `gpt-5.5` | `low` |
| `second-verdict` | 3 judge | `gpt-5.5` | `high` |
| Account refresh, order preparation, and execution | `Main agent` | `gpt-5.5` | `medium` |

## Run Identity, Market Status, And Metrics

The Codex execution prompt normally injects `run_id` and `started_at`.

1. Preserve the injected values unchanged for the complete run.
2. If either value is absent, generate both immediately before any daily-trading work:
   - `run_id`: filesystem-safe unique value
   - `started_at`: current Asia/Seoul time with timezone
3. Immediately create `reports/runs/<run_id>/run.json` using `run-artifacts.md`.
4. Immediately initialize `reports/runs/<run_id>/stage-metrics.json`.
5. Before account collection, verdicts, or order preparation, run `$check-holiday` for the Asia/Seoul target date.
6. Record the normalized market status in `run.json`, `decision-brief.json`, and the final report using only `open`, `closed`, or `unknown`.
7. Use `reports/runs/<run_id>/` for every intermediate JSON file.
8. Record every major stage in `stage-metrics.json` with stage name, agent role, `started_at`, `ended_at`, `duration_ms`, and status.
9. If exact token usage is available, record it. If exact token usage is unavailable, set `input_tokens`, `output_tokens`, and `total_tokens` to `null`, set `token_source="unavailable"`, and record a non-sensitive reason.
10. When this skill is actually used, include the following line in the final response on both success and failure:

   ```text
   작업 시작: YYYY-MM-DD HH:MM:SS KST
   ```

11. Do not show that line for work that did not use this skill.

These rules apply to direct and indirect use, including `daily-*`, `pre-open`, `$execute-trade`, and direct or indirect `$daily-trading` execution.

### Market Status Rules

- `open`: normal analysis and explicitly authorized demo/real order flow may continue.
- `closed`: label the run as previous-trading-day snapshot mode. Use the most recent valid trading-day price snapshot for verdicts. If the user or schedule explicitly requested 실전(acct) 예약거래, the Main agent may create reservation-order candidates and use only the `order_resv` path after every risk gate passes. Do not submit intraday `order_cash` while closed. This status affects market snapshot mode and order submission type only.
- `unknown`: analysis may continue, but 실전 order submission and reservation submission are blocked. Record the unknown reason from `$check-holiday`.

## Authority Boundaries

### Main Agent Only

- Orchestrate every stage and decide the portfolio universe.
- Create and update files under `reports/`.
- Perform KIS authentication preflight and token reissue.
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
- They return JSON only through launcher wrappers and do not write canonical artifact files.

### Verdict Sub-Agents

- Run every verdict role through the daily-trading sub-agent launcher. Do not use `multi_agent_v1.spawn_agent` for daily-trading stage delegation because the launcher is the model/effort enforcement boundary.
- `first-verdict` and `second-verdict` agents cannot call KIS, web, MCP, network, shell, or any external data source.
- They cannot write files or submit orders.
- Their input is `decision-brief.json`.
- They may use only the exact snapshots, personas, and verdict artifacts supplied by the Main agent.
- Missing data stays missing. Recollection, substitution from another symbol, target-quantity invention outside the assigned stage, and guessing are forbidden.

## Execution Flow

### 1. Initialize

1. Resolve input symbols from the user request and, when requested, `$check-portfolio`.
2. Run `$check-holiday` for the Asia/Seoul target date and record the normalized `open | closed | unknown` status.
3. Perform authentication preflight using `auth-token.md`.
4. The Main agent collects the initial read-only account snapshot and writes `account-before-verdict.json`.
5. Sanitize and persist only non-sensitive account fields.
6. The complete portfolio universe is the union of requested/configured symbols and current holdings. Resolve names and identifiers without dropping unresolved inputs.
7. If the initial account snapshot fails, continue collection and analysis only for requested/configured symbols, mark current holdings as unknown, and block order preparation and submission.
8. Record every step above in `stage-metrics.json`.

### 2. Collect Once

1. Run the daily-trading sub-agent launcher for the `market`, `financial`, and `news` collection agents in parallel.
2. Give each agent the same complete symbol list, `run_id`, `started_at`, environment, schema, and permission boundary. The launcher spec must include the same complete list in `symbol_ids` so wrapper validation can reject missing-symbol market outputs before the Main agent writes canonical artifacts.
3. `market` collection is required for verdict and trading. `financial` and `news` collection are best-effort report inputs; missing, failed, partial, or no-data financial/news results must not stop merge, verdicts, target calculation, or order execution.
4. The financial path is cache-first:
   - cache location: `~/.cache/codex/collect-financial-information/<YYYY-MM-DD>.json`
   - validity: one Korea trading day
   - cache helper: `collect-financial-information/scripts/financial_cache.py`
   - cache hit: generate `financial.json` from helper-validated cache without external financial calls
   - cache miss or explicit force refresh: collect from KIS/official sources and let the Main agent update the cache through the helper
   - invalid, failed, empty, wrong-date, wrong-stage, wrong-domain, or missing-requested-symbol cache payloads are misses and must not overwrite an existing valid cache
5. The news path is KIS news/disclosure only. Web search and web news sources are not allowed.
6. Write each returned payload immediately and exactly once when available:
   - `market.json`
   - `financial.json`
   - `news.json`
7. If the market launcher wrapper is missing, `failed`, or contains no valid `parsed_json`, the Main agent writes a `failed` market envelope containing every symbol and the agent-level error. If the financial/news wrapper is missing, `failed`, or invalid, record a non-blocking warning and continue from market/account data.
8. If an agent or symbol partially fails, preserve its successful data and errors. Do not discard or overwrite an already-created snapshot.
9. Do not recollect between `first-verdict` and `second-verdict`. All verdict stages reuse saved artifacts.

The market collector gathers KIS price, chart, order-book/trade, flow, rank, industry, and ETF/NAV data. It must return canonical `symbol_id`, `symbol_name`, and `price.current_or_last` / `price.observed_at` fields for every input symbol. Alias-only fields such as `symbol` or `current_or_latest_price` are not valid canonical market evidence. The financial and news collectors follow their own skills.

### 3. Merge, Exclude, And Brief

1. Merge required market/account evidence and any available financial/news summaries directly into `decision-brief.json`.
2. Record source provenance and per-symbol errors in `decision-brief.json`.
3. A symbol receives `eligible_for_verdict=false` only when symbol identity is unresolved/ambiguous, usable price or observation time is missing, or market data is too broken to support even a price-only verdict.
4. Missing financial data, missing news/disclosure data, completed no-data searches, failed financial/news wrappers, or absent financial/news artifacts are not exclusion reasons by themselves. If identifier, name, current-or-last price, and observation time exist, keep the symbol eligible.
5. Missing, partial, failed, no-data, or absent financial/news evidence must not lower a score, remove a target, block target calculation, block `order_cash`, block `order_resv`, block demo submission, or block real submission by itself.
6. An ineligible symbol is excluded from every verdict stage, target-quantity calculation, and trading. Keep it in artifacts and the report with explicit exclusion reasons.
7. Build `decision-brief.json` from market/account evidence, optional financial/news summaries, and market status.
8. `decision-brief.json` includes symbol id/name, eligibility, evidence mode, price and observation time, core market signals, core financial summary when available, core KIS news summary when available, account exposure summary, and missing/error reasons.
9. `decision-brief.json` excludes long raw API payloads, full article text, repeated source detail, and sensitive information.
10. Keep `decision-brief.json` compact for verdict fan-out: at most five market signals, three financial summary bullets when available, three KIS news/disclosure items when available, and five warnings/errors per symbol. Summarize repeated domain-wide missing reasons once and reference the domain status from symbols.
11. Domain or run status uses only `success`, `partial`, or `failed` as defined in `run-artifacts.md`.

### 4. `first-verdict`: Independent Symbol Scores

1. Run the daily-trading sub-agent launcher for the selected seven `first-verdict` personas in parallel.
2. Give every first-verdict agent the same immutable `decision-brief.json` and only eligible symbols, including `evidence_mode="price_only"` symbols.
3. Each agent independently returns one `+2`, `+1`, `0`, `-1`, or `-2` score per symbol using `verdict-format.md`.
4. Agents cannot see other verdict-agent results.
5. The Main agent aggregates each symbol's valid scores using the thresholds in `verdict-format.md`.
6. Preserve raw responses, aggregation inputs, excluded symbols, and final first scores in `verdict-first.json`.
7. If no usable `first-verdict` result exists, still write `verdict-first.json` with `status="failed"` and stop before `second-verdict`.

### 5. `second-verdict`: Portfolio Targets

1. Build the `second-verdict` set from:
   - eligible symbols with first score `+2` or `+1`
   - every eligible current holding, regardless of first score
2. Run the daily-trading sub-agent launcher for the short-, mid-, and long-term judge personas in parallel.
3. Give each judge the same immutable `decision-brief.json` and `verdict-first.json`.
4. Judges perform portfolio-level comparison without external calls and return target quantities for every `second-verdict` symbol.
5. They must consider relative attractiveness, duplicate exposure, current weight, market conditions, and same-day fills from the brief.
6. Fixed minimum or maximum cash ratios are forbidden. Target cash is decided from the market and portfolio evidence.
7. The Main agent reconciles judge outputs using `verdict-format.md` and writes `verdict-second.json`.
8. If no usable `second-verdict` target exists, still write `verdict-second.json` with `status="failed"` and do not calculate orders.

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
11. Submit orders only when explicitly authorized, market status permits the requested submission type, and all execution gates pass.
12. Record the order API response, accepted order or reservation identifiers, blocked candidates, and failed submissions in `execution.json`.
13. Do not run routine verification lookups after accepted submissions. If an order result is uncertain because of timeout or transport ambiguity, perform only the minimum lookup needed to avoid duplicate submission and record that lookup as part of the order attempt.
14. Write `execution.json` even when all orders are skipped, blocked, or fail.

### 7. Report And Finalize

1. Write `reports/YYYY-MM-DD_포트폴리오.md` using `report-template.md`.
2. Update `run.json` with final status, validation status, market status, and artifact states. Preserve every partial or failed artifact.
3. Summarize market status, snapshot mode, collection status, exclusions, price-only warnings, `first-verdict` scores, `second-verdict` target quantities, cash decision, latest account state, stage metrics availability, and actual order submission.
4. Include the required `작업 시작` line.

## Sub-Agent Result Handling

- Use the daily-trading sub-agent launcher for collection and verdict roles. `fork_context=false` remains the conceptual isolation contract; transfer results through launcher wrapper files and canonical artifacts.
- Runner input specs are JSON files containing `run_id`, `started_at`, `stage`, `agent_role`, `task_name`, `prompt`, `workspace_dir`, and `output_dir`.
- The launcher writes `reports/runs/<run_id>/subagents/<task_name>.wrapper.json` and raw text only. The Main agent owns every canonical artifact under `reports/runs/<run_id>/`.
- Main agent may use only sanitized wrapper `parsed_json` for canonical artifacts. Failed wrappers, invalid JSON, or missing wrappers must be recorded as failed stage evidence in `stage-metrics.json`.
- Preserve every raw verdict result inside the corresponding verdict JSON.
- If a verdict response is structurally incomplete, request one correction without adding new data.
- After one failed correction, record the response error and continue with remaining valid responses.
- Never let a failed collector or verdict agent silently disappear from artifacts.

## Failure Rules

- Collection failure: preserve partial snapshots and exclude only symbols lacking required information.
- Authentication failure: reissue once using `auth-token.md`; if it still fails, block affected KIS, account, and order operations.
- Initial account snapshot failure: continue analysis only for requested/configured symbols, mark current holdings unknown, and do not calculate or submit orders.
- Latest account snapshot failure: do not calculate or submit orders. Preserve the failure in `account-before-order.json`.
- Market status `unknown`: do not submit real orders or reservation orders. Analysis and reports may continue.
- Market status `closed`: submit only explicit reservation orders through `order_resv`; otherwise prepare/report candidates without submission.
- Runtime validation failure from `scripts/validate_run.py`: mark the run validation failed and include the validation summary in the final Telegram response.
- Order failure: do not reallocate the failed order's quantity or budget to another symbol during the same run.
- Sensitive data found in any payload: remove it before persistence or sub-agent transfer and record a sanitization error without recording the sensitive value.

## Storage Rules

- All JSON files are UTF-8 and valid JSON.
- All Markdown report text is Korean.
- Never store account numbers, account product codes, access tokens, app keys, app secrets, HTS IDs, or raw authentication headers.
- Missing data and errors remain explicit.
- The report must state that it is decision-support analysis, not investment advice.
