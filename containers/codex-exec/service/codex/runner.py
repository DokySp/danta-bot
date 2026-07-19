import html
import json
import logging
import os
import signal
import shlex
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .events import parse_codex_json_events
from .progress import CodexProgressBridge
from .runtime_config import CodexRuntimeDefaults, load_codex_runtime_defaults
from .sessions import detect_new_session_id, session_ids
from .usage import append_token_usage_summary, read_usage_snapshot
from ..bootstrap.skills import sync_bundled_skills
from ..config import Config
from ..errors import classify_codex_error
from ..pipelines.daily_trading.artifacts import (
    daily_trading_artifact_exists,
    daily_trading_telegram_summary,
    refresh_daily_trading_token_artifacts,
)
from ..pipelines.daily_trading.holding_history import append_holding_history_from_run
from ..trading.daily_trading import (
    append_daily_trading_started_at,
    attach_daily_trading_context,
    codex_run_context_prompt,
    mcp_trading_env_prompt,
    new_codex_run_context,
)

HTML_PROMPT_SUFFIX = (
    "\n\n결과는 telegram으로 보낼거기 때문에 마크다운이 아니라 "
    "parse_mode=HTML에 맞춰서 출력해줘. Telegram HTML에서 지원되는 "
    "<b>, <i>, <u>, <s>, <code>, <pre>, <a> 태그 위주로 사용하고 "
    "전체 메시지는 가능한 4096자 안쪽으로 요약해줘."
)

STDERR_CAPTURE_LIMIT = 8000
PROCESS_TERMINATE_GRACE_SECONDS = 5.0


class CodexRunCancelled(RuntimeError):
    def __init__(self) -> None:
        super().__init__("codex run cancelled by /stop")


def _signal_process_group(process: subprocess.Popen[str], sig: signal.Signals) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, sig)
        else:
            process.send_signal(sig)
    except (ProcessLookupError, PermissionError, OSError):
        return


def _schedule_forced_kill(process: subprocess.Popen[str]) -> None:
    def force_kill() -> None:
        if process.poll() is None:
            _signal_process_group(process, signal.SIGKILL)

    timer = threading.Timer(PROCESS_TERMINATE_GRACE_SECONDS, force_kill)
    timer.daemon = True
    timer.start()


class _ActiveCodexRun:
    def __init__(self, process: subprocess.Popen[str] | None = None) -> None:
        self.process = process
        self._lock = threading.Lock()
        self._state = "running"

    def attach_process(self, process: subprocess.Popen[str]) -> None:
        with self._lock:
            if self.process is not None:
                raise RuntimeError("active Codex run already has a process")
            self.process = process
            stop_requested = self._state in {"cancelled", "timed_out"}
        if stop_requested:
            _signal_process_group(process, signal.SIGTERM)
            _schedule_forced_kill(process)

    def cancel(self) -> bool:
        return self._request_stop("cancelled")

    def timeout(self) -> bool:
        return self._request_stop("timed_out")

    def mark_stdout_closed(self) -> None:
        with self._lock:
            if self._state == "running":
                self._state = "completed"

    def state(self) -> str:
        with self._lock:
            return self._state

    def is_cancellable(self) -> bool:
        with self._lock:
            return self._state == "running" and (
                self.process is None or self.process.poll() is None
            )

    def _request_stop(self, state: str) -> bool:
        with self._lock:
            if self._state != "running":
                return False
            if self.process is not None and self.process.poll() is not None:
                return False
            self._state = state
            process = self.process
        if process is not None:
            _signal_process_group(process, signal.SIGTERM)
            _schedule_forced_kill(process)
        return True


class _TextTail:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self._value = ""
        self._lock = threading.Lock()

    def append(self, value: str) -> None:
        with self._lock:
            self._value = (self._value + value)[-self.limit :]

    def value(self) -> str:
        with self._lock:
            return self._value


class CodexRunner:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.tmp_dir = Path(os.getenv("CODEX_EXEC_TMP_DIR", "/tmp/codex-exec"))
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        self.config.codex_home.mkdir(parents=True, exist_ok=True)
        self._active_telegram_lock = threading.Lock()
        self._active_telegram_run: _ActiveCodexRun | None = None
        self.runtime_defaults()
        sync_bundled_skills(config)

    def run_new_session(
        self,
        prompt: str | None = None,
        on_progress: Callable[[str], None] | None = None,
    ) -> tuple[str, str]:
        runtime_defaults = self.runtime_defaults()
        before = self._session_ids()
        output = self._run_codex(
            ["exec"],
            prompt if prompt is not None else runtime_defaults.new_session_prompt,
            runtime_defaults=runtime_defaults,
            on_progress=on_progress,
        )
        session_id = self._detect_new_session_id(before)
        if not session_id:
            raise RuntimeError("codex finished but new session id was not found")
        return session_id, output

    def run_resume(
        self,
        session_id: str,
        prompt: str,
        on_progress: Callable[[str], None] | None = None,
    ) -> str:
        return self._run_codex(
            ["exec", "resume", session_id],
            prompt,
            on_progress=on_progress,
        )

    def run_once(
        self,
        prompt: str,
        daily_trading_hint: bool = False,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> str:
        return self._run_codex(
            ["exec"],
            prompt,
            daily_trading_hint=daily_trading_hint,
            model=model,
            reasoning_effort=reasoning_effort,
        )

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

    def runtime_defaults(self) -> CodexRuntimeDefaults:
        return load_codex_runtime_defaults(self.config.codex_runtime_config_file)

    def cancel_active_telegram_run(self) -> bool:
        with self._active_telegram_lock:
            active_run = self._active_telegram_run
            if active_run is None:
                return False
            return active_run.cancel()

    def has_active_telegram_run(self) -> bool:
        with self._active_telegram_lock:
            active_run = self._active_telegram_run
            return active_run is not None and active_run.is_cancellable()

    def _build_prompt(self, prompt: str, context, daily_trading_hint: bool) -> str:
        return (
            prompt.rstrip()
            + mcp_trading_env_prompt(self.config.mcp_trading_env)
            + codex_run_context_prompt(context)
            + HTML_PROMPT_SUFFIX
        )

    def _run_codex(
        self,
        subcommand: list[str],
        prompt: str,
        daily_trading_hint: bool = False,
        model: str | None = None,
        reasoning_effort: str | None = None,
        runtime_defaults: CodexRuntimeDefaults | None = None,
        on_progress: Callable[[str], None] | None = None,
    ) -> str:
        context = new_codex_run_context()
        output_file = self.tmp_dir / f"{context.run_id}.txt"
        full_prompt = self._build_prompt(prompt, context, daily_trading_hint)
        usage_before = self._read_usage_snapshot()
        defaults = runtime_defaults or self.runtime_defaults()
        model_value = model or defaults.model
        reasoning_effort_value = reasoning_effort or defaults.model_reasoning_effort
        main_model_usage_recorded = False

        cmd = [
            self.config.codex_bin,
            "exec",
            "--json",
            *subcommand[1:],
            "-m",
            model_value,
            "-c",
            f"model_reasoning_effort={json.dumps(reasoning_effort_value, ensure_ascii=False)}",
            "--skip-git-repo-check",
            "-o",
            str(output_file),
        ]
        if self.config.bypass_sandbox:
            cmd.append("--dangerously-bypass-approvals-and-sandbox")
        cmd.extend(["--", full_prompt])

        env = os.environ.copy()
        env["CODEX_HOME"] = str(self.config.codex_home)
        env["CODEX_MCP_TRADING_ENV"] = self.config.mcp_trading_env

        if daily_trading_hint:
            self._append_daily_trading_model_usage(
                context,
                model=model_value,
                reasoning_effort=reasoning_effort_value,
                subcommand=subcommand,
            )
            main_model_usage_recorded = True
        logging.info("running codex command=%s", " ".join(shlex.quote(part) for part in cmd[:-1]))
        try:
            if on_progress is None:
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
            else:
                result = self._run_codex_streaming(cmd, env, on_progress)
        except Exception as exc:
            daily_trading_artifact_exists = self._daily_trading_artifact_exists(context)
            if daily_trading_artifact_exists and not main_model_usage_recorded:
                self._append_daily_trading_model_usage(
                    context,
                    model=model_value,
                    reasoning_effort=reasoning_effort_value,
                    subcommand=subcommand,
                )
                main_model_usage_recorded = True
            if daily_trading_hint or daily_trading_artifact_exists:
                self._append_holding_history_if_available(context)
                attach_daily_trading_context(exc, context)
            raise

        if result.returncode != 0:
            exc = classify_codex_error(result.returncode, result.stdout, result.stderr)
            daily_trading_artifact_exists = self._daily_trading_artifact_exists(context)
            if daily_trading_artifact_exists and not main_model_usage_recorded:
                self._append_daily_trading_model_usage(
                    context,
                    model=model_value,
                    reasoning_effort=reasoning_effort_value,
                    subcommand=subcommand,
                )
                main_model_usage_recorded = True
            if daily_trading_hint or daily_trading_artifact_exists:
                self._append_holding_history_if_available(context)
                attach_daily_trading_context(exc, context)
            raise exc

        event_summary = parse_codex_json_events(result.stdout or "")
        if output_file.exists():
            output = output_file.read_text()
        else:
            output = str(event_summary.get("last_agent_message") or "").strip() or result.stdout.strip()
        output = output.strip() or "<i>Codex completed without output.</i>"
        daily_trading_artifact_exists = self._daily_trading_artifact_exists(context)
        if daily_trading_artifact_exists and not main_model_usage_recorded:
            self._append_daily_trading_model_usage(
                context,
                model=model_value,
                reasoning_effort=reasoning_effort_value,
                subcommand=subcommand,
            )
            main_model_usage_recorded = True
        if daily_trading_hint or daily_trading_artifact_exists:
            refresh_daily_trading_token_artifacts(self.config.workspace_dir, context, result.stdout or "")
            self._append_holding_history_if_available(context)
            output = daily_trading_telegram_summary(self.config.workspace_dir, context) or output
            output = append_daily_trading_started_at(output, context)
        usage_after = self._read_usage_snapshot()
        output = self._append_token_usage_summary(
            output,
            context,
            event_summary,
            usage_before,
            usage_after,
        )
        return output

    def _run_codex_streaming(
        self,
        cmd: list[str],
        env: dict[str, str],
        on_progress: Callable[[str], None],
    ) -> subprocess.CompletedProcess[str]:
        active_run = _ActiveCodexRun()
        self._register_active_telegram_run(active_run)
        try:
            if active_run.state() == "cancelled":
                raise CodexRunCancelled()
            process = subprocess.Popen(
                cmd,
                cwd=self.config.workspace_dir,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=1,
                start_new_session=os.name == "posix",
            )
            active_run.attach_process(process)
        except BaseException:
            self._unregister_active_telegram_run(active_run)
            raise
        stderr_tail = _TextTail(STDERR_CAPTURE_LIMIT)

        def drain_stderr() -> None:
            assert process.stderr is not None
            for chunk in iter(lambda: process.stderr.read(4096), ""):
                stderr_tail.append(chunk)

        stderr_thread = threading.Thread(
            target=drain_stderr,
            name="codex-stderr",
            daemon=True,
        )
        stderr_thread.start()
        timeout_timer = threading.Timer(
            self.config.codex_timeout_seconds,
            active_run.timeout,
        )
        timeout_timer.daemon = True
        timeout_timer.start()
        stdout_parts: list[str] = []
        bridge = CodexProgressBridge(on_progress)

        try:
            assert process.stdout is not None
            for line in process.stdout:
                stdout_parts.append(line)
                bridge.handle_line(line)
            bridge.finish()
            active_run.mark_stdout_closed()
            timeout_timer.cancel()
            try:
                returncode = process.wait(timeout=PROCESS_TERMINATE_GRACE_SECONDS + 1)
            except subprocess.TimeoutExpired:
                _signal_process_group(process, signal.SIGKILL)
                returncode = process.wait(timeout=PROCESS_TERMINATE_GRACE_SECONDS)
        except BaseException:
            _signal_process_group(process, signal.SIGTERM)
            _schedule_forced_kill(process)
            raise
        finally:
            timeout_timer.cancel()
            if process.poll() is None:
                _signal_process_group(process, signal.SIGKILL)
                try:
                    process.wait(timeout=PROCESS_TERMINATE_GRACE_SECONDS)
                except subprocess.TimeoutExpired:
                    logging.error("codex process did not exit after SIGKILL pid=%s", process.pid)
            stderr_thread.join(timeout=1)
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()
            self._unregister_active_telegram_run(active_run)

        stdout = "".join(stdout_parts)
        stderr = stderr_tail.value()
        stop_state = active_run.state()
        if stop_state == "cancelled":
            raise CodexRunCancelled()
        if stop_state == "timed_out":
            raise subprocess.TimeoutExpired(
                cmd,
                self.config.codex_timeout_seconds,
                output=stdout,
                stderr=stderr,
            )
        return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)

    def _register_active_telegram_run(self, active_run: _ActiveCodexRun) -> None:
        with self._active_telegram_lock:
            if self._active_telegram_run is not None:
                raise RuntimeError("another Telegram-triggered Codex run is already active")
            self._active_telegram_run = active_run

    def _unregister_active_telegram_run(self, active_run: _ActiveCodexRun) -> None:
        with self._active_telegram_lock:
            if self._active_telegram_run is active_run:
                self._active_telegram_run = None

    def _append_daily_trading_model_usage(
        self,
        context: Any,
        *,
        model: str,
        reasoning_effort: str,
        subcommand: list[str],
    ) -> Path:
        path = self.config.workspace_dir / "reports" / "runs" / context.run_id / "model-usage.jsonl"
        task_name = "main-resume" if "resume" in subcommand else "main-exec"
        payload = {
            "schema_version": "1",
            "run_id": context.run_id,
            "run_started_at": context.started_at,
            "started_at": context.started_at,
            "source": "daily-trading-main",
            "stage": "main",
            "agent_role": "main",
            "task_name": task_name,
            "model": model,
            "model_reasoning_effort": reasoning_effort,
        }
        encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode(
            "utf-8"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
        try:
            written = os.write(descriptor, encoded)
        finally:
            os.close(descriptor)
        if written != len(encoded):
            raise OSError(f"short write while recording model usage: {path}")
        return path

    def _read_usage_snapshot(self) -> dict[str, Any] | None:
        return read_usage_snapshot(self.config)

    def _append_token_usage_summary(
        self,
        output: str,
        context,
        event_summary: dict[str, Any],
        usage_before: dict[str, Any] | None,
        usage_after: dict[str, Any] | None,
    ) -> str:
        return append_token_usage_summary(
            output,
            self.config.workspace_dir,
            context,
            event_summary,
            usage_before,
            usage_after,
        )

    def _daily_trading_artifact_exists(self, context) -> bool:
        return daily_trading_artifact_exists(self.config.workspace_dir, context)

    def _append_holding_history_if_available(self, context) -> None:
        try:
            append_holding_history_from_run(self.config.workspace_dir, context)
        except Exception:
            logging.exception("failed to append holding history run_id=%s", context.run_id)

    def _session_ids(self) -> list[str]:
        return session_ids(self.config.codex_home)

    def _detect_new_session_id(self, before: list[str]) -> str | None:
        after = self._session_ids()
        return detect_new_session_id(before, after)
