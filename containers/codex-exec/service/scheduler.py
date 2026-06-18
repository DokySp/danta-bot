import html
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from .config import Config
from .daily_trading import error_message_with_run_context, is_daily_trading_schedule
from .errors import UserFacingError
from .runner import CodexRunner
from .telegram_gateway import TelegramGateway, TypingIndicator


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
    ) -> None:
        logging.info(
            "running scheduled job id=%s model=%s reasoning_effort=%s",
            job_id,
            model or self.config.model,
            reasoning_effort or self.config.reasoning_effort,
        )
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
