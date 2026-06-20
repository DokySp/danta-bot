import html
import json
import logging
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

from .config import Config
from .daily_trading import (
    append_daily_trading_started_at,
    attach_daily_trading_context,
    codex_run_context_prompt,
    daily_trading_model_contract_prompt,
    is_explicit_daily_trading_request,
    mcp_trading_env_prompt,
    new_codex_run_context,
)
from .errors import classify_codex_error
from .holding_history import append_holding_history_from_run
from .skills_sync import sync_bundled_skills

HTML_PROMPT_SUFFIX = (
    "\n\n결과는 telegram으로 보낼거기 때문에 마크다운이 아니라 "
    "parse_mode=HTML에 맞춰서 출력해줘. Telegram HTML에서 지원되는 "
    "<b>, <i>, <u>, <s>, <code>, <pre>, <a> 태그 위주로 사용하고 "
    "전체 메시지는 가능한 4096자 안쪽으로 요약해줘."
)

TOKEN_USAGE_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)


def zero_token_usage() -> dict[str, int]:
    return {field: 0 for field in TOKEN_USAGE_FIELDS}


def token_usage_from(raw: Any) -> dict[str, int]:
    usage = zero_token_usage()
    if not isinstance(raw, dict):
        return usage
    for field in TOKEN_USAGE_FIELDS:
        value = raw.get(field)
        if isinstance(value, bool):
            continue
        try:
            usage[field] = int(value)
        except (TypeError, ValueError):
            usage[field] = 0
    if usage["total_tokens"] <= 0:
        usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]
    return usage


def add_token_usage(total: dict[str, int], usage: dict[str, int]) -> None:
    for field in TOKEN_USAGE_FIELDS:
        total[field] = int(total.get(field, 0)) + int(usage.get(field, 0))


def token_count_payload(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    if item.get("type") == "token_count":
        return item
    if item.get("type") != "event_msg":
        return None
    payload = item.get("payload")
    if isinstance(payload, dict) and payload.get("type") == "token_count":
        return payload
    return None


def turn_completed_usage(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    if item.get("type") != "turn.completed":
        return None
    usage = item.get("usage")
    return usage if isinstance(usage, dict) else None


def parse_codex_json_events(stdout: str) -> dict[str, Any]:
    usage = zero_token_usage()
    event_count = 0
    last_rate_limits: Any | None = None
    last_message = ""
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        payload = token_count_payload(item)
        if payload is not None:
            info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
            add_token_usage(usage, token_usage_from(info.get("last_token_usage")))
            event_count += 1
            last_rate_limits = item.get("rate_limits") or payload.get("rate_limits") or last_rate_limits
            continue
        completed_usage = turn_completed_usage(item)
        if completed_usage is not None:
            add_token_usage(usage, token_usage_from(completed_usage))
            event_count += 1
            last_rate_limits = item.get("rate_limits") or last_rate_limits
            continue
        if isinstance(item, dict) and item.get("type") == "event_msg":
            event_payload = item.get("payload")
            if isinstance(event_payload, dict) and event_payload.get("type") == "task_complete":
                message = event_payload.get("last_agent_message")
                if isinstance(message, str):
                    last_message = message
    return {
        "token_usage": usage,
        "token_usage_event_count": event_count,
        "rate_limits": last_rate_limits,
        "last_agent_message": last_message,
    }


def parse_percent(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip().removesuffix("%").strip()
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def usage_window(snapshot: Any, key: str) -> dict[str, Any] | None:
    if not isinstance(snapshot, dict):
        return None
    limits = snapshot.get("rateLimits")
    if not isinstance(limits, dict):
        return None
    window = limits.get(key)
    return window if isinstance(window, dict) else None


def used_percent(snapshot: Any, key: str) -> float | None:
    window = usage_window(snapshot, key)
    if not window:
        return None
    return parse_percent(window.get("usedPercent"))


def format_percent_delta(before: Any, after: Any, key: str) -> str:
    before_used = used_percent(before, key)
    after_used = used_percent(after, key)
    if before_used is None or after_used is None or after_used < before_used:
        return "n/a"
    delta = max(0.0, after_used - before_used)
    if delta.is_integer():
        return f"{int(delta)}%"
    return f"{delta:.1f}".rstrip("0").rstrip(".") + "%"


def format_token_count(total_tokens: int, has_usage: bool) -> str:
    if not has_usage:
        return "n/a"
    return f"{total_tokens:,}"


class CodexRunner:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.tmp_dir = Path(os.getenv("CODEX_EXEC_TMP_DIR", "/tmp/codex-exec"))
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        self.config.codex_home.mkdir(parents=True, exist_ok=True)
        sync_bundled_skills(config)

    def run_new_session(self, prompt: str) -> tuple[str, str]:
        before = self._session_ids()
        output = self._run_codex(["exec"], prompt)
        session_id = self._detect_new_session_id(before)
        if not session_id:
            raise RuntimeError("codex finished but new session id was not found")
        return session_id, output

    def run_resume(self, session_id: str, prompt: str) -> str:
        return self._run_codex(["exec", "resume", session_id], prompt)

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

    def _build_prompt(self, prompt: str, context, daily_trading_hint: bool) -> str:
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
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> str:
        context = new_codex_run_context()
        output_file = self.tmp_dir / f"{context.run_id}.txt"
        daily_trading_hint = daily_trading_hint or is_explicit_daily_trading_request(prompt)
        full_prompt = self._build_prompt(prompt, context, daily_trading_hint)
        usage_before = self._read_usage_snapshot()
        model_value = model or self.config.model
        reasoning_effort_value = reasoning_effort or self.config.reasoning_effort

        cmd = [
            self.config.codex_bin,
            "exec",
            "--json",
            *subcommand[1:],
            "-m",
            model_value,
            "-c",
            f'model_reasoning_effort="{reasoning_effort_value}"',
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
                self._append_holding_history_if_available(context)
                attach_daily_trading_context(exc, context)
            raise

        if result.returncode != 0:
            exc = classify_codex_error(result.returncode, result.stdout, result.stderr)
            if daily_trading_hint or self._daily_trading_artifact_exists(context):
                self._append_holding_history_if_available(context)
                attach_daily_trading_context(exc, context)
            raise exc

        event_summary = parse_codex_json_events(result.stdout or "")
        if output_file.exists():
            output = output_file.read_text()
        else:
            output = str(event_summary.get("last_agent_message") or "").strip() or result.stdout.strip()
        output = output.strip() or "<i>Codex completed without output.</i>"
        if daily_trading_hint or self._daily_trading_artifact_exists(context):
            self._refresh_daily_trading_token_artifacts(context, result.stdout or "")
            self._append_holding_history_if_available(context)
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

    def _read_usage_snapshot(self) -> dict[str, Any] | None:
        if not self.config.usage_script.exists():
            logging.warning("codex usage script not found path=%s", self.config.usage_script)
            return None

        cmd = [
            str(self.config.usage_script),
            "--json",
            "--timeout",
            str(self.config.usage_timeout_seconds),
        ]
        env = os.environ.copy()
        env["CODEX_HOME"] = str(self.config.codex_home)
        env["CODEX_BIN"] = self.config.codex_bin

        try:
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
        except Exception:
            logging.exception("failed to query codex usage snapshot")
            return None
        if result.returncode != 0:
            logging.warning("codex usage snapshot failed stderr=%s", (result.stderr or "").strip()[-1000:])
            return None
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            logging.warning("codex usage snapshot returned invalid JSON")
            return None

    def _append_token_usage_summary(
        self,
        output: str,
        context,
        event_summary: dict[str, Any],
        usage_before: dict[str, Any] | None,
        usage_after: dict[str, Any] | None,
    ) -> str:
        if "총 사용 토큰:" in output:
            return output

        main_usage = token_usage_from(event_summary.get("token_usage"))
        subagent_usage, subagent_has_usage = self._subagent_token_usage(context)
        total_usage = zero_token_usage()
        add_token_usage(total_usage, main_usage)
        add_token_usage(total_usage, subagent_usage)
        has_usage = bool(event_summary.get("token_usage_event_count")) or subagent_has_usage

        summary = "\n".join(
            [
                f"<b>총 사용 토큰: {format_token_count(total_usage['total_tokens'], has_usage)}</b>",
                f"<b>5h: {format_percent_delta(usage_before, usage_after, 'primary')}</b>",
                f"<b>weekly: {format_percent_delta(usage_before, usage_after, 'secondary')}</b>",
            ]
        )
        return f"{output.rstrip()}\n\n{summary}"

    def _subagent_token_usage(self, context) -> tuple[dict[str, int], bool]:
        total = zero_token_usage()
        has_usage = False
        subagent_dir = self.config.workspace_dir / "reports" / "runs" / context.run_id / "subagents"
        if not subagent_dir.is_dir():
            return total, False
        for path in sorted(subagent_dir.glob("*.wrapper.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
            except (OSError, json.JSONDecodeError):
                logging.warning("failed to read subagent token wrapper path=%s", path)
                continue
            if not isinstance(payload, dict):
                continue
            usage = token_usage_from(payload.get("token_usage"))
            if int(usage.get("total_tokens", 0)) > 0 or payload.get("token_usage_event_count"):
                has_usage = True
            add_token_usage(total, usage)
        return total, has_usage

    def _daily_trading_artifact_exists(self, context) -> bool:
        return (self.config.workspace_dir / "reports" / "runs" / context.run_id / "run.json").is_file()

    def _daily_trading_run_dir(self, context) -> Path:
        return self.config.workspace_dir / "reports" / "runs" / context.run_id

    def _daily_trading_artifact_script(self) -> Path | None:
        candidates = [
            self.config.workspace_dir
            / "containers/codex-exec/shared-skills/daily-trading/scripts/build_run_artifacts.py",
            Path("/app/skills/daily-trading/scripts/build_run_artifacts.py"),
        ]
        for candidate in candidates:
            if candidate.exists():
                return candidate
        return None

    def _write_daily_trading_main_events(self, run_dir: Path, stdout: str) -> Path | None:
        lines: list[str] = []
        for line in stdout.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                item = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if token_count_payload(item) is not None or turn_completed_usage(item) is not None:
                lines.append(json.dumps(item, ensure_ascii=False, separators=(",", ":")))
        if not lines:
            return None
        path = run_dir / "main-events.jsonl"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def _refresh_daily_trading_token_artifacts(self, context, stdout: str) -> None:
        run_dir = self._daily_trading_run_dir(context)
        if not run_dir.is_dir():
            return
        artifact_script = self._daily_trading_artifact_script()
        if artifact_script is None:
            logging.warning("daily-trading artifact helper not found; cannot refresh token-summary run_id=%s", context.run_id)
            return
        main_events = self._write_daily_trading_main_events(run_dir, stdout)
        if main_events is None:
            return

        cmd = [
            sys.executable,
            str(artifact_script),
            "token-summary",
            "--run-dir",
            str(run_dir),
            "--main-events",
            str(main_events),
        ]
        result = subprocess.run(
            cmd,
            cwd=self.config.workspace_dir,
            env=os.environ.copy(),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if result.returncode != 0:
            logging.warning("daily-trading token-summary refresh failed stderr=%s", (result.stderr or "").strip()[-1000:])
            return
        token_summary_path = run_dir / "token-summary.json"
        pipeline_summary_path = run_dir / "pipeline-summary.json"
        try:
            token_summary = json.loads(token_summary_path.read_text(encoding="utf-8"))
            pipeline_summary = json.loads(pipeline_summary_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logging.warning("daily-trading token artifact refresh could not read summaries run_id=%s", context.run_id)
            return
        pipeline_summary["token_usage"] = {
            "main": (token_summary.get("main") or {}).get("token_usage", zero_token_usage()),
            "subagents": (token_summary.get("subagents") or {}).get("token_usage", zero_token_usage()),
            "total": (token_summary.get("total") or {}).get("token_usage", zero_token_usage()),
        }
        tmp = pipeline_summary_path.with_suffix(pipeline_summary_path.suffix + ".tmp")
        tmp.write_text(json.dumps(pipeline_summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(pipeline_summary_path)

    def _append_holding_history_if_available(self, context) -> None:
        try:
            append_holding_history_from_run(self.config.workspace_dir, context)
        except Exception:
            logging.exception("failed to append holding history run_id=%s", context.run_id)

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
