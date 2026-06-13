def parse_telegram_command(text: str) -> tuple[str, str] | None:
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None
    command, _, args = stripped[1:].partition(" ")
    return command, args.strip()
