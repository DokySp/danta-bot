---
name: daily-trading
description: "[v20260611-02] KIS MCP 기반 한국 주식·ETF 포트폴리오를 one-pass로 수집하고, compact `decision-brief.json`을 재사용해 4개 관점 `first-verdict`, 단일 `judge-midterm` `second-verdict`, 계좌/주문 gate를 수행한다. 가격·계좌·주문 gate는 보존하고 재무·뉴스·market-status는 optional-best-effort로 다룬다."
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
| Compact verdict input | `decision-brief.json` |

## Launcher Contract

Use `scripts/run_subagent.py` for every collection and verdict sub-agent. The launcher enforces collection sub-agents with `gpt-5.4-mini` and `model_reasoning_effort=low`, `first-verdict` sub-agents with `gpt-5.4-mini` and `model_reasoning_effort=medium`, and `second-verdict` sub-agents with `gpt-5.5` and `model_reasoning_effort=low`. It writes `subagents/<task_name>.wrapper.json`, raw output when retained, and verdict input slices when compact verdict specs are used. It treats financial/news/market-status text stages as optional group failures. Do not use `multi_agent_v1.spawn_agent` for daily-trading stage delegation.

For verdict stages, prefer compact specs with `artifact_paths` and `symbol_ids` instead of long handwritten prompts. The launcher builds the standard prompt from those fields and writes per-task `verdict-inputs/` slices containing only the listed symbols.

Supported sub-agent stages:

- `financial-collection`: `$collect-financial-information`; text output is a cache path or fixed missing-cache message. Skip this sub-agent when a valid same-date cache for the full symbol universe is already available.
- `news-collection`: `$collect-news-information`; text output is a cache path or fixed missing-cache message. Skip this sub-agent when a valid same-date cache for the full symbol universe is already available.
- `market-status-collection`: `$get-market-status`; text output is concise Markdown for S&P 500, Nasdaq, Dow, KOSPI, and KOSDAQ.
- `first-verdict`: selected four personas: `analyst-blackrock`, `analyst-fidelity`, `analyst-jpmorgan`, `analyst-morganstanley`. `analyst-fidelity` also carries the former quality/value/momentum/low-volatility factor checks from `analyst-statestreet`.
- `second-verdict`: `judge-midterm` only. Retry the `judge-midterm` task at most two times when the required output is missing or unusable.

## Run Identity

Preserve injected `run_id` and `started_at`. If absent, generate both before work starts, create `reports/runs/<run_id>/run.json`, and use that run directory for every artifact. When this skill is actually used, include this final line:

```text
작업 시작: YYYY-MM-DD HH:MM:SS KST
```

## Authority Boundaries

Main agent only:

- Resolve the portfolio universe from `$check-portfolio` JSON `universe`; do not separately re-read live holdings only to expand the universe.
- Handle auth, account snapshots, price/chart KIS MCP calls, artifact writes, sanitization, merging, exclusions, target/order calculation, explicit approval checks, active pending/reserved order adjustment, and order submission.
- Write canonical artifacts under `reports/runs/<run_id>/`.

Collection sub-agents:

- Receive the complete symbol universe, but may only collect their assigned optional domain.
- Never call account, balance, order, order-available, fill-history, pending-order, reservation-order, correction, or cancellation APIs.

Verdict sub-agents:

- Use only supplied snapshots, `decision-brief.json`, `verdict-first.json` when applicable, and their persona.
- May use read-only local shell commands such as `cat` and `jq` only for explicitly listed artifact/persona/rule files.
- Do not call KIS, MCP, web, network, account/order APIs, or external data sources.
- Return compact JSON only. They must not write files, create Markdown sidecars, emit diffs, or include code fences.
- Include the informational `human_markdown_path`; the Main agent creates human-review Markdown sidecars from parsed JSON.

## Execution Flow

1. Initialize run identity and auth handling.
2. Build the complete portfolio universe from `$check-portfolio` JSON `universe`, which already includes `recommanded`, `specified`, and direct KIS `holding` symbols.
3. Main agent collects required price/chart evidence once and writes `price-chart.json`.
4. Reuse valid same-date financial/news caches when they cover the full symbol universe; otherwise run missing optional `financial`, `news`, and `market-status` collection in parallel through the launcher.
5. Merge required price/chart and account evidence plus short optional summaries into compact `decision-brief.json`.
6. Run the selected four `first-verdict` personas in parallel with the same immutable brief, then have the Main agent create human-review sidecars from parsed JSON.
7. Build `second-verdict` set from eligible symbols with `final_first_score >= 7` plus eligible `holding` symbols from `$check-portfolio`, then run only `judge-midterm` with at most two retries. Use the single valid judge target as the canonical target after deterministic validation.
8. Refresh account state before orders, validate the single target set, reconcile active pending/reserved orders, apply `trade-execution.md` gates, and submit only explicitly authorized adjustments or orders.
9. Write `execution.json`, final report, and final `run.json` status.

## Compact Prompt Rules

- Sub-agent prompts should name the stage, role, run paths, required output, one relevant persona/rule reference, and the compact artifact paths.
- Verdict sub-agents may use read-only local shell commands such as `cat` and `jq` only for explicitly listed artifact/persona/rule files. KIS, MCP, web, network, account/order APIs, file writes, raw caches, secrets, and unlisted paths remain forbidden.
- Verdict compact specs must pass `artifact_paths.decision_brief` and `symbol_ids`; they may pass `artifact_paths.verdict_first`, `artifact_paths.persona`, and `artifact_paths.verdict_format`.
- Verdict compact specs must omit `prompt`; if `prompt` is non-empty, the launcher treats the spec as a backward-compatible raw prompt spec even when artifact metadata is present.
- Pass artifact paths and short excerpts, not full JSON, unless the sub-agent must score that exact compact JSON.
- `decision-brief.json` is the only verdict input; keep it compact as defined in `data-collection.md`.
- Do not repeat auth, order, sanitization, optional-stage, or schema rules in every prompt. Cite the relevant rule file instead.
- Verdict output must be compact JSON with short `reason_code` and `one_line_reason` fields instead of long rationale, risk, or evidence arrays.
- Missing financial/news/market-status data stays optional and must not become a verdict or order blocker by prompt wording.

## Failure Rules

- Required price/chart, account, verdict, and explicitly requested order gates fail closed according to the rule docs.
- If `market_status` from `$check-holiday` or scheduler context is supplied, order execution requires `open`; `closed` or `unknown` blocks order submission.
- Financial/news/market-status failures are visible but non-blocking when price/chart and account gates pass.
- A failed required wrapper remains evidence; write the failed canonical envelope and stop only the dependent stages.
- Sensitive values must be removed before artifacts, prompts, reports, or user responses.
