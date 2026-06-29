from datetime import datetime


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
        field_matches(minute, now.minute, 0, 59)
        and field_matches(hour, now.hour, 0, 23)
        and field_matches(day, now.day, 1, 31)
        and field_matches(month, now.month, 1, 12)
        and field_matches(weekday, cron_weekday, 0, 7)
    )


def field_matches(expr: str, value: int, minimum: int, maximum: int) -> bool:
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
