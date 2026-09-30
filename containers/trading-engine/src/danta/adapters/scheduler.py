"""Calendar-driven due intents; the application owns durable claim/execution."""

from dataclasses import dataclass
from datetime import datetime, timedelta

from . import AdapterError


@dataclass(frozen=True)
class ScheduledIntent:
    key: str
    kind: str
    due_at: datetime
    expires_at: datetime
    payload: dict


class SchedulePlanner:
    def __init__(self, jobs, *, quote_poll_seconds=5, order_poll_seconds=10):
        self.jobs = jobs
        self.polls = {"market_event_with_poll_fallback": quote_poll_seconds, "order_event_with_poll_fallback": order_poll_seconds}

    def due(self, now, *, session_id, continuous_open, continuous_close, enabled=False, discretionary_enabled=True, last_seen=None, events=()):
        if any(value.tzinfo is None for value in (now, continuous_open, continuous_close)) or continuous_open >= continuous_close:
            raise AdapterError("INVALID_EXCHANGE_SESSION")
        result = []
        in_session = continuous_open <= now < continuous_close
        for job in self.jobs:
            kind, trigger = job["kind"], job["trigger"]
            protective = kind in {"risk_monitor", "reconcile", "time_limit_exit"}
            if not protective and not enabled:
                continue
            if kind in {"full_review", "event_review"} and not discretionary_enabled:
                continue
            trigger_type = trigger["type"]
            due_times = []
            if trigger_type == "session_offset":
                anchor = {"continuous_open": continuous_open, "continuous_close": continuous_close}[trigger["anchor"]]
                due_times = [(anchor + timedelta(minutes=trigger["minutes"]), {})]
            elif trigger_type in {"interval_in_session", *self.polls} and in_session:
                interval = trigger.get("seconds", self.polls.get(trigger_type))
                if type(interval) is not int or interval <= 0:
                    raise AdapterError("INVALID_SCHEDULE_INTERVAL")
                slot = int((now - continuous_open).total_seconds()) // interval
                due_times = [(continuous_open + timedelta(seconds=slot * interval), {})]
            elif trigger_type == "verified_event":
                seconds = trigger["coalesce_seconds"]
                due_times = [(event["verified_at"] + timedelta(seconds=seconds), {"event_id": event["event_id"]}) for event in events if event.get("verified") is True]
            for due_at, payload in due_times:
                if due_at > now:
                    continue
                # An expired discretionary entry is skipped after restart. Protection is rechecked.
                expiry = continuous_close - timedelta(minutes=30) if kind in {"full_review", "event_review"} else continuous_close
                if kind == "finalize_and_report":
                    expiry = due_at + timedelta(hours=12)
                if now >= expiry and not protective:
                    continue
                key = f"{session_id}:{job['id']}:{due_at.isoformat()}:{payload.get('event_id', '')}"
                if last_seen is not None and key in last_seen:
                    continue
                result.append(ScheduledIntent(key, kind, due_at, expiry, payload))
        return tuple(result)

    @staticmethod
    def recovery_intents():
        return ("reconcile", "risk_monitor", "time_limit_exit")
