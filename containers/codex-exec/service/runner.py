import html
import json
import logging
import os
import shlex
import subprocess
from pathlib import Path

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
                self._append_holding_history_if_available(context)
                attach_daily_trading_context(exc, context)
            raise

        if result.returncode != 0:
            exc = classify_codex_error(result.returncode, result.stdout, result.stderr)
            if daily_trading_hint or self._daily_trading_artifact_exists(context):
                self._append_holding_history_if_available(context)
                attach_daily_trading_context(exc, context)
            raise exc

        if output_file.exists():
            output = output_file.read_text()
        else:
            output = result.stdout.strip()
        output = output.strip() or "<i>Codex completed without output.</i>"
        if daily_trading_hint or self._daily_trading_artifact_exists(context):
            self._append_holding_history_if_available(context)
            output = append_daily_trading_started_at(output, context)
        return output

    def _daily_trading_artifact_exists(self, context) -> bool:
        return (self.config.workspace_dir / "reports" / "runs" / context.run_id / "run.json").is_file()

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
