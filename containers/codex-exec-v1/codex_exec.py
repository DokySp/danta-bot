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

import yaml


HTML_PROMPT_SUFFIX = (
    "\n\n결과는 telegram으로 보낼거기 때문에 마크다운이 아니라 "
    "parse_mode=HTML에 맞춰서 출력해줘. Telegram HTML에서 지원되는 "
    "<b>, <i>, <u>, <s>, <code>, <pre>, <a> 태그 위주로 사용하고 "
    "전체 메시지는 가능한 4096자 안쪽으로 요약해줘."
)

MCP_TRADING_ENV_VALUES = {"paper", "acct"}


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


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
    sync_skills: bool
    sync_skills_once: bool
    sync_skills_overwrite: bool

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
            reasoning_effort=os.getenv("CODEX_REASONING_EFFORT", "xhigh"),
            codex_timeout_seconds=env_int("CODEX_TIMEOUT_SECONDS", 1800),
            scheduler_poll_seconds=env_int("SCHEDULER_POLL_SECONDS", 15),
            telegram_typing_interval_seconds=env_float("TELEGRAM_TYPING_INTERVAL_SECONDS", 4.0),
            bypass_sandbox=env_bool("CODEX_BYPASS_APPROVALS_AND_SANDBOX", True),
            new_session_prompt=os.getenv("NEW_SESSION_PROMPT", "새 대화 시작"),
            usage_script=Path(os.getenv("CODEX_USAGE_SCRIPT", "/app/codex_usage")),
            usage_timeout_seconds=env_int("CODEX_USAGE_TIMEOUT_SECONDS", 20),
            bundled_skills_dir=Path(os.getenv("BUNDLED_SKILLS_DIR", "/app/skills")),
            sync_skills=env_bool("CODEX_SYNC_SKILLS", True),
            sync_skills_once=env_bool("CODEX_SYNC_SKILLS_ONCE", True),
            sync_skills_overwrite=env_bool("CODEX_SYNC_SKILLS_OVERWRITE", False),
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
            "updated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
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
        if not chat_id:
            return
        outbound_route = route or self.config.telegram_route
        if not outbound_route:
            logging.debug("telegram chat action skipped because route is missing")
            return

        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "route": outbound_route,
            "action": action,
        }
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
        if self.chat_id:
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

    def run_once(self, prompt: str) -> str:
        return self._run_codex(["exec"], prompt)

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

    def _build_prompt(self, prompt: str) -> str:
        return (
            prompt.rstrip()
            + mcp_trading_env_prompt(self.config.mcp_trading_env)
            + HTML_PROMPT_SUFFIX
        )

    def _run_codex(self, subcommand: list[str], prompt: str) -> str:
        run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
        output_file = self.tmp_dir / f"{run_id}.txt"
        full_prompt = self._build_prompt(prompt)

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

        if result.returncode != 0:
            raise classify_codex_error(result.returncode, result.stdout, result.stderr)

        if output_file.exists():
            output = output_file.read_text()
        else:
            output = result.stdout.strip()
        return output.strip() or "<i>Codex completed without output.</i>"

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
        if not self.config.sync_skills:
            return
        source = self.config.bundled_skills_dir
        if not source.exists():
            logging.info("bundled skills dir does not exist: %s", source)
            return

        target_root = self.config.codex_home / "skills"
        marker = self.config.codex_home / ".bundled_skills_initialized"

        target_root.mkdir(parents=True, exist_ok=True)

        copied = 0
        skipped = 0
        for skill_dir in sorted(path for path in source.iterdir() if path.is_dir()):
            target = target_root / skill_dir.name
            if target.exists() and self.config.sync_skills_overwrite:
                shutil.rmtree(target)
            if target.exists():
                skipped += 1
                continue
            shutil.copytree(skill_dir, target)
            copied += 1

        if self.config.sync_skills_once:
            self._write_skills_marker(marker, copied=copied, skipped=skipped)

        logging.info(
            "synced bundled skills copied=%s skipped_existing=%s source=%s target=%s",
            copied,
            skipped,
            source,
            target_root,
        )

    def _write_skills_marker(self, marker: Path, copied: int, skipped: int) -> None:
        payload = {
            "source": str(self.config.bundled_skills_dir),
            "target": str(self.config.codex_home / "skills"),
            "copied": copied,
            "skipped_existing": skipped,
            "initialized_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
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
        if isinstance(exc, UserFacingError):
            return exc.html_message
        return f"<b>알 수 없는 에러가 발생했습니다.</b>\n<pre>{html.escape(str(exc))}</pre>"


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
                output = self.runner.run_once(message)
            self.gateway.send_message(output, chat_id_text, route_text)
        except Exception as exc:  # noqa: BLE001 - report schedule failures to Telegram
            if isinstance(exc, UserFacingError):
                logging.warning("scheduled job failed id=%s: %s", job_id, exc)
            else:
                logging.exception("scheduled job failed id=%s", job_id)
            if isinstance(exc, UserFacingError):
                message = exc.html_message
            else:
                message = (
                    f"<b>알 수 없는 에러가 발생했습니다.</b>\n<code>{html.escape(job_id)}</code>\n"
                    f"<pre>{html.escape(str(exc))}</pre>"
                )
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
