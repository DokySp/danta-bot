import html
import logging
import os
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..codex.events import parse_codex_json_events
from ..codex.runner import CodexRunner
from ..codex.usage import append_token_usage_summary, read_usage_snapshot
from ..config import Config
from ..pipelines.deferred_buy_retry.pipeline import enqueue_deferred_buy_retries, load_deferred_buy_retry_config
from ..pipelines.daily_trading.holding_history import append_holding_history_from_run
from ..pipelines.daily_trading.launcher import (
    build_daily_trading_command,
    load_execute_trade_config,
)
from .daily_trading import (
    append_daily_trading_started_at,
    attach_daily_trading_context,
    new_codex_run_context,
)


def is_execute_trade_direct_request(text: str) -> bool:
    return text.strip() == "$execute-trade"


@dataclass(frozen=True)
class DailyTradingDirectResult:
    output: str
    html_report_path: Path | None


class DailyTradingDirectRunner:
    def __init__(self, config: Config, codex_runner: CodexRunner) -> None:
        self.config = config
        self.codex_runner = codex_runner

    def run(
        self,
        raw_config: Any,
        chat_id: str | None = None,
        route: str | None = None,
    ) -> DailyTradingDirectResult:
        context = new_codex_run_context()
        run_dir = self.config.workspace_dir / "reports" / "runs" / context.run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        usage_before = read_usage_snapshot(self.config)
        cmd, schedule_config = build_daily_trading_command(self.config, context, raw_config, run_dir)
        logging.info("running scheduled daily-trading pipeline command=%s", shlex.join(cmd))
        env = os.environ.copy()
        env["CODEX_MCP_TRADING_ENV"] = schedule_config.env
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
            self.append_holding_history(context)
            attach_daily_trading_context(exc, context)
            raise
        if result.returncode != 0:
            self.append_holding_history(context)
            message = (result.stderr or result.stdout or "").strip()
            exc = RuntimeError(f"daily-trading pipeline failed: {message[-1000:]}")
            attach_daily_trading_context(exc, context)
            raise exc
        self.append_holding_history(context)
        output = self.read_telegram_summary(run_dir)
        retry_config = load_deferred_buy_retry_config(self.config)
        retry_paths = []
        if retry_config.enabled:
            retry_paths = enqueue_deferred_buy_retries(
                workspace_dir=self.config.workspace_dir,
                source_run_dir=run_dir,
                chat_id=chat_id,
                route=route,
                delay_seconds=retry_config.delay_seconds,
                expires_after_seconds=retry_config.expires_after_seconds,
                slippage_bps=retry_config.slippage_bps,
            )
        if retry_paths:
            output = f"{output}\n\n후속 매수 재시도: {len(retry_paths)}건 예약됨"
        output = append_daily_trading_started_at(output, context)
        usage_after = read_usage_snapshot(self.config)
        output = append_token_usage_summary(
            output,
            self.config.workspace_dir,
            context,
            parse_codex_json_events(""),
            usage_before,
            usage_after,
        )
        html_report_path = run_dir / "daily-trading-report.html"
        return DailyTradingDirectResult(
            output=output,
            html_report_path=html_report_path if html_report_path.is_file() else None,
        )

    def read_telegram_summary(self, run_dir: Path) -> str:
        path = run_dir / "telegram-summary.txt"
        if not path.is_file():
            summary = run_dir / "pipeline-summary.json"
            raise RuntimeError(f"daily-trading telegram summary not found: {path}; summary={summary}")
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            raise RuntimeError(f"daily-trading telegram summary is empty: {path}")
        return text

    def append_holding_history(self, context: Any) -> None:
        try:
            append_holding_history_from_run(self.config.workspace_dir, context)
        except Exception:
            logging.exception("failed to append holding history run_id=%s", context.run_id)


def format_direct_runner_error(job_id: str, exc: Exception) -> str:
    return (
        f"<b>daily-trading direct runner failed</b>\n"
        f"<code>{html.escape(job_id)}</code>\n"
        f"<pre>{html.escape(str(exc))}</pre>"
    )


def format_direct_delivery_error(job_id: str, exc: Exception) -> str:
    return (
        f"<b>daily-trading 거래 실행 성공 / Telegram 전송 실패</b>\n"
        f"<code>{html.escape(job_id)}</code>\n"
        f"<pre>{html.escape(str(exc))}</pre>"
    )
