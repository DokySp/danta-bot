import os
import uuid
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from .errors import UserFacingError

KST = ZoneInfo("Asia/Seoul")

DAILY_TRADING_STAGE_MODEL_CONTRACT = (
    "\n\n[daily-trading stage model contract]\n"
    "- Routine daily-trading execution must start with the pipeline, not manual helper/launcher orchestration: "
    "`python3 /app/skills/daily-trading/scripts/run_daily_trading_pipeline.py run "
    "--workspace-dir /workspace --output-dir reports/runs/<run_id> --run-id <run_id> "
    "--started-at <started_at> --env <acct|paper> --request-type <analysis|prepare|demo-submit|real-submit> [--submit-orders] [--order-path <reservation|immediate>]`. "
    "Use CODEX_MCP_TRADING_ENV to choose acct/paper unless the explicit request requires a narrower mode.\n"
    "- After the pipeline returns, use `reports/runs/<run_id>/telegram-summary.txt` as the Telegram/user-facing "
    "response when it exists. It is rendered deterministically from `pipeline-summary.json`. Open `pipeline-command-log.json`, raw wrappers, `decision-brief.json`, "
    "`verdict-first.json`, `verdict-second.json`, account snapshots, or rule/persona files only when the pipeline "
    "failed and the compact summary is insufficient to diagnose the failed stage.\n"
    "- The routine Telegram/user-facing response must not be hand-written from raw artifacts. The fixed renderer "
    "`scripts/render_telegram_summary.py` reads `pipeline-summary.json` and writes `telegram-summary.txt`; send that "
    "string unchanged except for the standard `작업 시작` line added by the service runner.\n"
    "- For `analysis` or `prepare` requests, stop after the pipeline summary and do not manually rebuild the "
    "report from intermediate artifacts. For explicitly authorized `demo-submit` or `real-submit` requests, pass "
    "`--submit-orders` with the pipeline command so `scripts/execute_orders.py` refreshes read-only gates, reconciles "
    "active pending/reserved orders, and submits/corrects/cancels or blocks orders before summary generation. Use "
    "`--order-path reservation` for reservation orders and `--order-path immediate` for immediate cash orders. If `--submit-orders` "
    "was not used and `execution.requires_main_agent_order_execution=true` remains, treat that as a non-submitting "
    "gate summary rather than a final submitted/blocked execution result. Do not reread verdict inputs or rebuild "
    "scoring just to submit orders. For explicit limit reservation requests, treat the deterministic execution-plan `order_price` "
    "values as the default limit price candidates when the user did not provide per-symbol limit prices; do not "
    "require the user to type the same prices again, and block only if a candidate price is missing, invalid, stale, "
    "unsupported by the current order API detail, or inconsistent with the refreshed order-available response.\n"
    "- `--submit-orders` runs regenerate `pipeline-summary.json` from the final `execution.json`; read the refreshed "
    "`pipeline-summary.json` before the final Telegram/user-facing response.\n"
    "- Treat `/app/skills/daily-trading/scripts/run_subagent.py` as the verified launcher interface only for "
    "focused retry/debug of a failed pipeline stage. "
    "Do not read or paste the launcher source, persona files, or `references/rules/*.md` in full just to run "
    "collection/verdict stages. "
    "If the skill path differs between host/container installs, resolve the installed skill path and do not treat "
    "that path difference as a trading failure.\n"
    "- Pipeline verification command, for install/change validation only: "
    "`python3 <daily-trading-skill>/scripts/run_daily_trading_pipeline.py self-test`. "
    "Launcher verification command, for install/change validation only: "
    "`python3 <daily-trading-skill>/scripts/run_subagent.py self-test`.\n"
    "- Focused fallback first-verdict command: "
    "`python3 <daily-trading-skill>/scripts/run_subagent.py run-group --spec reports/runs/<run_id>/first-verdict-specs.json --max-workers 3`.\n"
    "- Focused fallback second-verdict command: "
    "`python3 <daily-trading-skill>/scripts/run_subagent.py run-one --spec reports/runs/<run_id>/second-verdict-spec.json`.\n"
    "- collection/verdict sub-agent는 multi_agent_v1.spawn_agent가 아니라 "
    "`/app/skills/daily-trading/scripts/run_subagent.py`로 실행한다.\n"
    "- launcher spec에는 run_id, started_at, stage, agent_role, task_name, workspace_dir, output_dir를 넣는다. "
    "collection stage는 prompt를 쓸 수 있지만 verdict stage는 artifact_paths와 symbol_ids만 사용한다.\n"
    "- launcher는 `codex exec -m <model> -c model_reasoning_effort=\"<effort>\"`를 사용하며 "
    "parent와 같은 CODEX_HOME, CODEX_MCP_TRADING_ENV, workspace cwd, sandbox bypass 설정을 상속한다.\n"
    "- launcher는 `reports/runs/<run_id>/subagents/<task_name>.wrapper.json`와 raw text만 남기며 "
    "wrapper에는 spec_fingerprint와 token_usage를 기록한다. "
    "Main agent만 JSON 단계의 parsed_json 또는 financial/news 단계의 parsed_text를 sanitize한 뒤 canonical artifact와 verdict Markdown sidecar를 작성한다.\n"
    "- collection sub-agent: model=gpt-5.4-mini, effort=low.\n"
    "- first-verdict sub-agent: model=gpt-5.5, effort=medium, selected 3 functional analysts only "
    "(analyst-quality-value, analyst-momentum-cycle, analyst-risk-allocation).\n"
    "- second-verdict sub-agent: model=gpt-5.5, effort=medium, judge-final only with at most 2 retries.\n"
    "- verdict sub-agent 입력은 canonical decision-brief.json을 직접 넘기지 않고 launcher가 만든 verdict-core slice를 사용한다.\n"
    "- second-verdict 입력은 verdict-first.json 전체가 아니라 launcher가 만든 selected-symbol first-verdict slice를 사용한다.\n"
    "- verdict sub-agents return compact JSON only; they do not write Markdown, emit diffs, or use long rationale/risk arrays.\n"
    "- verdict stage에서 raw prompt fallback은 금지한다. 실패 retry도 compact artifact_paths/symbol_ids spec만 사용한다.\n"
    "- sub-agent retry는 실패한 task만 수행한다. spec_fingerprint가 같은 성공 wrapper는 재사용한다.\n"
    "- financial/news는 같은 날짜 full-universe cache hit이면 cache path만 사용한다. cache miss 또는 universe mismatch면 "
    "helper get을 확인하고 collector를 한 번만 실행한 뒤 helper get으로 cache path를 다시 확인한다. 그래도 없으면 해당 optional domain 없이 진행하고, "
    "미완성 cache가 있으면 partial cache path를 decision-brief에 넘긴다. 같은 pipeline run에서 추가 재시도는 하지 않는다.\n"
    "- Main agent initialize, account snapshots, merge-and-brief, report: model=gpt-5.5, effort=medium. Routine order-execution is handled by `scripts/execute_orders.py`.\n"
)


def mcp_trading_env_prompt(mcp_trading_env: str) -> str:
    if mcp_trading_env == "paper":
        env_dv = "demo"
        mode_text = "모의투자/모의거래"
    elif mcp_trading_env == "acct":
        env_dv = "real"
        mode_text = "실전 계좌"
    else:
        raise ValueError(f"unsupported CODEX_MCP_TRADING_ENV={mcp_trading_env}")

    return (
        "\n\n[KIS MCP 거래환경]\n"
        f"- CODEX_MCP_TRADING_ENV={mcp_trading_env} ({mode_text}).\n"
        "- 이 설정은 사용자 요청, 스케줄 메시지, 스킬 문서의 모의/실전 표현보다 우선한다.\n"
        f"- 한국투자증권 MCP 도구 호출에서 env_dv 파라미터가 있으면 반드시 env_dv=\"{env_dv}\"를 사용한다.\n"
    )


@dataclass(frozen=True)
class CodexRunContext:
    run_id: str
    started_at: str
    started_at_display: str


def new_codex_run_context() -> CodexRunContext:
    started_at = datetime.now(KST)
    return CodexRunContext(
        run_id=started_at.strftime("%Y%m%dT%H%M%S%z") + "-" + uuid.uuid4().hex[:8],
        started_at=started_at.isoformat(timespec="seconds"),
        started_at_display=started_at.strftime("%Y-%m-%d %H:%M:%S KST"),
    )


def codex_run_context_prompt(context: CodexRunContext) -> str:
    return (
        "\n\n[Codex 실행 메타데이터]\n"
        f"- run_id={context.run_id}\n"
        f"- started_at={context.started_at}\n"
        "- daily-trading을 실제 사용하면 이 값을 변경하지 말고 "
        "reports/runs/<run_id>/ 아티팩트와 최종 작업 시작 시각에 사용한다.\n"
        "- daily-trading을 실제 사용하지 않으면 최종 응답에 작업 시작 시각을 표시하지 않는다.\n"
    )


def daily_trading_model_contract_prompt() -> str:
    return DAILY_TRADING_STAGE_MODEL_CONTRACT


def is_explicit_daily_trading_request(prompt: str) -> bool:
    return "$daily-trading" in prompt or "$execute-trade" in prompt


def is_daily_trading_schedule(job_id: str) -> bool:
    return job_id == "pre-open" or job_id.startswith("daily-")


def append_daily_trading_started_at(text: str, context: CodexRunContext) -> str:
    line = f"작업 시작: {context.started_at_display}"
    if line in text:
        return text
    return f"{text.rstrip()}\n\n{line}"


def attach_daily_trading_context(exc: Exception, context: CodexRunContext) -> None:
    setattr(exc, "daily_trading_run_context", context)


def error_message_with_run_context(exc: Exception, fallback: str) -> str:
    message = exc.html_message if isinstance(exc, UserFacingError) else fallback
    context = getattr(exc, "daily_trading_run_context", None)
    if isinstance(context, CodexRunContext):
        return append_daily_trading_started_at(message, context)
    return message
