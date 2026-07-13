import html
import logging
import threading
from datetime import datetime
from typing import Any

from ..codex.runner import CodexRunner
from ..config import Config
from ..errors import UserFacingError
from ..telegram.gateway import TelegramGateway, TypingIndicator
from ..trading.daily_trading import error_message_with_run_context, is_daily_trading_schedule
from ..trading.daily_trading_direct import DailyTradingDirectRunner, format_direct_runner_error
from ..pipelines.deferred_buy_retry.pipeline import run_due_deferred_buy_retries
from .config import parse_yaml_schedule
from .cron import cron_matches


class Scheduler:
    def __init__(self, config: Config, runner: CodexRunner, gateway: TelegramGateway) -> None:
        self.config = config
        self.runner = runner
        self.daily_trading_direct_runner = DailyTradingDirectRunner(config, runner)
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
        run_due_deferred_buy_retries(self.config, self.gateway)
        minute_key = now.strftime("%Y%m%d%H%M")
        for item in parse_yaml_schedule(self.config.schedule_file):
            job_id = str(item.get("id", "")).strip()
            if not job_id:
                continue
            if item.get("enabled", True) is False:
                continue
            cron = str(item.get("cron", "")).strip()
            message = str(item.get("message", "")).strip()
            daily_trading_config = item.get("daily_trading")
            if not cron or (not message and daily_trading_config is None):
                continue
            key = (job_id, minute_key)
            if key in self.last_run_keys:
                continue
            if cron_matches(cron, now):
                model = optional_schedule_text(item.get("model"))
                reasoning_effort = optional_schedule_text(item.get("model_reasoning_effort"))
                self.last_run_keys.add(key)
                thread = threading.Thread(
                    target=self._run_job,
                    args=(
                        job_id,
                        message,
                        item.get("chat_id"),
                        item.get("route"),
                        model,
                        reasoning_effort,
                        daily_trading_config,
                    ),
                    name=f"schedule-{job_id}",
                    daemon=True,
                )
                thread.start()

    def _run_job(
        self,
        job_id: str,
        message: str,
        chat_id: Any,
        route: Any,
        model: str | None,
        reasoning_effort: str | None,
        daily_trading_config: Any,
    ) -> None:
        chat_id_text = str(chat_id) if chat_id else None
        route_text = str(route) if route else None
        try:
            if daily_trading_config is None:
                runtime_defaults = self.runner.runtime_defaults()
                model = model or runtime_defaults.model
                reasoning_effort = reasoning_effort or runtime_defaults.model_reasoning_effort
            logging.info(
                "running scheduled job id=%s model=%s reasoning_effort=%s",
                job_id,
                model or "daily-trading-runtime",
                reasoning_effort or "daily-trading-runtime",
            )
            with TypingIndicator(
                self.gateway,
                chat_id_text,
                route_text,
                self.config.telegram_typing_interval_seconds,
            ):
                if daily_trading_config is not None:
                    output = self.daily_trading_direct_runner.run(daily_trading_config, chat_id_text, route_text)
                else:
                    output = self.runner.run_once(
                        message,
                        daily_trading_hint=is_daily_trading_schedule(job_id),
                        model=model,
                        reasoning_effort=reasoning_effort,
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
            if daily_trading_config is not None:
                fallback = format_direct_runner_error(job_id, exc)
            message = error_message_with_run_context(exc, fallback)
            self.gateway.send_message(
                message,
                chat_id_text,
                route_text,
            )


def optional_schedule_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
