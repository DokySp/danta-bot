#!/usr/bin/env python3
import html
import json
import logging
import os
import queue
import shutil
import shlex
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import yaml


HTML_PROMPT_SUFFIX = (
    "\n\n결과는 telegram으로 보낼거기 때문에 마크다운이 아니라 "
    "parse_mode=HTML에 맞춰서 출력해줘. Telegram HTML에서 지원되는 "
    "<b>, <i>, <u>, <s>, <code>, <pre>, <a> 태그 위주로 사용하고 "
    "전체 메시지는 가능한 4096자 안쪽으로 요약해줘."
)

MCP_TRADING_ENV_VALUES = {"paper", "acct"}
KST = ZoneInfo("Asia/Seoul")
DAILY_TRADING_STAGE_MODEL_CONTRACT = (
    "\n\n[daily-trading stage model contract]\n"
    "- daily-trading을 실제 사용하면 아래 stage별 model/effort는 권장값이 아니라 필수값이다.\n"
    "- collect-account-state, market, financial, news: model=gpt-5.3-codex-spark, effort=low.\n"
    "- first-verdict analyst/juror: model=gpt-5.5, effort=low.\n"
    "- second-verdict judge: model=gpt-5.5, effort=high.\n"
    "- final-risk-verdict: model=gpt-5.5, effort=high.\n"
    "- Main agent initialize, merge-and-brief, execution, report: model=gpt-5.5, effort=medium.\n"
    "- stage-metrics.json의 모든 metrics 항목에는 recommended_model, recommended_effort, "
    "actual_model, actual_effort를 기록한다.\n"
    "- actual_model 또는 actual_effort가 필수값과 다르면 run validation failed로 처리된다.\n"
)
VALIDATION_SUMMARY_LIMIT = 8


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def required_env_bool(name: str) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        raise ValueError(f"{name} is required")
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "y", "on"}:
        return True
    if value in {"0", "false", "no", "n", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return int(raw)


def env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return float(raw)


def env_choice(name: str, default: str, allowed: set[str]) -> str:
    raw = os.getenv(name)
    value = default if raw is None or raw.strip() == "" else raw.strip().lower()
    if value not in allowed:
        allowed_text = ", ".join(sorted(allowed))
        raise ValueError(f"{name} must be one of: {allowed_text}")
    return value


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


@dataclass(frozen=True)
class DailyTradingValidation:
    passed: bool
    summary_lines: list[str]
    raw: dict[str, Any]


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


def append_daily_trading_validation_summary(text: str, validation: DailyTradingValidation) -> str:
    if validation.passed:
        return text
    lines = validation.summary_lines[:VALIDATION_SUMMARY_LIMIT]
    if not lines:
        lines = ["validator failed without a summary"]
    summary = "\n".join(lines)
    return f"{text.rstrip()}\n\n<b>daily-trading validation failed</b>\n<pre>{html.escape(summary)}</pre>"


def attach_daily_trading_context(exc: Exception, context: CodexRunContext) -> None:
    setattr(exc, "daily_trading_run_context", context)


def attach_daily_trading_validation(exc: Exception, validation: DailyTradingValidation | None) -> None:
    if validation:
        setattr(exc, "daily_trading_validation", validation)


def error_message_with_run_context(exc: Exception, fallback: str) -> str:
    message = exc.html_message if isinstance(exc, UserFacingError) else fallback
    validation = getattr(exc, "daily_trading_validation", None)
    if isinstance(validation, DailyTradingValidation):
        message = append_daily_trading_validation_summary(message, validation)
    context = getattr(exc, "daily_trading_run_context", None)
    if isinstance(context, CodexRunContext):
        return append_daily_trading_started_at(message, context)
    return message


ERROR_LOG_LIMIT = 2000


class UserFacingError(RuntimeError):
    def __init__(self, log_message: str, html_message: str) -> None:
        super().__init__(log_message)
        self.html_message = html_message


class CodexAuthError(UserFacingError):
    def __init__(self) -> None:
        super().__init__(
            "codex authentication failed",
            "Codex 로그인이 되어있지 않거나 API 키가 설정되지 않았습니다.\n"
            "컨테이너에서 <code>codex login</code>을 먼저 실행하거나 "
            "<code>OPENAI_API_KEY</code> 설정을 확인해주세요.",
        )


class CodexUsageLimitError(UserFacingError):
    def __init__(self, log_excerpt: str) -> None:
        log_block = f"\n<pre>{html.escape(log_excerpt)}</pre>" if log_excerpt else ""
        super().__init__(
            "codex usage limit reached",
            "<b>Codex 사용 한도에 도달했습니다.</b>\n"
            "사용 가능 시간이 지나면 다시 시도하거나 Codex 사용량/크레딧 설정을 확인해주세요."
            f"{log_block}",
        )


class UnknownCodexError(UserFacingError):
    def __init__(self, returncode: int, log_excerpt: str) -> None:
        log_block = html.escape(log_excerpt or "no stderr/stdout captured")
        super().__init__(
            f"codex exited with {returncode}",
            "<b>알 수 없는 에러가 발생했습니다.</b>\n"
            f"<code>exit_code={returncode}</code>\n"
            f"<pre>{log_block}</pre>",
        )


def classify_codex_error(returncode: int, stdout: str, stderr: str) -> UserFacingError:
    log_excerpt = codex_error_log(stdout, stderr)
    if is_codex_usage_limit_error(log_excerpt):
        return CodexUsageLimitError(log_excerpt)
    if is_codex_auth_error(log_excerpt):
        return CodexAuthError()
    return UnknownCodexError(returncode, log_excerpt)


def codex_error_log(stdout: str, stderr: str) -> str:
    parts = []
    if stderr.strip():
        parts.append(stderr.strip())
    if stdout.strip():
        parts.append(stdout.strip())
    combined = "\n".join(parts).strip()
    return combined[-ERROR_LOG_LIMIT:] if combined else ""


def is_codex_usage_limit_error(text: str) -> bool:
    lowered = text.lower()
    return (
        "you've hit your usage limit" in lowered
        or "you have hit your usage limit" in lowered
        or ("usage limit" in lowered and "try again" in lowered)
        or ("purchase more credits" in lowered and "try again" in lowered)
    )


def is_codex_auth_error(text: str) -> bool:
    text = text.lower()
    return (
        "401 unauthorized" in text
        or "missing bearer or basic authentication" in text
        or ("unauthorized" in text and "api.openai.com/v1/responses" in text)
    )


@dataclass(frozen=True)
class Config:
    host: str
    port: int
    codex_bin: str
    codex_home: Path
    state_dir: Path
    workspace_dir: Path
    schedule_file: Path
    telegram_gateway_url: str
    telegram_route: str | None
    mcp_trading_env: str
    model: str
    reasoning_effort: str
    codex_timeout_seconds: int
    scheduler_poll_seconds: int
    telegram_typing_interval_seconds: float
    bypass_sandbox: bool
    new_session_prompt: str
    usage_script: Path
    usage_timeout_seconds: int
    bundled_skills_dir: Path
    sync_skills_overwrite: bool
    daily_trading_validator: Path

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            host=os.getenv("CODEX_EXEC_HOST", "0.0.0.0"),
            port=env_int("CODEX_EXEC_PORT", 8080),
            codex_bin=os.getenv("CODEX_BIN", "codex"),
            codex_home=Path(os.getenv("CODEX_HOME", "/codex-home")),
            state_dir=Path(os.getenv("STATE_DIR", "/state")),
            workspace_dir=Path(os.getenv("WORKSPACE_DIR", "/workspace")),
            schedule_file=Path(os.getenv("SCHEDULE_FILE", "/app/config/schedules.yaml")),
            telegram_gateway_url=os.getenv(
                "TELEGRAM_GATEWAY_URL",
                "http://telegram-gateway:8080/sendMessage",
            ),
            telegram_route=os.getenv("TELEGRAM_ROUTE", "").strip() or None,
            mcp_trading_env=env_choice(
                "CODEX_MCP_TRADING_ENV",
                "paper",
                MCP_TRADING_ENV_VALUES,
            ),
            model=os.getenv("CODEX_MODEL", "gpt-5.5"),
            reasoning_effort=os.getenv("CODEX_REASONING_EFFORT", "medium"),
            codex_timeout_seconds=env_int("CODEX_TIMEOUT_SECONDS", 1800),
            scheduler_poll_seconds=env_int("SCHEDULER_POLL_SECONDS", 15),
            telegram_typing_interval_seconds=env_float("TELEGRAM_TYPING_INTERVAL_SECONDS", 4.0),
            bypass_sandbox=env_bool("CODEX_BYPASS_APPROVALS_AND_SANDBOX", True),
            new_session_prompt=os.getenv("NEW_SESSION_PROMPT", "새 대화 시작"),
            usage_script=Path(os.getenv("CODEX_USAGE_SCRIPT", "/app/codex_usage")),
            usage_timeout_seconds=env_int("CODEX_USAGE_TIMEOUT_SECONDS", 20),
            bundled_skills_dir=Path(os.getenv("BUNDLED_SKILLS_DIR", "/app/skills")),
            sync_skills_overwrite=required_env_bool("CODEX_SYNC_SKILLS_OVERWRITE"),
            daily_trading_validator=Path(
                os.getenv(
                    "DAILY_TRADING_VALIDATOR",
                    "/app/skills/daily-trading/scripts/validate_run.py",
                )
            ),
        )


class StateStore:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.path = config.state_dir / "default_session.json"
        self.lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def get_default_session(self) -> str | None:
        with self.lock:
            if not self.path.exists():
                return None
            try:
                data = json.loads(self.path.read_text())
            except (OSError, json.JSONDecodeError):
                logging.exception("failed to read session state")
                return None
            value = data.get("session_id")
            return str(value) if value else None

    def set_default_session(self, session_id: str) -> None:
        payload = {
            "session_id": session_id,
            "updated_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        }
        tmp = self.path.with_suffix(".json.tmp")
        with self.lock:
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
            tmp.replace(self.path)


class TelegramGateway:
    def __init__(self, config: Config) -> None:
        self.config = config

    def send_message(
        self,
        text: str,
        chat_id: str | None = None,
        route: str | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "text": text or "<i>No output</i>",
            "parse_mode": "HTML",
            "escape": False,
        }
        if chat_id:
            payload["chat_id"] = chat_id
        outbound_route = route or self.config.telegram_route
        if outbound_route:
            payload["route"] = outbound_route

        body = json.dumps(payload).encode("utf-8")
        request = Request(
            self.config.telegram_gateway_url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=20) as response:
                response.read()
        except HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"telegram-gateway failed: HTTP {exc.code}: {raw}") from exc
        except URLError as exc:
            raise RuntimeError(f"telegram-gateway failed: {exc}") from exc

    def send_chat_action(
        self,
        chat_id: str | None,
        route: str | None = None,
        action: str = "typing",
    ) -> None:
        outbound_route = route or self.config.telegram_route
        if not outbound_route:
            logging.debug("telegram chat action skipped because route is missing")
            return

        payload: dict[str, Any] = {
            "route": outbound_route,
            "action": action,
        }
        if chat_id:
            payload["chat_id"] = chat_id
        body = json.dumps(payload).encode("utf-8")
        request = Request(
            self._gateway_url("/sendChatAction"),
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=10) as response:
                response.read()
        except HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"telegram-gateway chat action failed: HTTP {exc.code}: {raw}") from exc
        except URLError as exc:
            raise RuntimeError(f"telegram-gateway chat action failed: {exc}") from exc

    def _gateway_url(self, endpoint: str) -> str:
        base = self.config.telegram_gateway_url.rstrip("/")
        if base.endswith("/sendMessage"):
            return base.rsplit("/", 1)[0] + endpoint
        return base + endpoint


class TypingIndicator:
    def __init__(
        self,
        gateway: TelegramGateway,
        chat_id: str | None,
        route: str | None,
        interval_seconds: float,
    ) -> None:
        self.gateway = gateway
        self.chat_id = chat_id
        self.route = route
        self.interval_seconds = max(1.0, interval_seconds)
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None

    def __enter__(self) -> "TypingIndicator":
        if self.chat_id or self.route or self.gateway.config.telegram_route:
            self.thread = threading.Thread(target=self._loop, name="telegram-typing", daemon=True)
            self.thread.start()
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=1)

    def _loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.gateway.send_chat_action(self.chat_id, self.route, "typing")
            except Exception:
                logging.exception("failed to send telegram typing action")
            self.stop_event.wait(self.interval_seconds)


class CodexRunner:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.tmp_dir = Path(os.getenv("CODEX_EXEC_TMP_DIR", "/tmp/codex-exec"))
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        self.config.codex_home.mkdir(parents=True, exist_ok=True)
        self._sync_bundled_skills()

    def run_new_session(self, prompt: str) -> tuple[str, str]:
        before = self._session_ids()
        output = self._run_codex(["exec"], prompt)
        session_id = self._detect_new_session_id(before)
        if not session_id:
            raise RuntimeError("codex finished but new session id was not found")
        return session_id, output

    def run_resume(self, session_id: str, prompt: str) -> str:
        return self._run_codex(["exec", "resume", session_id], prompt)

    def run_once(self, prompt: str, daily_trading_hint: bool = False) -> str:
        return self._run_codex(["exec"], prompt, daily_trading_hint=daily_trading_hint)

    def run_usage(self) -> str:
        if not self.config.usage_script.exists():
            raise RuntimeError(f"codex usage script not found: {self.config.usage_script}")

        cmd = [
            str(self.config.usage_script),
            "--timeout",
            str(self.config.usage_timeout_seconds),
        ]
        env = os.environ.copy()
        env["CODEX_HOME"] = str(self.config.codex_home)
        env["CODEX_BIN"] = self.config.codex_bin

        logging.info("running codex usage command=%s", " ".join(shlex.quote(part) for part in cmd))
        result = subprocess.run(
            cmd,
            cwd=self.config.workspace_dir,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=self.config.usage_timeout_seconds + 5,
            check=False,
        )
        if result.returncode != 0:
            raise classify_codex_error(result.returncode, result.stdout, result.stderr)

        output = result.stdout.strip()
        if not output:
            return "<i>Codex usage returned no output.</i>"
        if len(output) > 3500:
            output = "... truncated ...\n" + output[-3500:]
        return f"<b>Codex usage</b>\n<pre>{html.escape(output)}</pre>"

    def _build_prompt(self, prompt: str, context: CodexRunContext, daily_trading_hint: bool) -> str:
        daily_trading_prompt = daily_trading_model_contract_prompt() if daily_trading_hint else ""
        return (
            prompt.rstrip()
            + mcp_trading_env_prompt(self.config.mcp_trading_env)
            + codex_run_context_prompt(context)
            + daily_trading_prompt
            + HTML_PROMPT_SUFFIX
        )

    def _run_codex(
        self,
        subcommand: list[str],
        prompt: str,
        daily_trading_hint: bool = False,
    ) -> str:
        context = new_codex_run_context()
        output_file = self.tmp_dir / f"{context.run_id}.txt"
        daily_trading_hint = daily_trading_hint or is_explicit_daily_trading_request(prompt)
        full_prompt = self._build_prompt(prompt, context, daily_trading_hint)

        cmd = [
            self.config.codex_bin,
            *subcommand,
            "-m",
            self.config.model,
            "-c",
            f'model_reasoning_effort="{self.config.reasoning_effort}"',
            "--skip-git-repo-check",
            "-o",
            str(output_file),
        ]
        if self.config.bypass_sandbox:
            cmd.append("--dangerously-bypass-approvals-and-sandbox")
        cmd.append(full_prompt)

        env = os.environ.copy()
        env["CODEX_HOME"] = str(self.config.codex_home)
        env["CODEX_MCP_TRADING_ENV"] = self.config.mcp_trading_env

        logging.info("running codex command=%s", " ".join(shlex.quote(part) for part in cmd[:-1]))
        try:
            result = subprocess.run(
                cmd,
                cwd=self.config.workspace_dir,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self.config.codex_timeout_seconds,
                check=False,
            )
        except Exception as exc:
            if daily_trading_hint or self._daily_trading_artifact_exists(context):
                attach_daily_trading_validation(exc, self._validate_daily_trading_artifacts(context))
                attach_daily_trading_context(exc, context)
            raise

        if result.returncode != 0:
            exc = classify_codex_error(result.returncode, result.stdout, result.stderr)
            if daily_trading_hint or self._daily_trading_artifact_exists(context):
                attach_daily_trading_validation(exc, self._validate_daily_trading_artifacts(context))
                attach_daily_trading_context(exc, context)
            raise exc

        if output_file.exists():
            output = output_file.read_text()
        else:
            output = result.stdout.strip()
        output = output.strip() or "<i>Codex completed without output.</i>"
        if daily_trading_hint or self._daily_trading_artifact_exists(context):
            validation = self._validate_daily_trading_artifacts(context)
            if validation:
                output = append_daily_trading_validation_summary(output, validation)
            output = append_daily_trading_started_at(output, context)
        return output

    def _daily_trading_artifact_exists(self, context: CodexRunContext) -> bool:
        return (self.config.workspace_dir / "reports" / "runs" / context.run_id / "run.json").is_file()

    def _daily_trading_run_dir(self, context: CodexRunContext) -> Path:
        return self.config.workspace_dir / "reports" / "runs" / context.run_id

    def _validate_daily_trading_artifacts(self, context: CodexRunContext) -> DailyTradingValidation | None:
        run_dir = self._daily_trading_run_dir(context)
        if not run_dir.exists():
            return None

        validator = self.config.daily_trading_validator
        if not validator.exists():
            return DailyTradingValidation(
                passed=False,
                summary_lines=[f"daily-trading validator not found: {validator}"],
                raw={"status": "failed", "errors": [{"message": f"validator not found: {validator}"}]},
            )

        cmd = [
            "python3",
            str(validator),
            "--run-dir",
            str(run_dir),
            "--json",
            "--mark-run",
        ]
        logging.info("running daily-trading validator command=%s", " ".join(shlex.quote(part) for part in cmd))
        try:
            result = subprocess.run(
                cmd,
                cwd=self.config.workspace_dir,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
                check=False,
            )
        except Exception as exc:  # noqa: BLE001 - report validator failures to Telegram
            logging.exception("daily-trading validator crashed run_id=%s", context.run_id)
            return DailyTradingValidation(
                passed=False,
                summary_lines=[f"validator crashed: {exc}"],
                raw={"status": "failed", "errors": [{"message": str(exc)}]},
            )

        raw: dict[str, Any]
        try:
            parsed = json.loads(result.stdout or "{}")
            raw = parsed if isinstance(parsed, dict) else {"status": "failed", "errors": []}
        except json.JSONDecodeError:
            raw = {
                "status": "failed",
                "errors": [
                    {
                        "code": "validator_output_not_json",
                        "message": (result.stderr or result.stdout or "validator returned no output").strip(),
                    }
                ],
            }

        errors = raw.get("errors") if isinstance(raw.get("errors"), list) else []
        summary_lines = []
        for item in errors[:VALIDATION_SUMMARY_LIMIT]:
            if isinstance(item, dict):
                code = str(item.get("code", "validation_error"))
                message = str(item.get("message", ""))
                artifact = str(item.get("artifact", "")).strip()
                prefix = f"{artifact}: " if artifact else ""
                summary_lines.append(f"{prefix}{code}: {message}")
            else:
                summary_lines.append(str(item))

        if result.returncode != 0 and not summary_lines:
            summary_lines.append((result.stderr or "validator failed without details").strip())

        passed = result.returncode == 0 and str(raw.get("status", "")).lower() == "passed"
        return DailyTradingValidation(passed=passed, summary_lines=summary_lines, raw=raw)

    def _session_ids(self) -> list[str]:
        ids: list[str] = []
        index_path = self.config.codex_home / "session_index.jsonl"
        if index_path.exists():
            ids.extend(self._session_ids_from_jsonl(index_path))

        sessions_root = self.config.codex_home / "sessions"
        if sessions_root.exists():
            session_files = sorted(
                sessions_root.rglob("*.jsonl"),
                key=lambda path: (path.stat().st_mtime_ns, str(path)),
            )
            for path in session_files:
                session_id = self._session_id_from_session_file(path)
                if session_id:
                    ids.append(session_id)

        seen: set[str] = set()
        unique_ids: list[str] = []
        for session_id in ids:
            if session_id in seen:
                continue
            seen.add(session_id)
            unique_ids.append(session_id)
        return unique_ids

    def _session_ids_from_jsonl(self, path: Path) -> list[str]:
        ids: list[str] = []
        for line in path.read_text(errors="replace").splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            value = item.get("id")
            if value:
                ids.append(str(value))
        return ids

    def _session_id_from_session_file(self, path: Path) -> str | None:
        try:
            lines = path.read_text(errors="replace").splitlines()
        except OSError:
            logging.exception("failed to read codex session file path=%s", path)
            return None

        for line in lines[:20]:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if item.get("type") != "session_meta":
                continue
            payload = item.get("payload")
            if not isinstance(payload, dict):
                continue
            value = payload.get("id")
            if value:
                return str(value)

        stem = path.stem
        if "-" not in stem:
            return None
        candidate = stem.rsplit("-", 5)[-5:]
        return "-".join(candidate) if len(candidate) == 5 else None

    def _detect_new_session_id(self, before: list[str]) -> str | None:
        after = self._session_ids()
        before_set = set(before)
        created = [session_id for session_id in after if session_id not in before_set]
        if created:
            return created[-1]
        if after and (not before or after[-1] != before[-1]):
            return after[-1]
        return None

    def _sync_bundled_skills(self) -> None:
        source = self.config.bundled_skills_dir
        if not source.exists():
            logging.info("bundled skills dir does not exist: %s", source)
            return

        target_root = self.config.codex_home / "skills"
        marker = self.config.codex_home / ".bundled_skills_initialized"

        target_root.mkdir(parents=True, exist_ok=True)

        copied = 0
        replaced = 0
        skipped = 0
        for skill_dir in sorted(path for path in source.iterdir() if path.is_dir()):
            target = target_root / skill_dir.name
            if (target.exists() or target.is_symlink()) and self.config.sync_skills_overwrite:
                self._remove_existing_skill(target)
                replaced += 1
            if target.exists() or target.is_symlink():
                skipped += 1
                continue
            shutil.copytree(skill_dir, target)
            copied += 1

        self._write_skills_marker(marker, copied=copied, replaced=replaced, skipped=skipped)

        logging.info(
            "synced bundled skills copied=%s replaced_existing=%s skipped_existing=%s source=%s target=%s",
            copied,
            replaced,
            skipped,
            source,
            target_root,
        )

    @staticmethod
    def _remove_existing_skill(path: Path) -> None:
        if path.is_symlink() or not path.is_dir():
            path.unlink()
            return
        shutil.rmtree(path)

    def _write_skills_marker(self, marker: Path, copied: int, replaced: int, skipped: int) -> None:
        payload = {
            "source": str(self.config.bundled_skills_dir),
            "target": str(self.config.codex_home / "skills"),
            "copied": copied,
            "replaced_existing": replaced,
            "skipped_existing": skipped,
            "synced_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        }
        marker.write_text(json.dumps(payload, ensure_ascii=False, indent=2))


@dataclass(frozen=True)
class TelegramTask:
    chat_id: str | None
    text: str
    route: str | None = None
    message_id: Any = None


class TelegramWorker:
    def __init__(self, config: Config, state: StateStore, runner: CodexRunner, gateway: TelegramGateway) -> None:
        self.config = config
        self.state = state
        self.runner = runner
        self.gateway = gateway
        self.queue: queue.Queue[TelegramTask] = queue.Queue()
        self.thread = threading.Thread(target=self._work, name="telegram-worker", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def submit(self, task: TelegramTask) -> None:
        self.queue.put(task)

    def _work(self) -> None:
        while True:
            task = self.queue.get()
            try:
                self._handle(task)
            except Exception as exc:  # noqa: BLE001 - report task failures to Telegram
                if isinstance(exc, UserFacingError):
                    logging.warning("telegram task failed: %s", exc)
                else:
                    logging.exception("telegram task failed")
                self.gateway.send_message(self._error_message(exc), task.chat_id, task.route)
            finally:
                self.queue.task_done()

    def _handle(self, task: TelegramTask) -> None:
        text = task.text.strip()
        logging.info("handling telegram task message_id=%s text=%r", task.message_id, text)

        if text.startswith("/new "):
            self.gateway.send_message(
                "사용법: <code>/new</code>\n새 세션 생성 명령은 메시지를 함께 받지 않습니다.",
                task.chat_id,
                task.route,
            )
            return

        if text == "/new":
            with TypingIndicator(
                self.gateway,
                task.chat_id,
                task.route,
                self.config.telegram_typing_interval_seconds,
            ):
                session_id, output = self.runner.run_new_session(self.config.new_session_prompt)
            self.state.set_default_session(session_id)
            logging.info("new default session_id=%s", session_id)
            self.gateway.send_message(output, task.chat_id, task.route)
            return

        if text == "/usage":
            with TypingIndicator(
                self.gateway,
                task.chat_id,
                task.route,
                self.config.telegram_typing_interval_seconds,
            ):
                output = self.runner.run_usage()
            self.gateway.send_message(output, task.chat_id, task.route)
            return

        session_id = self.state.get_default_session()
        if not session_id:
            self.gateway.send_message(
                "기본 Codex 세션이 없습니다.\n먼저 <code>/new</code>로 새 세션을 시작해주세요.",
                task.chat_id,
                task.route,
            )
            return

        with TypingIndicator(
            self.gateway,
            task.chat_id,
            task.route,
            self.config.telegram_typing_interval_seconds,
        ):
            output = self.runner.run_resume(session_id, text)
        self.gateway.send_message(output, task.chat_id, task.route)

    @staticmethod
    def _error_message(exc: Exception) -> str:
        fallback = f"<b>알 수 없는 에러가 발생했습니다.</b>\n<pre>{html.escape(str(exc))}</pre>"
        return error_message_with_run_context(exc, fallback)


def parse_yaml_schedule(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text()) or {}
    schedules = data.get("schedules", [])
    if not isinstance(schedules, list):
        raise ValueError("schedule file must contain a schedules list")
    return [item for item in schedules if isinstance(item, dict)]


def cron_matches(expr: str, now: datetime) -> bool:
    aliases = {
        "@hourly": "0 * * * *",
        "@daily": "0 0 * * *",
        "@weekly": "0 0 * * 0",
    }
    expr = aliases.get(expr.strip(), expr.strip())
    fields = expr.split()
    if len(fields) != 5:
        raise ValueError(f"unsupported cron expression: {expr}")

    minute, hour, day, month, weekday = fields
    cron_weekday = (now.weekday() + 1) % 7
    return (
        _field_matches(minute, now.minute, 0, 59)
        and _field_matches(hour, now.hour, 0, 23)
        and _field_matches(day, now.day, 1, 31)
        and _field_matches(month, now.month, 1, 12)
        and _field_matches(weekday, cron_weekday, 0, 7)
    )


def _field_matches(expr: str, value: int, minimum: int, maximum: int) -> bool:
    for part in expr.split(","):
        part = part.strip()
        if not part:
            continue
        base, step = (part.split("/", 1) + ["1"])[:2] if "/" in part else (part, "1")
        step_int = int(step)
        if base == "*":
            start, end = minimum, maximum
        elif "-" in base:
            start_text, end_text = base.split("-", 1)
            start, end = int(start_text), int(end_text)
        else:
            start = end = int(base)
        if maximum == 7 and value == 0 and start == end == 7:
            return True
        if start <= value <= end and (value - start) % step_int == 0:
            return True
    return False


class Scheduler:
    def __init__(self, config: Config, runner: CodexRunner, gateway: TelegramGateway) -> None:
        self.config = config
        self.runner = runner
        self.gateway = gateway
        self.stop_event = threading.Event()
        self.last_run_keys: set[tuple[str, str]] = set()
        self.thread = threading.Thread(target=self._loop, name="scheduler", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()

    def _loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                self._tick()
            except Exception:
                logging.exception("scheduler tick failed")
            self.stop_event.wait(self.config.scheduler_poll_seconds)

    def _tick(self) -> None:
        now = datetime.now()
        minute_key = now.strftime("%Y%m%d%H%M")
        for item in parse_yaml_schedule(self.config.schedule_file):
            job_id = str(item.get("id", "")).strip()
            if not job_id:
                continue
            if item.get("enabled", True) is False:
                continue
            cron = str(item.get("cron", "")).strip()
            message = str(item.get("message", "")).strip()
            if not cron or not message:
                continue
            key = (job_id, minute_key)
            if key in self.last_run_keys:
                continue
            if cron_matches(cron, now):
                self.last_run_keys.add(key)
                thread = threading.Thread(
                    target=self._run_job,
                    args=(job_id, message, item.get("chat_id"), item.get("route")),
                    name=f"schedule-{job_id}",
                    daemon=True,
                )
                thread.start()

    def _run_job(self, job_id: str, message: str, chat_id: Any, route: Any) -> None:
        logging.info("running scheduled job id=%s", job_id)
        chat_id_text = str(chat_id) if chat_id else None
        route_text = str(route) if route else None
        try:
            with TypingIndicator(
                self.gateway,
                chat_id_text,
                route_text,
                self.config.telegram_typing_interval_seconds,
            ):
                output = self.runner.run_once(
                    message,
                    daily_trading_hint=is_daily_trading_schedule(job_id),
                )
            self.gateway.send_message(output, chat_id_text, route_text)
        except Exception as exc:  # noqa: BLE001 - report schedule failures to Telegram
            if isinstance(exc, UserFacingError):
                logging.warning("scheduled job failed id=%s: %s", job_id, exc)
            else:
                logging.exception("scheduled job failed id=%s", job_id)
            fallback = (
                f"<b>알 수 없는 에러가 발생했습니다.</b>\n<code>{html.escape(job_id)}</code>\n"
                f"<pre>{html.escape(str(exc))}</pre>"
            )
            message = error_message_with_run_context(exc, fallback)
            self.gateway.send_message(
                message,
                chat_id_text,
                route_text,
            )


class App:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.state = StateStore(config)
        self.runner = CodexRunner(config)
        self.gateway = TelegramGateway(config)
        self.telegram_worker = TelegramWorker(config, self.state, self.runner, self.gateway)
        self.scheduler = Scheduler(config, self.runner, self.gateway)

    def start(self) -> ThreadingHTTPServer:
        self.telegram_worker.start()
        self.scheduler.start()
        return self._serve_http()

    def stop(self) -> None:
        self.scheduler.stop()

    def _serve_http(self) -> ThreadingHTTPServer:
        app = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "codex-exec"

            def do_GET(self) -> None:
                if self.path != "/healthz":
                    self._write_json(404, {"ok": False, "error": "not found"})
                    return
                self._write_json(200, {"ok": True})

            def do_POST(self) -> None:
                if self.path != "/telegram":
                    self._write_json(404, {"ok": False, "error": "not found"})
                    return
                try:
                    payload = self._read_json()
                    text = str(payload.get("text", "")).strip()
                    if not text:
                        self._write_json(400, {"ok": False, "error": "text is required"})
                        return

                    app.telegram_worker.submit(
                        TelegramTask(
                            chat_id=str(payload.get("chat_id")) if payload.get("chat_id") else None,
                            text=text,
                            route=str(payload.get("route")) if payload.get("route") else None,
                            message_id=payload.get("message_id"),
                        )
                    )
                except Exception as exc:  # noqa: BLE001 - expose endpoint failures as JSON
                    logging.exception("failed to accept telegram payload")
                    self._write_json(500, {"ok": False, "error": str(exc)})
                    return

                self._write_json(202, {"ok": True, "queued": True})

            def log_message(self, fmt: str, *args: Any) -> None:
                logging.info("http %s - %s", self.address_string(), fmt % args)

            def _read_json(self) -> dict[str, Any]:
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length).decode("utf-8")
                parsed = json.loads(raw or "{}")
                if not isinstance(parsed, dict):
                    raise ValueError("request body must be a JSON object")
                return parsed

            def _write_json(self, status: int, payload: dict[str, Any]) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer((self.config.host, self.config.port), Handler)
        thread = threading.Thread(target=server.serve_forever, name="http-server", daemon=True)
        thread.start()
        return server


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    config = Config.from_env()
    app = App(config)

    stop_event = threading.Event()

    def stop(_signum: int, _frame: Any) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    server = app.start()
    logging.info(
        "codex-exec listening on %s:%s",
        config.host,
        config.port,
    )
    try:
        while not stop_event.wait(1):
            pass
    finally:
        app.stop()
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
