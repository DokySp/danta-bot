import html
import json
import logging
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .config import Config
from .daily_trading import (
    append_daily_trading_started_at,
    attach_daily_trading_context,
    new_codex_run_context,
)
from .holding_history import append_holding_history_from_run
from .runner import CodexRunner, parse_codex_json_events

ALLOWED_ENVS = {"acct", "paper"}
ALLOWED_REQUEST_TYPES = {"analysis", "prepare", "demo-submit", "real-submit"}
ALLOWED_ORDER_PATHS = {"auto", "reservation", "immediate"}
ALLOWED_VERDICT_INSTRUCTION_STAGES = {"first_verdict", "second_verdict"}
MAX_EXTRA_INSTRUCTION_COUNT = 10
MAX_EXTRA_INSTRUCTION_CHARS = 500


@dataclass(frozen=True)
class DailyTradingScheduleConfig:
    env: str
    request_type: str
    submit_orders: bool
    order_path: str
    verdict_extra_instructions: dict[str, list[str]]


def bool_value(raw: Any, *, default: bool = False) -> bool:
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    value = str(raw).strip().lower()
    if value in {"1", "true", "yes", "y", "on"}:
        return True
    if value in {"0", "false", "no", "n", "off"}:
        return False
    raise ValueError(f"expected boolean value, got {raw!r}")


def validate_daily_trading_config(raw: Any) -> DailyTradingScheduleConfig:
    if not isinstance(raw, dict):
        raise ValueError("daily_trading must be a mapping")
    env = str(raw.get("env") or "acct").strip().lower()
    request_type = str(raw.get("request_type") or "real-submit").strip()
    submit_orders = bool_value(raw.get("submit_orders"), default=request_type in {"demo-submit", "real-submit"})
    order_path = str(raw.get("order_path") or "auto").strip().lower()
    if env not in ALLOWED_ENVS:
        raise ValueError(f"daily_trading.env must be one of: {', '.join(sorted(ALLOWED_ENVS))}")
    if request_type not in ALLOWED_REQUEST_TYPES:
        raise ValueError(
            f"daily_trading.request_type must be one of: {', '.join(sorted(ALLOWED_REQUEST_TYPES))}"
        )
    if order_path not in ALLOWED_ORDER_PATHS:
        raise ValueError(f"daily_trading.order_path must be one of: {', '.join(sorted(ALLOWED_ORDER_PATHS))}")
    if request_type == "real-submit" and env != "acct":
        raise ValueError("daily_trading request_type=real-submit requires env=acct")
    if request_type == "demo-submit" and env != "paper":
        raise ValueError("daily_trading request_type=demo-submit requires env=paper")
    if submit_orders and request_type not in {"demo-submit", "real-submit"}:
        raise ValueError("daily_trading.submit_orders=true requires demo-submit or real-submit")
    extra = validate_verdict_extra_instructions(raw.get("verdict_extra_instructions"))
    return DailyTradingScheduleConfig(
        env=env,
        request_type=request_type,
        submit_orders=submit_orders,
        order_path=order_path,
        verdict_extra_instructions=extra,
    )


def execute_trade_config_candidates(config: Config) -> list[Path]:
    candidates: list[Path] = []
    env_path = os.getenv("EXECUTE_TRADE_CONFIG_FILE", "").strip()
    if env_path:
        candidates.append(Path(env_path))
    candidates.extend(
        [
            Path("/app/config/execute-trade.yaml"),
            config.workspace_dir / "containers/codex-exec/profiles/base/config/execute-trade.yaml",
            Path("containers/codex-exec/profiles/base/config/execute-trade.yaml"),
        ]
    )
    return candidates


def load_execute_trade_config(config: Config) -> dict[str, Any]:
    for path in execute_trade_config_candidates(config):
        if not path.exists():
            continue
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(payload, dict):
            raise ValueError(f"execute-trade config must be a mapping: {path}")
        daily_trading = payload.get("daily_trading")
        if not isinstance(daily_trading, dict):
            raise ValueError(f"execute-trade config missing daily_trading mapping: {path}")
        return daily_trading
    searched = ", ".join(str(path) for path in execute_trade_config_candidates(config))
    raise RuntimeError(f"execute-trade config not found; searched: {searched}")


def is_execute_trade_direct_request(text: str) -> bool:
    return text.strip() == "$execute-trade"


def validate_verdict_extra_instructions(raw: Any) -> dict[str, list[str]]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("daily_trading.verdict_extra_instructions must be a mapping")
    normalized: dict[str, list[str]] = {}
    for key, value in raw.items():
        stage = str(key).strip()
        if stage not in ALLOWED_VERDICT_INSTRUCTION_STAGES:
            raise ValueError(
                "daily_trading.verdict_extra_instructions keys must be first_verdict or second_verdict"
            )
        if not isinstance(value, list):
            raise ValueError(f"daily_trading.verdict_extra_instructions.{stage} must be a list")
        items: list[str] = []
        for item in value:
            text = str(item).strip()
            if not text:
                continue
            if len(text) > MAX_EXTRA_INSTRUCTION_CHARS:
                raise ValueError(
                    f"daily_trading.verdict_extra_instructions.{stage} item is too long"
                )
            items.append(text)
        if len(items) > MAX_EXTRA_INSTRUCTION_COUNT:
            raise ValueError(
                f"daily_trading.verdict_extra_instructions.{stage} has too many items"
            )
        if items:
            normalized[stage] = items
    return normalized


class DailyTradingDirectRunner:
    def __init__(self, config: Config, codex_runner: CodexRunner) -> None:
        self.config = config
        self.codex_runner = codex_runner

    def run(self, raw_config: Any) -> str:
        schedule_config = validate_daily_trading_config(raw_config)
        context = new_codex_run_context()
        run_dir = self.config.workspace_dir / "reports" / "runs" / context.run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        usage_before = self.codex_runner._read_usage_snapshot()
        script = self.pipeline_script()
        cmd = [
            sys.executable,
            str(script),
            "run",
            "--workspace-dir",
            str(self.config.workspace_dir),
            "--output-dir",
            str(run_dir),
            "--run-id",
            context.run_id,
            "--started-at",
            context.started_at,
            "--env",
            schedule_config.env,
            "--request-type",
            schedule_config.request_type,
            "--order-path",
            schedule_config.order_path,
        ]
        if schedule_config.submit_orders:
            cmd.append("--submit-orders")
        extra_path = self.write_extra_instructions(run_dir, schedule_config)
        if extra_path is not None:
            cmd.extend(["--verdict-extra-instructions-file", str(extra_path)])
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
        output = append_daily_trading_started_at(output, context)
        usage_after = self.codex_runner._read_usage_snapshot()
        return self.codex_runner._append_token_usage_summary(
            output,
            context,
            parse_codex_json_events(""),
            usage_before,
            usage_after,
        )

    def pipeline_script(self) -> Path:
        candidates = [
            self.config.workspace_dir
            / "containers/codex-exec/service/pipelines/daily_trading/scripts/run_daily_trading_pipeline.py",
            Path("/app/service/pipelines/daily_trading/scripts/run_daily_trading_pipeline.py"),
        ]
        for candidate in candidates:
            if candidate.exists():
                return candidate
        raise RuntimeError("daily-trading pipeline script not found")

    def write_extra_instructions(
        self,
        run_dir: Path,
        schedule_config: DailyTradingScheduleConfig,
    ) -> Path | None:
        if not schedule_config.verdict_extra_instructions:
            return None
        path = run_dir / "verdict-extra-instructions.json"
        payload = {
            "schema_version": "1",
            "verdict_extra_instructions": schedule_config.verdict_extra_instructions,
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        return path

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
