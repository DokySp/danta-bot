from typing import Any

from ..schedule_commands import ScheduleCommandError, toggle_daily_schedules


def handle_schedule_on(worker: Any, task: Any, args: str) -> None:
    handle_schedule_toggle(worker, task, args, "on")


def handle_schedule_off(worker: Any, task: Any, args: str) -> None:
    handle_schedule_toggle(worker, task, args, "off")


def handle_schedule_toggle(worker: Any, task: Any, args: str, state: str) -> None:
    try:
        result = toggle_daily_schedules(
            worker.config.bundled_skills_dir,
            worker.config.schedule_file,
            state,
            args,
        )
    except ScheduleCommandError as exc:
        worker.gateway.send_message(exc.html_message, task.chat_id, task.route)
        return
    worker.gateway.send_message(result.html_message, task.chat_id, task.route)
