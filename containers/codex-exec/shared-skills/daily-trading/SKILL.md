---
name: daily-trading
description: "[v20260619-01] `run_daily_trading_pipeline.py` 단일 명령으로 한국 주식·ETF 포트폴리오 수집, compact verdict sub-agent 실행, 비제출 주문 gate 요약을 수행한다. Main agent는 `pipeline-summary.json`만 우선 읽어 토큰 사용을 줄이고, 가격·계좌·주문 gate는 보존하며 재무·뉴스·market-status는 optional-best-effort로 다룬다."
---

# Daily Trading Portfolio Orchestrator

Token budget rule: load only the reference needed for the current stage. Do not paste whole artifacts, raw API payloads, full cache files, or repeated rule text into sub-agent prompts.

## References By Stage

| Stage | Read |
|---|---|
| Initialize/auth/account/order | `references/rules/auth-token.md`, `references/rules/trade-execution.md` |
| Price/chart, financial, news, market-status, brief | `references/rules/data-collection.md`, `references/rules/run-artifacts.md` |
| `first-verdict`, `second-verdict` | `references/rules/verdict-format.md` plus only that agent's persona file |
| Report | `references/rules/report-template.md` |
| Strategy interpretation | `references/rules/strategy-mapping.md` only when signals need mapping |

## Canonical Terms

| Concept | Term |
|---|---|
| Main execution owner | `Main agent` |
| Independent symbol score stage | `first-verdict` |
| Portfolio target stage | `second-verdict` |
| Canonical verdict input | `decision-brief.json` |
| Sub-agent verdict input | launcher-created lossless selected-symbol slices |

## Pipeline Contract

Routine daily-trading execution must use the pipeline first. The Main agent should not manually orchestrate the helper/launcher sequence unless the pipeline itself fails and the failed stage cannot be diagnosed from `pipeline-summary.json`.

```text
python3 <daily-trading-skill>/scripts/run_daily_trading_pipeline.py run \
  --workspace-dir <workspace> \
  --output-dir reports/runs/<run_id> \
  --run-id <run_id> \
  --started-at <started_at> \
  --env <acct|paper> \
  --request-type <analysis|prepare|demo-submit|real-submit>
```

After the command returns, read `reports/runs/<run_id>/pipeline-summary.json` first and use it for the user-facing response. Open `pipeline-command-log.json` or intermediate artifacts only when a stage failed and the compact summary does not contain enough evidence. The pipeline captures verbose helper stdout internally, writes canonical artifacts, and prints only a compact JSON pointer to stdout.

Validation command after pipeline changes:

```text
python3 <daily-trading-skill>/scripts/run_daily_trading_pipeline.py self-test
```

## Launcher Contract

The pipeline uses `scripts/run_subagent.py` for verdict sub-agents. Use the launcher directly only for focused retry/debug of a failed stage. The launcher enforces collection sub-agents with `gpt-5.4-mini` and `model_reasoning_effort=low`, `first-verdict` sub-agents with `gpt-5.5` and `model_reasoning_effort=medium`, and `second-verdict` sub-agents with `gpt-5.5` and `model_reasoning_effort=medium`. It writes `subagents/<task_name>.wrapper.json`, raw output when retained, token usage metadata, and verdict input slices when compact verdict specs are used. It treats financial/news/market-status text stages as optional group failures. Do not use `multi_agent_v1.spawn_agent` for daily-trading stage delegation.

Treat the launcher as a verified command interface, not as context to reread on every run. After install or launcher changes, validate it with:

```text
python3 <daily-trading-skill>/scripts/run_subagent.py self-test
```

Focused fallback verdict execution should only create compact spec JSON files and call:

```text
python3 <daily-trading-skill>/scripts/run_subagent.py run-group --spec reports/runs/<run_id>/first-verdict-specs.json --max-workers 3
python3 <daily-trading-skill>/scripts/run_subagent.py run-one --spec reports/runs/<run_id>/second-verdict-spec.json
```

Do not open or paste `scripts/run_subagent.py`, persona files, or `references/rules/*.md` in full merely to execute these stages. Read a specific rule file only when the current stage's safety gate or schema is genuinely ambiguous. The installed skill path may differ between host and container environments; resolve the actual installed path and do not treat a path mismatch as a skill failure.

For verdict stages, use compact specs with `artifact_paths` and `symbol_ids`; do not include `prompt`. The launcher builds the standard prompt from those fields and writes per-task lossless `verdict-inputs/` slices containing only the listed symbols. It derives `verdict-core` from `decision-brief.json` and derives a selected-symbol first-verdict slice from `verdict-first.json` for `second-verdict`.

Supported sub-agent stages:

- `financial-collection`: `$collect-financial-information`; text output is a cache path or fixed missing-cache message. Skip this sub-agent when a valid same-date cache for the full symbol universe is already available.
- `news-collection`: `$collect-news-information`; text output is a cache path or fixed missing-cache message. Skip this sub-agent when a valid same-date cache for the full symbol universe is already available.
- `market-status-collection`: `$get-market-status`; text output is concise Markdown for S&P 500, Nasdaq, Dow, KOSPI, and KOSDAQ.
- `first-verdict`: selected three functional personas: `analyst-quality-value`, `analyst-momentum-cycle`, `analyst-risk-allocation`.
- `second-verdict`: `judge-midterm` only. Retry the failed `judge-midterm` task at most two times when the required output is missing or unusable.

## Run Identity

Preserve injected `run_id` and `started_at`. If absent, generate both before work starts, create `reports/runs/<run_id>/run.json`, and use that run directory for every artifact. When this skill is actually used, include this final line:

```text
작업 시작: YYYY-MM-DD HH:MM:SS KST
```

## Authority Boundaries

Main agent only:

- Resolve the portfolio universe from `$check-portfolio` JSON `universe`; do not separately re-read live holdings only to expand the universe.
- Handle auth, account snapshots, direct price/chart evidence collection through `scripts/collect_main_evidence.py`, artifact writes, sanitization, merging, exclusions, target/order calculation, explicit approval checks, active pending/reserved order adjustment, and order submission.
- Write canonical artifacts under `reports/runs/<run_id>/`.

## Main Evidence Helper

Use `scripts/collect_main_evidence.py` for required price/chart evidence and the initial sanitized account snapshot. The helper calls direct KIS REST APIs, writes only canonical JSON artifacts, and prints a compact path/count summary.

```text
python3 scripts/collect_main_evidence.py collect \
  --run-id <run_id> \
  --started-at <started_at> \
  --symbols <comma-separated-universe> \
  --output-dir reports/runs/<run_id> \
  --env acct
```

The helper may write `collection-summary.json`, `price-chart.json`, and `account-before-order.json`. It must not submit, cancel, correct, or reserve orders. If active-order or order-available lookups are not collected, order preparation/execution remains blocked until the Main agent refreshes those required fields with validated read-only APIs.

## Deterministic Artifact Helper

The pipeline uses `scripts/build_run_artifacts.py` for deterministic artifact shaping instead of rebuilding these JSON files in the Main agent prompt. Use the helper directly only for pipeline debugging or a focused retry. The helper writes canonical artifacts and specs only; it does not call KIS, submit orders, or change account state.

```text
python3 <daily-trading-skill>/scripts/build_run_artifacts.py decision-brief --output-dir reports/runs/<run_id> --portfolio-json <check-portfolio-json-path> --financial-cache-path <optional-financial-cache-path>
python3 <daily-trading-skill>/scripts/build_run_artifacts.py first-specs --output-dir reports/runs/<run_id> --workspace-dir <workspace>
python3 <daily-trading-skill>/scripts/build_run_artifacts.py merge-first --output-dir reports/runs/<run_id>
python3 <daily-trading-skill>/scripts/build_run_artifacts.py second-spec --output-dir reports/runs/<run_id> --portfolio-json <check-portfolio-json-path> --workspace-dir <workspace>
python3 <daily-trading-skill>/scripts/build_run_artifacts.py execution-plan --output-dir reports/runs/<run_id> --request-type <analysis|prepare|demo-submit|real-submit>
python3 <daily-trading-skill>/scripts/build_run_artifacts.py token-summary --run-dir reports/runs/<run_id> --main-events <codex-json-events-path>
```

After install or helper changes, validate it with:

```text
python3 <daily-trading-skill>/scripts/build_run_artifacts.py self-test
```

Collection sub-agents:

- Receive the complete symbol universe, but may only collect their assigned optional domain.
- Never call account, balance, order, order-available, fill-history, pending-order, reservation-order, correction, or cancellation APIs.

Verdict sub-agents:

- Use only supplied snapshots, launcher-created verdict slices, and their persona.
- May use read-only local shell commands such as `cat` and `jq` only for explicitly listed artifact/persona/rule files.
- Do not call KIS, MCP, web, network, account/order APIs, or external data sources.
- Return compact JSON only. They must not write files, create Markdown sidecars, emit diffs, or include code fences.
- Include the informational `human_markdown_path`; the Main agent creates human-review Markdown sidecars from parsed JSON.

## Execution Flow

Routine path: run `scripts/run_daily_trading_pipeline.py run`, then read only `pipeline-summary.json` unless a failed stage needs deeper diagnosis. The detailed flow below is the pipeline's internal contract and the fallback manual sequence.

1. Initialize run identity and auth handling.
2. Build the complete portfolio universe from `$check-portfolio` JSON `universe`, which already includes `recommanded`, `specified`, and direct KIS `holding` symbols.
3. Main agent runs `scripts/collect_main_evidence.py` once for the complete universe and writes `price-chart.json`, `account-before-order.json`, and optional `collection-summary.json`.
4. Reuse valid same-date financial/news caches only when their top-level `symbols` keys cover the full symbol universe. If a same-date cache is missing or incomplete, call the matching helper `get`, run the matching collector once, then call `get` again. If the cache is still absent, continue without that optional domain; if it exists but remains incomplete, pass the partial cache path into the brief so available rows can be used. Do not retry financial/news collection more than once in the same pipeline run. Default pipeline execution still records `market-status` as skipped. Optional financial/news/market-status absence remains non-blocking.
5. Use `scripts/build_run_artifacts.py decision-brief` to merge required price/chart and account evidence plus short optional summaries into compact `decision-brief.json`.
6. Use `scripts/build_run_artifacts.py first-specs`, then run the selected three `first-verdict` functional personas in parallel with launcher-created `verdict-core` inputs. Main agent may reduce `symbol_ids` and merge prior valid verdict rows only when it can prove price, holdings, news, and active-order status are stable; otherwise rerun the symbol. Always re-evaluate sell/stop-loss candidates, same-day fills, price shocks, new news, active orders, and score-boundary cases. Launcher automatic wrapper reuse is limited to the same spec fingerprint. Use `scripts/build_run_artifacts.py merge-first` to create `verdict-first.json` and human-review sidecars from parsed JSON.
7. Use `scripts/build_run_artifacts.py second-spec` to build the `second-verdict` set from eligible symbols with `final_first_score >= 7` plus eligible `holding` symbols from `$check-portfolio`, then run only `judge-midterm` with a selected-symbol first-verdict slice and at most two retries for the failed task only. Use the single valid judge target as the canonical target after deterministic validation.
8. Refresh any missing required account/order-availability fields before orders, validate the single target set, reconcile active pending/reserved orders, apply `trade-execution.md` gates, and submit only explicitly authorized adjustments or orders. Use `scripts/build_run_artifacts.py execution-plan` for the deterministic non-submitting order math and gate summary before any actual submission attempt.
9. Write `execution.json`, final report, and final `run.json` status.

## Compact Prompt Rules

- Sub-agent prompts should name the stage, role, run paths, required output, one relevant persona/rule reference, and the compact artifact paths.
- Verdict sub-agents may use read-only local shell commands such as `cat` and `jq` only for explicitly listed artifact/persona/rule files. KIS, MCP, web, network, account/order APIs, file writes, raw caches, secrets, and unlisted paths remain forbidden.
- Verdict compact specs must pass `artifact_paths.decision_brief` and `symbol_ids`; `second-verdict` must also pass `artifact_paths.verdict_first`. They may pass `artifact_paths.persona` and `artifact_paths.verdict_format`.
- Verdict compact specs must omit `prompt`; raw prompt fallback is forbidden for verdict stages.
- Pass artifact paths and short excerpts, not full JSON, unless the sub-agent must score that exact compact JSON.
- Keep canonical `decision-brief.json` for the run record, but pass launcher-created verdict slices to sub-agents.
- Do not repeat auth, order, sanitization, optional-stage, or schema rules in every prompt. Cite the relevant rule file instead.
- Verdict output must be compact JSON with short `reason_code` and `one_line_reason` fields instead of long rationale, risk, or evidence arrays.
- Missing financial/news/market-status data stays optional and must not become a verdict or order blocker by prompt wording.

## Failure Rules

- Required price/chart, account, verdict, and explicitly requested order gates fail closed according to the rule docs.
- If `market_status` from `$check-holiday` or scheduler context is supplied, order execution requires `open`; `closed` or `unknown` blocks order submission.
- Financial/news/market-status failures are visible but non-blocking when price/chart and account gates pass.
- A failed required wrapper remains evidence; write the failed canonical envelope and stop only the dependent stages.
- When retrying, submit only failed sub-agent specs. Successful wrappers with the same `spec_fingerprint` are reusable evidence.
- Sensitive values must be removed before artifacts, prompts, reports, or user responses.
