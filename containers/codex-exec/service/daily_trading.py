import os
import uuid
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from .errors import UserFacingError

KST = ZoneInfo("Asia/Seoul")

DAILY_TRADING_STAGE_MODEL_CONTRACT = (
    "\n\n[daily-trading stage model contract]\n"
    "- collection/verdict sub-agent는 multi_agent_v1.spawn_agent가 아니라 "
    "`/app/skills/daily-trading/scripts/run_subagent.py`로 실행한다.\n"
    "- launcher spec에는 run_id, started_at, stage, agent_role, task_name, workspace_dir, output_dir를 넣는다. "
    "collection stage는 prompt를 쓸 수 있지만 verdict stage는 artifact_paths와 symbol_ids만 사용한다.\n"
    "- launcher는 `codex exec -m <model> -c model_reasoning_effort=\"<effort>\"`를 사용하며 "
    "parent와 같은 CODEX_HOME, CODEX_MCP_TRADING_ENV, workspace cwd, sandbox bypass 설정을 상속한다.\n"
    "- launcher는 `reports/runs/<run_id>/subagents/<task_name>.wrapper.json`와 raw text만 남기며 "
    "wrapper에는 spec_fingerprint와 token_usage를 기록한다. "
    "Main agent만 JSON 단계의 parsed_json 또는 financial/news/market-status 단계의 parsed_text를 sanitize한 뒤 canonical artifact와 verdict Markdown sidecar를 작성한다.\n"
    "- collection sub-agent: model=gpt-5.4-mini, effort=low.\n"
    "- first-verdict sub-agent: model=gpt-5.5, effort=medium, selected 3 functional analysts only "
    "(analyst-quality-value, analyst-momentum-cycle, analyst-risk-allocation).\n"
    "- second-verdict sub-agent: model=gpt-5.5, effort=medium, judge-midterm only with at most 2 retries.\n"
    "- verdict sub-agent 입력은 canonical decision-brief.json을 직접 넘기지 않고 launcher가 만든 verdict-core slice를 사용한다.\n"
    "- second-verdict 입력은 verdict-first.json 전체가 아니라 launcher가 만든 selected-symbol first-verdict slice를 사용한다.\n"
    "- verdict sub-agents return compact JSON only; they do not write Markdown, emit diffs, or use long rationale/risk arrays.\n"
    "- verdict stage에서 raw prompt fallback은 금지한다. 실패 retry도 compact artifact_paths/symbol_ids spec만 사용한다.\n"
    "- sub-agent retry는 실패한 task만 수행한다. spec_fingerprint가 같은 성공 wrapper는 재사용한다.\n"
    "- financial/news는 같은 날짜 full-universe cache hit이면 collector를 실행하지 말고, cache miss 또는 universe mismatch일 때만 수집한다.\n"
    "- Main agent initialize, account snapshots, merge-and-brief, order-execution, report: model=gpt-5.5, effort=medium.\n"
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
